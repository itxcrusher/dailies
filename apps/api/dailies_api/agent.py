"""The investigator: the agent that says why a shot is failing, with its work shown.

A render supervisor does not need another dashboard. They need an answer to "what is
wrong with shot SH040 and will it make the 6pm review", and they need to be able to check
that answer without re-doing the investigation. Everything in this module exists to make
the answer *checkable*, because an LLM diagnosis that cannot be checked is worse than no
diagnosis: it is a confident sentence that a human will act on.

Three things enforce that:

1. **The response schema** (:data:`DIAGNOSIS_SCHEMA`). ``evidence`` is required, and each
   entry pairs the query that was run with what it showed. A cause with an empty evidence
   list is a schema violation, not a stylistic lapse.
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
from collections.abc import Iterable
from types import MethodType
from typing import Any, NoReturn

from google.adk.agents import Agent
from google.adk.tools import FunctionTool

from dailies_api.mcp_client import GrafanaMCP

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
#: ``gemini-3.7-flash``, read off ai.google.dev/gemini-api/docs/models on 2026-08-27,
#: where it is the current generally-available Flash model and the one described as built
#: for agentic workflows and reliable multi-step execution. That is this agent's shape:
#: several tool calls, each one deciding the next. A preview id was avoided deliberately -
#: ``gemini-3-pro-preview`` was shut down in March 2026, and a demo that dies on a preview
#: retirement is a demo that dies.
#:
#: ADK does not validate this beyond the ``gemini-.*`` pattern, so a wrong id here fails
#: at the first API call, not at import. Override per call site with ``model=``.
INVESTIGATOR_MODEL = "gemini-3.7-flash"

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
#: cause *and* the queries behind it has to either do the work or visibly leave the array
#: empty. ``confidence`` is a closed set including ``"low"`` so that uncertainty has
#: somewhere to go other than into hedged prose in ``cause``.
DIAGNOSIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["shot", "cause", "evidence", "confidence"],
    "properties": {
        "shot": {"type": "string"},
        "cause": {"type": "string", "description": "One sentence naming the root cause"},
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "finding": {"type": "string"},
                },
            },
            "description": (
                "The queries run and what each showed. Never assert without one."
            ),
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
2. Prefer the narrowest query that answers the question. A query over the whole farm when
   one shot is in question buries the signal you are looking for.
3. Follow the frames. A shot-level average hides the two frames that are actually broken;
   per-frame data is usually where the cause is.
4. Stop when the evidence stops. Extra queries that show nothing are still evidence, and
   an investigation that found nothing conclusive is a real result.

Rules you do not break:

- Never state a cause you have not supported with a query result. Every entry in evidence
  must name the query you ran and what it showed.
- If metrics and logs disagree, say so rather than picking one. Two sources disagreeing is
  itself a finding, and it is often the most useful thing you can report.
- A frame that completed is not necessarily correct. If logs show a missing asset on a
  frame that saved successfully, that is a defective deliverable, not a success. A green
  metric means the process exited, not that the picture is right.
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
    return FunctionTool(MethodType(getattr(GrafanaMCP, name), client))


def build_investigator(
    mcp_tools: Iterable[Any],
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
        ValueError: If a tool name is not one this project wraps. Deliberately at build
            time: a typo in a tool name is otherwise a runtime failure against a live
            Grafana, which in practice means mid-demo.
    """
    client: GrafanaMCP | _Unconfigured = _UNCONFIGURED if grafana is None else grafana
    tools = [
        _grafana_tool(item, client) if isinstance(item, str) else item
        for item in mcp_tools
    ]
    return Agent(
        name=name,
        model=model,
        description=_DESCRIPTION,
        instruction=INVESTIGATOR_INSTRUCTION,
        tools=tools,
    )
