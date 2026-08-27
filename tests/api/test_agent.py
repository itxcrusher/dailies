"""Tests for the investigator agent.

None of this tests Gemini. What an LLM says back is not a property this repo can assert,
and pinning it would be a test that fails on a model upgrade rather than on a bug. What
is tested is everything the repo actually owns and can get wrong silently:

- the **tool wiring**: which Grafana MCP tools the agent is handed, that each name
  resolves to a real method on the verified wrapper rather than a plausible-looking
  string, that the parameter names the model will see are the wrapper's own, and that a
  call really lands on the wrapper;
- the **prompt contract**: the four rules that make a diagnosis evidence-bound, and the
  response schema that carries the evidence.

The ADK ``Agent`` is the real installed one (google-adk 1.20.0); nothing here is stubbed.
No network: the Grafana session is a local fake, and building an agent never contacts a
model.
"""

import json
from types import SimpleNamespace

import pytest
from dailies_api.agent import (
    DIAGNOSIS_SCHEMA,
    GRAFANA_MCP_TOOLS,
    INVESTIGATOR_INSTRUCTION,
    INVESTIGATOR_MODEL,
    GrafanaNotConfigured,
    build_investigator,
)
from dailies_api.mcp_client import GrafanaMCP
from google.adk.utils.instructions_utils import inject_session_state


class _Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _Result:
    def __init__(self, text):
        self.content = [_Block(text)]
        self.isError = False


class _RecordingSession:
    """An MCP session that records the call and answers with fixed JSON."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def list_tools(self):
        raise AssertionError("the investigator tests never list tools")

    async def call_tool(self, name, args, /):
        self.calls.append((name, args))
        return _Result(json.dumps(self.payload))


# -- the response schema ---------------------------------------------------------


def test_diagnosis_schema_requires_evidence():
    assert "evidence" in DIAGNOSIS_SCHEMA["required"]
    assert "cause" in DIAGNOSIS_SCHEMA["required"]


def test_diagnosis_schema_evidence_entries_name_the_query_and_the_finding():
    """An answer is only auditable if each piece of evidence says what was run."""
    evidence = DIAGNOSIS_SCHEMA["properties"]["evidence"]
    assert evidence["type"] == "array"
    assert set(evidence["items"]["properties"]) == {"query", "finding"}


def test_diagnosis_schema_confidence_is_a_closed_set_including_low():
    confidence = DIAGNOSIS_SCHEMA["properties"]["confidence"]
    assert confidence["enum"] == ["high", "medium", "low"]
    assert "confidence" in DIAGNOSIS_SCHEMA["required"]


# -- the prompt contract ---------------------------------------------------------


@pytest.mark.parametrize(
    "rule",
    [
        # Never assert a cause without a query result behind it.
        "Never state a cause you have not supported with a query result",
        "must name the query you ran and what it showed",
        # Disagreement is reported, not resolved by preference.
        "If metrics and logs disagree, say so rather than picking one",
        # A saved frame is not a correct frame.
        "A frame that completed is not necessarily correct",
        "defective deliverable, not a success",
        # Honest uncertainty beats a confident guess.
        'Report confidence honestly. "low" is a valid answer',
    ],
)
def test_instruction_carries_the_evidence_rules(rule):
    assert rule in INVESTIGATOR_INSTRUCTION


def test_instruction_carries_the_response_schema():
    """The model cannot fill a shape it was never shown."""
    assert json.dumps(DIAGNOSIS_SCHEMA, indent=2) in INVESTIGATOR_INSTRUCTION


def test_instruction_is_reachable_on_the_built_agent():
    agent = build_investigator(mcp_tools=["query_prometheus"])
    assert agent.instruction == INVESTIGATOR_INSTRUCTION


async def test_instruction_survives_adk_state_templating():
    """ADK substitutes ``{name}`` in a string instruction with session state.

    The embedded JSON schema is full of braces, so this is the one way the prompt could
    arrive at Gemini mangled - or not arrive at all, since an unrecognised variable
    raises ``KeyError`` on every turn. ADK leaves a brace group alone when its contents
    are not a valid state name, which the schema's are not, but that is a property of
    ADK's regex and of the exact schema text. Both can change, and neither change would
    show up anywhere else.
    """
    context = SimpleNamespace(
        _invocation_context=SimpleNamespace(
            artifact_service=None, session=SimpleNamespace(state={})
        )
    )
    rendered = await inject_session_state(INVESTIGATOR_INSTRUCTION, context)
    assert rendered == INVESTIGATOR_INSTRUCTION


# -- tool wiring -----------------------------------------------------------------


def test_investigator_exposes_grafana_tools():
    agent = build_investigator(mcp_tools=["query_prometheus", "query_loki_logs"])
    names = [getattr(t, "name", t) for t in agent.tools]
    assert "query_prometheus" in names


def test_every_declared_grafana_tool_resolves_to_a_wrapper_method():
    """The allow-list and the wrapper drift apart silently otherwise."""
    for name in GRAFANA_MCP_TOOLS:
        assert callable(getattr(GrafanaMCP, name, None)), name


def test_all_declared_tools_can_be_wired():
    agent = build_investigator(mcp_tools=sorted(GRAFANA_MCP_TOOLS))
    assert sorted(t.name for t in agent.tools) == sorted(GRAFANA_MCP_TOOLS)


def test_an_unknown_tool_name_fails_at_build_time():
    """A typo must break here, not against a live Grafana mid-render."""
    with pytest.raises(ValueError) as excinfo:
        build_investigator(mcp_tools=["query_prometheis"])
    assert "query_prometheis" in str(excinfo.value)


def test_a_wrapper_method_that_is_not_an_mcp_tool_is_rejected():
    """``available_tools`` is a real method on the wrapper and not a Grafana tool."""
    with pytest.raises(ValueError):
        build_investigator(mcp_tools=["available_tools"])


def test_prebuilt_tools_pass_through_untouched():
    """Production may hand in a real ADK tool or toolset instead of names."""
    prebuilt = build_investigator(mcp_tools=["query_loki_logs"]).tools[0]
    agent = build_investigator(mcp_tools=[prebuilt])
    assert agent.tools == [prebuilt]


def test_tool_declaration_uses_the_wrappers_own_parameter_names():
    """What the model sees is the wrapper's snake_case surface, not Grafana's JSON keys.

    The wrapper owns the camelCase translation and has tests pinning it. If the raw
    server spellings leaked into the declaration, that translation would be bypassed.
    """
    agent = build_investigator(mcp_tools=["query_prometheus"])
    declaration = agent.tools[0]._get_declaration()
    assert declaration.name == "query_prometheus"
    properties = declaration.parameters.properties
    assert "expr" in properties
    assert "datasource_uid" in properties
    assert "datasourceUid" not in properties


@pytest.mark.parametrize("name", sorted(GRAFANA_MCP_TOOLS))
def test_every_wired_tool_produces_a_function_declaration(name):
    """Declarations are built lazily, so a bad one fails at the first model call.

    ``create_incident`` takes ``list[dict[str, Any]]`` and several optional unions; if
    ADK's schema builder chokes on any wrapper signature, nothing else in this file
    notices, and the first symptom is a live agent that cannot start a turn.
    """
    agent = build_investigator(mcp_tools=[name])
    declaration = agent.tools[0]._get_declaration()
    assert declaration.name == name
    assert declaration.parameters.properties


def test_tool_description_comes_from_the_wrapper_docstring():
    agent = build_investigator(mcp_tools=["query_loki_logs"])
    assert agent.tools[0].description.startswith("Run a LogQL query.")


# -- tools actually reach Grafana ------------------------------------------------


async def test_calling_a_tool_routes_through_the_grafana_wrapper():
    session = _RecordingSession({"data": {"result": []}})
    grafana = GrafanaMCP(session, prometheus_uid="prom-uid")
    agent = build_investigator(mcp_tools=["query_prometheus"], grafana=grafana)

    result = await agent.tools[0].run_async(
        args={"expr": "render_frame_seconds"}, tool_context=None
    )

    assert result == {"data": {"result": []}}
    tool, args = session.calls[0]
    assert tool == "query_prometheus"
    # The wrapper's mapping did its job: camelCase keys, configured UID filled in.
    assert args["expr"] == "render_frame_seconds"
    assert args["datasourceUid"] == "prom-uid"


async def test_calling_a_tool_without_a_configured_client_says_so():
    """Better than an AttributeError on None halfway down the wrapper."""
    agent = build_investigator(mcp_tools=["query_prometheus"])
    with pytest.raises(GrafanaNotConfigured):
        await agent.tools[0].run_async(args={"expr": "up"}, tool_context=None)


# -- model -----------------------------------------------------------------------


def test_model_is_a_gemini_model():
    """Google Cloud AI only. A non-Gemini id here is a competition-rule violation."""
    assert INVESTIGATOR_MODEL.startswith("gemini-")
    assert build_investigator(mcp_tools=["query_prometheus"]).model == INVESTIGATOR_MODEL


def test_output_schema_is_not_set():
    """ADK disables every tool on an agent that declares an ``output_schema``.

    The investigator's whole job is querying Grafana, so the schema is carried in the
    instruction instead. Setting it would silently produce an agent that cannot read
    any telemetry.
    """
    assert build_investigator(mcp_tools=["query_prometheus"]).output_schema is None
