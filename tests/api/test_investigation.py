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
    "problem_found": True,
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


# --- did it actually find a problem? -------------------------------------------------
#
# The board compares the telemetry verdict against the visual one and tells a supervisor
# whether the two agree. That comparison was wrong on the deployed system: it treated
# "a diagnosis exists" as "the telemetry found a problem", so a clean shot with a clean
# frame was announced, in yellow, as a DISAGREEMENT between sources.
#
# A diagnosis exists for every shot anyone asked about, including the healthy ones. The
# investigator has to say whether it found something, because nothing else in the answer
# distinguishes "no problem" from "a problem I am describing".


def test_the_schema_requires_the_investigator_to_say_whether_it_found_a_problem():
    from dailies_api.agent import DIAGNOSIS_SCHEMA

    assert "problem_found" in DIAGNOSIS_SCHEMA["required"]
    assert DIAGNOSIS_SCHEMA["properties"]["problem_found"]["type"] == "boolean"


def test_a_diagnosis_carries_the_flag_through():
    from dailies_api.investigation import parse_diagnosis

    answer = json.dumps(
        {
            "shot": "SH201",
            "problem_found": True,
            "cause": "a required asset was missing",
            "evidence": [{"query": "q", "finding": "f"}],
            "confidence": "high",
        }
    )
    assert parse_diagnosis(answer, "SH201")["problem_found"] is True


def test_a_clean_finding_is_carried_through_as_false():
    """The case that was being misreported, pinned."""
    from dailies_api.investigation import parse_diagnosis

    answer = json.dumps(
        {
            "shot": "SH200",
            "problem_found": False,
            "cause": "the render completed with no errors reported",
            "evidence": [{"query": "q", "finding": "f"}],
            "confidence": "high",
        }
    )
    assert parse_diagnosis(answer, "SH200")["problem_found"] is False


def test_an_answer_without_the_flag_is_refused():
    """Refused, not defaulted, and the consistency matters more than the convenience.

    Defaulting a missing flag to False would put a clean reading on an answer that never
    made one, which is the same class of invention the evidence rule exists to prevent.
    Defaulting it to True would redden a shot nobody said was broken. There is no honest
    default for "did you find a problem", so an answer that does not say is not one.

    My first version of this test asserted the flag should carry through as None, which
    contradicts how every other required field in this schema is treated.
    """
    from dailies_api.investigation import InvestigationFailed, parse_diagnosis

    answer = json.dumps(
        {
            "shot": "SH200",
            "cause": "something",
            "evidence": [{"query": "q", "finding": "f"}],
            "confidence": "high",
        }
    )
    with pytest.raises(InvestigationFailed):
        parse_diagnosis(answer, "SH200")
