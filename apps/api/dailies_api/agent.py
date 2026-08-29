"""The investigator: the agent that says why a shot is failing, with its work shown.

A render supervisor does not need another dashboard. They need an answer to "what is
wrong with shot SH040 and will it make the 6pm review", and they need to be able to check
that answer without re-doing the investigation. Everything in this module exists to make
the answer *checkable*, because an LLM diagnosis that cannot be checked is worse than no
diagnosis: it is a confident sentence that a human will act on.

Three things enforce that:

1. **The response schema** (:data:`DIAGNOSIS_SCHEMA`). ``evidence`` is required, may
   not be empty (``minItems``), and every entry must carry both the query that was run
   and what it showed (``required`` on the item). A cause with no evidence behind it is a
   schema violation, not a stylistic lapse, and each of those three constraints is pinned
   by a test that validates a candidate answer against the schema.
2. **The instruction** (:data:`INVESTIGATOR_INSTRUCTION`). The rules that a model will
   otherwise break under pressure to be helpful: no unsupported cause, report
   disagreement rather than resolving it by preference, a completed frame is not a
   correct frame, and "low" confidence is a real answer.
3. **The tools.** They are not free-text Grafana access. Each one is a method on the
   :class:`~dailies_api.mcp_client.GrafanaMCP` wrapper, whose tool names and JSON key
   spellings were verified against grafana/mcp-grafana source and are pinned by tests.
   The model sees the wrapper's Python parameter names; the wrapper does the camelCase
   translation and raises a typed error when Grafana says no.

Why the schema is in the prompt and not in ``output_schema``: ADK documents that setting
``LlmAgent.output_schema`` means the agent "can ONLY reply and CANNOT use any tools". An
investigator that cannot query Grafana has nothing to investigate with, and the failure
would be silent - a well-formed JSON diagnosis invented from nothing. So the schema is
rendered into the instruction and structured output is left off.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from functools import wraps
from types import MethodType
from typing import TYPE_CHECKING, Any, NoReturn

try:
    from google.adk.agents import Agent
    from google.adk.tools import FunctionTool
except ImportError as exc:  # the ADK is an optional extra; say which one.
    raise ImportError(
        "dailies_api.agent needs the Google ADK, which this project ships in its "
        'optional "agent" extra rather than its base dependencies. Install it with: '
        'pip install "dailies[agent]"'
    ) from exc

from dailies_api.mcp_client import GrafanaMCP, GrafanaMCPError

_log = logging.getLogger(__name__)

if TYPE_CHECKING:
    # ADK's own alias for what ``LlmAgent.tools`` accepts: a ``BaseTool``, a
    # ``BaseToolset`` such as ``McpToolset``, or a plain callable. Naming it is what
    # lets ``mcp_tools`` say something more useful than ``Any``, and keeping the import
    # annotation-only means an upstream rename cannot break importing this module.
    from google.adk.agents.llm_agent import ToolUnion

__all__ = [
    "DIAGNOSIS_SCHEMA",
    "GRAFANA_MCP_TOOLS",
    "INVESTIGATOR_INSTRUCTION",
    "INVESTIGATOR_MODEL",
    "GrafanaNotConfigured",
    "build_investigator",
]

#: The Gemini model the investigator runs on.
#:
#: ``gemini-2.5-flash``: fast enough that a judge clicking Diagnose is not left waiting,
#: and strong enough for this agent's shape, which is several tool calls each deciding the
#: next, ending in a schema-constrained answer.
#:
#: This id was **verified against Vertex in this project and region** rather than read out
#: of a docs page, and the distinction turned out to matter. The previous value here was
#: ``gemini-3.7-flash``, taken from ai.google.dev on 2026-08-27. That is the Gemini *API*
#: surface; this project runs on *Vertex*, and the two do not publish the same ids. Vertex
#: answers that name with a flat 404:
#:
#:     Publisher model `.../publishers/google/models/gemini-3.7-flash` was not found or
#:     your project does not have access to it.
#:
#: A live sweep of us-central1 on 2026-08-29 returned exactly three servable ids:
#: ``gemini-2.5-flash``, ``gemini-2.5-flash-lite`` and ``gemini-2.5-pro``. Nothing 3.x
#: resolves, with or without a dot in the version.
#:
#: The failure mode is why this comment is long. ADK does not validate the id beyond the
#: ``gemini-.*`` pattern, so a wrong one raises nothing at import, passes every test that
#: fakes the model, and dies at the first real diagnosis. Every unit test here injects a
#: fake, so the whole suite stays green while the deployed agent cannot answer at all.
#: **Re-verify with a real ``generateContent`` call before changing this**, against the
#: project and region the code actually runs in. Override per call site with ``model=``;
#: ``gemini-2.5-pro`` is the one-parameter upgrade if a diagnosis needs deeper reasoning.
INVESTIGATOR_MODEL = "gemini-2.5-flash"

#: Grafana MCP tools the investigator may be given, by their server-side names.
#:
#: An allow-list rather than "any attribute on the wrapper": ``available_tools`` and
#: ``has_tool`` are wrapper methods and not Grafana tools, and handing a model something
#: that is not a real MCP tool produces a call that fails against a live stack. Every name
#: here is pinned to a wrapper method by a test, so the two cannot drift apart quietly.
#:
#: ``get_panel_image`` is deliberately absent. It answers with an MCP image block and the
#: wrapper hands back ``PanelImage`` bytes, which is not a payload a function response can
#: carry back to the model. Pixels belong to the validation path, which consumes the PNG
#: directly rather than through tool calling.
GRAFANA_MCP_TOOLS = frozenset(
    {
        "query_prometheus",
        "list_prometheus_metric_names",
        "query_loki_logs",
        "create_annotation",
        "create_incident",
        "add_activity_to_incident",
    }
)

#: The shape of a diagnosis.
#:
#: ``evidence`` is the load-bearing field and the reason this schema exists. A model asked
#: for a cause will produce a plausible one whether or not it looked; a model asked for a
#: cause *and* the queries behind it has to either do the work or emit an answer that
#: fails validation in a way anyone can see. ``confidence`` is a closed set including
#: ``"low"`` so that uncertainty has somewhere to go other than into hedged prose in
#: ``cause``.
DIAGNOSIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["shot", "cause", "evidence", "confidence"],
    "properties": {
        "shot": {"type": "string"},
        "cause": {"type": "string", "description": "One sentence naming the root cause"},
        "evidence": {
            "type": "array",
            # An answer with no evidence, or a finding with no query behind it, is
            # exactly the failure this schema exists to catch. Without these two
            # keywords both validate, and the constraint lives only in the prose.
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["query", "finding"],
                "properties": {
                    "query": {"type": "string"},
                    "finding": {"type": "string"},
                },
            },
            "description": ("The queries run and what each showed. Never assert without one."),
        },
        "affected_frames": {"type": "string"},
        "recommended_action": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    },
}

INVESTIGATOR_INSTRUCTION = f"""\
You are the Dailies investigator. A render farm is producing frames for a shot that has a
creative deadline, and something has gone wrong or someone believes it has. Your job is to
find out what, from live telemetry, and to answer in a form a supervisor can act on and
another engineer can check.

You read the render through Grafana MCP tools. Prometheus holds the render metrics (frame
durations, memory, failure counts, per-shot progress). Loki holds the render logs, which
is where Blender says what actually went wrong on a frame. Some investigators are also
given tools that write back: an annotation puts your conclusion on the Grafana timeline
next to the metrics that produced it, and an incident escalates a shot that will not make
its deadline. Use only the tools you were actually given; the list you have is the list
that exists.

How to work:

1. Look before you conclude. Start from the metrics for the shot, then read the logs for
   the frames the metrics point at. If you do not know which metrics exist, list them
   rather than guessing a name.
2. Every Prometheus query is a range query. Pass query_type="range" and a start_time,
   always. A render is a batch job that ends, and an instant query returns nothing at all
   for a series whose job has finished. Empty is not "no such metric" and not "the job
   never recorded it"; it is almost always the wrong query type.
3. Read the raw series, not rate() or increase(). Those extrapolate over a window and are
   built for traffic that arrives continuously. A render counter goes 0 to 3 in one burst
   over a few seconds, and increase(...[24h]) turns that into a fraction like 1.5, which
   means nothing. Take the last value of the series.
4. Prefer the narrowest query that answers the question. A query over the whole farm when
   one shot is in question buries the signal you are looking for.
5. Follow the frames. A shot-level average hides the two frames that are actually broken;
   per-frame data is usually where the cause is.
6. Stop when the evidence stops. Extra queries that show nothing are still evidence, and
   an investigation that found nothing conclusive is a real result.

Rules you do not break:

- Never state a cause you have not supported with a query result. Every entry in evidence
  must name the query you ran and what it showed.
- If metrics and logs disagree, say so rather than picking one. Two sources disagreeing is
  itself a finding, and it is often the most useful thing you can report.
- A frame that completed is not necessarily correct. If logs show a missing asset on a
  frame that saved successfully, that is a defective deliverable, not a success. A green
  metric means the process exited, not that the picture is right.
- Say what actually happened, not what sounds worse. If frames_completed equals
  frames_expected then the render exited 0 and completed all of its frames: it did NOT
  "fail to render", however bad the logs are. A shot that succeeded and produced a wrong
  picture is a different problem from a shot that crashed, it is found differently and it
  is fixed differently, and calling the first one a render failure sends a supervisor to
  look at the farm when they should be looking at the asset. When you cannot get a clean
  reading of the frame counts, say the counts were unavailable rather than inferring a
  failure from their absence.
- Report confidence honestly. "low" is a valid answer. Say "low" when the evidence is thin,
  when the queries were inconclusive, or when more than one cause fits what you found. A
  confident wrong diagnosis costs a supervisor a whole re-render.

Answer with a single JSON object and nothing else. It must match this schema:

{json.dumps(DIAGNOSIS_SCHEMA, indent=2)}
"""

_DESCRIPTION = (
    "Diagnoses render failures for a shot by querying live Grafana telemetry, and "
    "answers with a root cause bound to the queries that support it."
)


class GrafanaNotConfigured(RuntimeError):
    """A Grafana tool was called on an investigator that was built without a client.

    ``build_investigator`` intentionally allows a client-less agent: composing the tool
    set, inspecting the declarations and testing the prompt contract are all things worth
    doing without a Grafana connection. Actually calling a tool is not, and this says so
    by name instead of surfacing an ``AttributeError`` on ``None`` from somewhere inside
    the wrapper.
    """


class _Unconfigured:
    """Stands in for the ``GrafanaMCP`` client when none was supplied.

    Bound as ``self`` on the wrapper's own methods, so the *real* wrapper code runs and
    the first thing it touches - a datasource UID, an internal helper - raises. That keeps
    one code path for configured and unconfigured agents; there is no second, fake
    implementation of the wrapper to drift out of sync with the real one.
    """

    __slots__ = ()

    def __getattr__(self, attribute: str) -> NoReturn:
        raise GrafanaNotConfigured(
            f"This investigator has no Grafana client, so {attribute!r} cannot be "
            "reached. Pass grafana=GrafanaMCP(session, ...) to build_investigator()."
        )


_UNCONFIGURED = _Unconfigured()


def _grafana_tool(name: str, client: GrafanaMCP | _Unconfigured) -> FunctionTool:
    """Wrap one Grafana MCP tool as an ADK tool bound to the wrapper.

    The callable handed to ``FunctionTool`` is the wrapper's own method bound to
    ``client``, which is what makes the model-facing surface correct for free: the tool
    name is the method name (which is the MCP tool name), the description is the method's
    docstring, and the parameters are its signature. Nothing about the tool is written out
    a second time here, so nothing about it can disagree with the code that runs.

    ``MethodType`` rather than ``getattr(client, name)`` because ``_Unconfigured`` raises
    on attribute access by design - the failure belongs at call time, not build time. The
    function bound is ``GrafanaMCP``'s own, so a tool always runs the wrapper this module
    validated the name against.
    """
    if name not in GRAFANA_MCP_TOOLS:
        raise ValueError(
            f"{name!r} is not a Grafana MCP tool this project wraps. Available: "
            f"{', '.join(sorted(GRAFANA_MCP_TOOLS))}."
        )
    return FunctionTool(_recoverable(MethodType(getattr(GrafanaMCP, name), client)))


def _recoverable(bound: Any) -> Any:
    """Return a tool that hands a rejected query back to the model instead of raising.

    A query the model wrote and the datasource refused is not a failure of the
    investigation; it is a correction the model is capable of making. Loki answering
    ``parse error at line 0, col 52: syntax error: unexpected |=`` says precisely what
    is wrong, and the agent that wrote the query is the one thing able to act on it.

    Raising throws that away. Measured on 2026-08-29: asked to diagnose SH050, the model
    wrote one malformed LogQL query, ``ToolCallFailed`` propagated out of the ADK run,
    and the entire diagnosis came back to the board as a 502 - for a shot whose telemetry
    was sitting there, complete and readable.

    Only :class:`~dailies_api.mcp_client.GrafanaMCPError` is converted, which is the far
    side saying "this request was wrong". A transport failure is not caught here and
    still propagates: the server being unreachable is not something a reworded query
    fixes, and pretending otherwise would have the agent retry its way through a whole
    investigation against a Grafana that is simply down, then answer as if it had looked.

    ``functools.wraps`` matters more than it looks. ADK builds the tool's name,
    description and parameter schema by introspecting this callable, so a wrapper that
    did not carry the name, docstring and signature through would present the model a
    nameless tool taking ``*args`` - which is a worse failure than the one being fixed,
    and a silent one.
    """

    @wraps(bound)
    async def tool(*args: Any, **kwargs: Any) -> Any:
        try:
            return await bound(*args, **kwargs)
        except GrafanaMCPError as exc:
            _log.warning("Tool %s rejected the model's call: %s", bound.__name__, exc)
            return {
                "error": str(exc),
                "hint": (
                    "The datasource rejected this call. Read the message, correct the "
                    "query and try again; do not report this as a finding about the shot."
                ),
            }

    return tool


def _reject_duplicate_tool_names(tools: list[Any]) -> None:
    """Raise if two resolved tools would declare the same function name to Gemini.

    The Gemini API rejects a request whose function declarations share a name, so a
    repeated tool is the same failure class as a typo'd one and belongs at the same
    place: build time, not the agent's first live turn. A toolset or a plain callable
    has no ``name`` attribute of its own - it resolves its declarations later - so it is
    skipped here rather than guessed at.
    """
    seen: set[str] = set()
    for tool in tools:
        name = getattr(tool, "name", None)
        if name is None:
            continue
        if name in seen:
            raise ValueError(
                f"{name!r} was given to build_investigator() more than once. Gemini "
                "rejects a request whose function declarations share a name, so this "
                "agent would fail on its first turn."
            )
        seen.add(name)


def build_investigator(
    mcp_tools: Iterable[str | ToolUnion],
    *,
    grafana: GrafanaMCP | None = None,
    model: str = INVESTIGATOR_MODEL,
    name: str = "investigator",
) -> Agent:
    """Build the investigator agent over a chosen set of Grafana tools.

    Args:
        mcp_tools: What the agent may call. A string is looked up in
            :data:`GRAFANA_MCP_TOOLS` and wrapped; anything else (an ADK ``BaseTool``, a
            toolset such as ``McpToolset``, a plain callable) is passed through untouched,
            so a caller that already has a live MCP toolset can hand it over directly.
        grafana: The Grafana MCP wrapper the string-named tools call. Omit it to build an
            agent whose tools raise :class:`GrafanaNotConfigured` when invoked.
        model: Gemini model id. Defaults to :data:`INVESTIGATOR_MODEL`.
        name: The ADK agent name, which must be a valid Python identifier.

    Raises:
        ValueError: If a tool name is not one this project wraps, if the same tool is
            given twice, or if the resolved tool set is empty. Deliberately at build
            time: every one of those is otherwise a runtime failure against a live
            Grafana or a live model, which in practice means mid-demo. An investigator
            with no tools is the worst of the three, because it does not fail at all -
            it reads no telemetry and answers anyway.
    """
    client: GrafanaMCP | _Unconfigured = _UNCONFIGURED if grafana is None else grafana
    tools = [_grafana_tool(item, client) if isinstance(item, str) else item for item in mcp_tools]
    if not tools:
        raise ValueError(
            "build_investigator() needs at least one tool; an investigator with none "
            "has no telemetry to read and would answer from the prompt alone. "
            f"Available Grafana tools: {', '.join(sorted(GRAFANA_MCP_TOOLS))}."
        )
    _reject_duplicate_tool_names(tools)
    return Agent(
        name=name,
        model=model,
        description=_DESCRIPTION,
        instruction=INVESTIGATOR_INSTRUCTION,
        tools=tools,
    )
