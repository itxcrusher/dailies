"""One diagnosis, end to end: open an MCP session, run the investigator, check the answer.

This is the seam between the three pieces that already exist and know nothing about each
other - :mod:`dailies_api.mcp_transport` (the wire), :class:`dailies_api.mcp_client.GrafanaMCP`
(the tool surface) and :func:`dailies_api.agent.build_investigator` (the agent) - and the
HTTP route that a supervisor presses a button on.

Two things here are not plumbing.

**The session's lifetime is the investigation's lifetime.** A session is opened per
diagnose call and closed when it ends, including when the model raises. A long-lived
shared session would be cheaper by one handshake and would put a Cloud-Run-scaled process
in charge of keeping an MCP session alive across cold starts, scale-to-zero and instance
recycling; the failure mode is a stale session id that answers 400 on the one request
anybody was watching.

**The answer is checked before it is stored.** :func:`parse_diagnosis` re-applies the
constraints the investigator was *told* to answer under, because a prompt is a request
and not a guarantee. The one that matters is ``evidence``: a cause with nothing behind it
is exactly the confident-and-unverifiable output this project exists to argue against, and
storing one on the board would put it in front of a supervisor with the same weight as a
real diagnosis. It is refused instead, loudly, with what the model actually said.

The MCP connection and the model call are both injected with working defaults, which is
what lets the whole pipeline be tested without a network while the defaults remain the
code that actually runs in production.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Iterable, Sequence
from typing import TYPE_CHECKING, Any

from dailies_api.mcp_client import GrafanaMCP, MCPSession
from dailies_api.mcp_transport import connect

if TYPE_CHECKING:  # pragma: no cover - annotation only, so this module imports without ADK
    from contextlib import AbstractAsyncContextManager

    from google.adk.agents import Agent

__all__ = [
    "APP_NAME",
    "DEFAULT_TOOLS",
    "Diagnose",
    "InvestigationFailed",
    "build_diagnoser",
    "investigation_prompt",
    "parse_diagnosis",
    "run_agent",
]

#: What a diagnose call may do to the Grafana stack: read it. The wrapper also exposes
#: ``create_annotation`` and the two incident tools, and they are deliberately absent
#: here - a button a supervisor presses to *ask a question* must not write to the
#: timeline or open an incident as a side effect. Writing back is the Guardian's job,
#: which decides to escalate rather than being asked to look.
DEFAULT_TOOLS: tuple[str, ...] = (
    "query_prometheus",
    "list_prometheus_metric_names",
    "query_loki_logs",
)

#: The ADK app name and the user the runner attributes a diagnose call to. One session
#: per call, so neither is a real identity; they exist because the runner requires them.
APP_NAME = "dailies"
_USER_ID = "dailies-api"

#: Async callable the HTTP route holds: a shot id in, a checked diagnosis out.
Diagnose = Callable[[str], Awaitable[dict[str, Any]]]

_SessionFactory = Callable[[str], "AbstractAsyncContextManager[MCPSession]"]
_RunAgent = Callable[["Agent", str], Awaitable[str]]


class InvestigationFailed(RuntimeError):
    """The investigator did not produce a diagnosis that can be shown to anyone.

    Distinct from a transport failure on purpose: the MCP server being unreachable and
    the model answering with prose need different fixes, and the route turns them into
    different statuses.
    """


def investigation_prompt(shot_id: str) -> str:
    """What the investigator is asked, for one shot.

    Carries the two facts about *this* stack that the agent cannot discover without
    wasting turns on it, both verified against the live Grafana on 2026-08-29:

    - the label spelling. Telemetry keys a series by the bare shot label, while the board
      keys a row by the composite ``project:sequence:shot:render_job`` id, so an agent
      handed only the board's id would filter on a label value that exists nowhere.
    - the staleness trap. An instant PromQL query returns nothing at all for a job that
      has finished, because the series has fallen outside Prometheus staleness. That
      reads exactly like "no such metric", and an investigator that believes it reports a
      shot with no telemetry rather than a shot with a problem.
    """
    label = shot_label(shot_id)
    return (
        f"Diagnose shot {label} on the current render.\n\n"
        f"The board tracks it as {shot_id!r}; Grafana labels the series for it "
        f'shot="{label}", on job "dailies-render".\n\n'
        "Two things about this stack, so you do not waste turns rediscovering them:\n"
        "- The render job has usually finished by the time you are asked. An instant "
        "PromQL query returns nothing for a finished job, because the series has passed "
        "out of Prometheus staleness. Query a range over the last 30 to 60 minutes "
        "instead, and do not read an empty instant result as an absent metric.\n"
        "- A render that exited 0 can still have produced broken frames. The logs are "
        "where that shows up, so read them even when the metrics look healthy.\n\n"
        "Answer with the JSON object described in your instructions and nothing else."
    )


def shot_label(shot_id: str) -> str:
    """The bare shot label inside a composite board id.

    ``project:sequence:shot:render_job`` -> ``shot``. Anything that is not the composite
    form is already a bare label (a fixture, a hand-registered shot) and is returned
    unchanged, because guessing at a shorter one would silently query the wrong series.
    """
    parts = shot_id.split(":")
    return parts[2] if len(parts) == 4 else shot_id


async def run_agent(agent: Agent, prompt: str) -> str:
    """Run the investigator to its final answer and return the text of it.

    The ADK's in-memory runner: session state lives for this call only, which is what a
    one-shot investigation wants, and it means the API holds no conversation to leak
    between shots or between users.

    Imported inside the function rather than at module import: the ADK is the ``agent``
    extra, and the read-only board routes must stay importable on the base install.
    """
    from google.adk.runners import InMemoryRunner
    from google.genai import types

    runner = InMemoryRunner(agent=agent, app_name=APP_NAME)
    try:
        session = await runner.session_service.create_session(app_name=APP_NAME, user_id=_USER_ID)
        answer = ""
        message = types.Content(role="user", parts=[types.Part(text=prompt)])
        async for event in runner.run_async(
            user_id=_USER_ID, session_id=session.id, new_message=message
        ):
            # Only the final response, and the last one of those: the stream also carries
            # every tool call and its result, and concatenating those would hand
            # parse_diagnosis a transcript instead of an answer.
            if event.is_final_response() and event.content and event.content.parts:
                answer = "".join(part.text or "" for part in event.content.parts)
        return answer
    finally:
        await runner.close()


def parse_diagnosis(answer: str, shot_id: str) -> dict[str, Any]:
    """Turn the model's text into a diagnosis, or refuse it.

    Args:
        answer: What the investigator replied with.
        shot_id: The shot under investigation, for the error message. The model's own
            ``shot`` field is left exactly as it answered it: silently overwriting it
            would hide an investigator that had gone and looked at the wrong shot, which
            is precisely the error worth seeing.

    Raises:
        InvestigationFailed: if the answer is not a JSON object, or does not carry the
            fields :data:`dailies_api.agent.DIAGNOSIS_SCHEMA` requires, or carries
            evidence that is empty or missing the query behind a finding. The message
            always includes what the model actually said, because "the diagnosis failed
            validation" without the text is not debuggable after the fact.

    ``confidence`` is required but its *value* is not checked against the schema's enum.
    A model that answers ``"moderate"`` has still done the investigation and still shown
    its evidence, and failing the whole call over one word would trade the useful part of
    the answer for a cosmetic rule. The evidence constraints are enforced because the
    answer is worthless without them, which is a different thing.
    """
    from dailies_api.agent import DIAGNOSIS_SCHEMA

    payload = _as_json_object(answer, shot_id)

    missing = [field for field in DIAGNOSIS_SCHEMA["required"] if field not in payload]
    if missing:
        raise InvestigationFailed(
            f"The diagnosis for {shot_id!r} is missing required field(s) "
            f"{', '.join(missing)}. The model answered: {answer!r}"
        )

    evidence = payload["evidence"]
    if not isinstance(evidence, list) or not evidence:
        raise InvestigationFailed(
            f"The diagnosis for {shot_id!r} carries no evidence, so its cause cannot be "
            f"checked by anyone. The model answered: {answer!r}"
        )
    for entry in evidence:
        if not isinstance(entry, dict) or not entry.get("query") or not entry.get("finding"):
            raise InvestigationFailed(
                f"An evidence entry for {shot_id!r} does not name both the query and "
                f"what it found: {entry!r}. The model answered: {answer!r}"
            )
    return payload


def _as_json_object(answer: str, shot_id: str) -> dict[str, Any]:
    """The JSON object in ``answer``, tolerating a code fence around it.

    A fence is the one deviation worth absorbing: it is a formatting habit rather than a
    failure to follow the instruction, and the object inside it is intact. Anything
    beyond that - prose with an object buried in it, two objects, a bare array - is
    refused rather than guessed at, because a guess here silently changes what a
    supervisor reads.
    """
    text = answer.strip()
    if text.startswith("```"):
        # ```json\n{...}\n``` -> {...}
        fenced = text.split("```")
        text = fenced[1] if len(fenced) > 1 else text
        _, _, body = text.partition("\n")
        text = (body or text).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InvestigationFailed(
            f"The investigator's answer for {shot_id!r} is not JSON ({exc.msg}). "
            f"The model answered: {answer!r}"
        ) from exc
    if not isinstance(payload, dict):
        raise InvestigationFailed(
            f"The investigator's answer for {shot_id!r} is JSON but not an object "
            f"({type(payload).__name__}). The model answered: {answer!r}"
        )
    return payload


def build_diagnoser(
    *,
    mcp_url: str,
    prometheus_uid: str | None = None,
    loki_uid: str | None = None,
    tools: Iterable[str] = DEFAULT_TOOLS,
    session_factory: _SessionFactory = connect,
    run: _RunAgent = run_agent,
) -> Diagnose:
    """Build the callable the diagnose route runs.

    Args:
        mcp_url: The Grafana MCP server's URL. ``/mcp`` is appended by the transport.
        prometheus_uid: Datasource UID for the metrics queries.
        loki_uid: Datasource UID for the log queries.
        tools: Which Grafana tools the investigator gets. Defaults to
            :data:`DEFAULT_TOOLS`, which is read-only.
        session_factory: Opens the MCP session. Defaults to
            :func:`dailies_api.mcp_transport.connect`; a test passes one over a fake.
        run: Runs the agent and returns its text. Defaults to :func:`run_agent`; a test
            passes one that never reaches Gemini.

    Returns:
        An async callable taking a shot id and returning the checked diagnosis.
    """
    tool_names: Sequence[str] = tuple(tools)

    async def diagnose(shot_id: str) -> dict[str, Any]:
        # Imported here, not at module scope: build_investigator needs the ADK, and this
        # module is imported by the API process whether or not a diagnosis is ever asked
        # for. The failure, when the extra is missing, is agent.py's pointed ImportError.
        from dailies_api.agent import build_investigator

        async with session_factory(mcp_url) as session:
            grafana = GrafanaMCP(session, prometheus_uid=prometheus_uid, loki_uid=loki_uid)
            agent = build_investigator(tool_names, grafana=grafana)
            answer = await run(agent, investigation_prompt(shot_id))
        return parse_diagnosis(answer, shot_id)

    return diagnose
