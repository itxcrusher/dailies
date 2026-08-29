"""Tests for the diagnose pipeline: MCP session in, checked diagnosis out.

The model call and the MCP connection are both injected, so nothing here reaches Gemini
or Grafana. What is tested is the wiring between them and the check on the way out: a
diagnosis that does not satisfy the schema the investigator was told to answer in must
not reach the board, because an unchecked diagnosis is the one failure mode this project
exists to argue against.
"""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from dailies_api.investigation import (
    DEFAULT_TOOLS,
    InvestigationFailed,
    build_diagnoser,
    parse_diagnosis,
)

GOOD = {
    "shot": "SH030",
    "cause": "Blender could not open jacket_diffuse.exr, so the frame rendered untextured.",
    "evidence": [
        {
            "query": '{service_name="dailies-render", shot="SH030"} |= "Unable to open"',
            "finding": "WARN asset_missing on frames 40-52 from a job that exited 0",
        }
    ],
    "confidence": "high",
}


class FakeSession:
    """The MCPSession protocol, with no network behind it."""

    def __init__(self):
        self.calls = []
        self.closed = False

    async def list_tools(self):
        return SimpleNamespace(tools=[SimpleNamespace(name=name) for name in DEFAULT_TOOLS])

    async def call_tool(self, name, args, /):
        self.calls.append((name, args))
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text='{"data":{"result":[]}}')], isError=False
        )


def diagnoser(session, answer=None, *, tools=("query_prometheus",), record=None):
    """A diagnoser wired to ``session`` with the model call replaced."""
    seen = {} if record is None else record

    @asynccontextmanager
    async def factory(url):
        seen["url"] = url
        try:
            yield session
        finally:
            session.closed = True

    async def run(agent, prompt):
        seen["prompt"] = prompt
        seen["agent"] = agent
        # Drive one real tool through the agent, so this proves the injected session is
        # actually reachable from the investigator rather than merely constructed.
        seen["tool_result"] = await agent.tools[0].run_async(
            args={"expr": "render_frame_duration_seconds_count"}, tool_context=None
        )
        return json.dumps(GOOD) if answer is None else answer

    return build_diagnoser(
        mcp_url="https://mcp.example.invalid",
        prometheus_uid="grafanacloud-prom",
        loki_uid="grafanacloud-logs",
        tools=tools,
        session_factory=factory,
        run=run,
    )


# -- the pipeline ----------------------------------------------------------------


async def test_the_investigator_reads_grafana_through_the_injected_session():
    session = FakeSession()
    seen = {}
    diagnosis = await diagnoser(session, record=seen)("SH030")

    assert diagnosis == GOOD
    tool, args = session.calls[0]
    assert tool == "query_prometheus"
    assert args["datasourceUid"] == "grafanacloud-prom"


async def test_the_prompt_names_the_shot_under_investigation():
    seen = {}
    await diagnoser(FakeSession(), record=seen)("SH030")
    assert "SH030" in seen["prompt"]
    assert seen["url"] == "https://mcp.example.invalid"


async def test_the_session_is_closed_even_when_the_model_fails():
    session = FakeSession()

    @asynccontextmanager
    async def factory(url):
        try:
            yield session
        finally:
            session.closed = True

    async def run(agent, prompt):
        raise RuntimeError("Vertex said no")

    diagnose = build_diagnoser(
        mcp_url="https://mcp.example.invalid",
        prometheus_uid="prom",
        loki_uid="loki",
        session_factory=factory,
        run=run,
    )
    with pytest.raises(RuntimeError):
        await diagnose("SH030")
    assert session.closed is True


async def test_the_default_tool_set_is_read_only_grafana_access():
    """A diagnose call must never mutate the stack it is reading."""
    assert set(DEFAULT_TOOLS) == {
        "query_prometheus",
        "list_prometheus_metric_names",
        "query_loki_logs",
    }


# -- the check on the way out ----------------------------------------------------


def test_a_fenced_json_answer_is_parsed():
    fenced = f"```json\n{json.dumps(GOOD)}\n```"
    assert parse_diagnosis(fenced, "SH030") == GOOD


def test_prose_around_the_object_is_not_guessed_at():
    with pytest.raises(InvestigationFailed, match="JSON"):
        parse_diagnosis("I could not reach Grafana, sorry.", "SH030")


def test_a_diagnosis_missing_its_evidence_is_refused():
    answer = json.dumps({"shot": "SH030", "cause": "memory", "confidence": "high"})
    with pytest.raises(InvestigationFailed, match="evidence"):
        parse_diagnosis(answer, "SH030")


def test_an_empty_evidence_list_is_refused():
    answer = json.dumps({**GOOD, "evidence": []})
    with pytest.raises(InvestigationFailed, match="evidence"):
        parse_diagnosis(answer, "SH030")


def test_evidence_without_the_query_behind_it_is_refused():
    answer = json.dumps({**GOOD, "evidence": [{"finding": "frames 40-52 look wrong"}]})
    with pytest.raises(InvestigationFailed, match="query"):
        parse_diagnosis(answer, "SH030")


def test_a_bad_answer_carries_what_the_model_actually_said():
    with pytest.raises(InvestigationFailed) as raised:
        parse_diagnosis("not json at all", "SH030")
    assert "not json at all" in str(raised.value)


async def test_a_bad_answer_fails_the_diagnosis_rather_than_storing_it():
    with pytest.raises(InvestigationFailed):
        await diagnoser(FakeSession(), answer="no idea")("SH030")
