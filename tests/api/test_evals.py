"""The scoring, and the replay that feeds it.

Graded separately from the model. A harness whose scoring is wrong reports a number with
the same confidence as one whose scoring is right, and the number is the entire product
here: it is what turns "the agent works" into something a reader can check.
"""

import json

import pytest
from dailies_api.evals.harness import ReplaySession, grade
from dailies_api.evals.scenarios import SCENARIOS, Scenario

FOUND = {
    "shot": "SH201",
    "problem_found": True,
    "cause": "A required asset was missing: /assets/jacket_diffuse.exr",
    "evidence": [{"query": "q", "finding": "Unable to open file"}],
    "confidence": "high",
}


def scenario_named(name: str) -> Scenario:
    return next(s for s in SCENARIOS if s.name == name)


class TestScoring:
    def test_a_correct_finding_passes_every_check(self):
        assert grade(scenario_named("missing_texture"), FOUND).passed

    def test_the_wrong_verdict_fails(self):
        wrong = {**FOUND, "problem_found": False}
        assert grade(scenario_named("missing_texture"), wrong).detected is False

    def test_a_right_verdict_for_an_unrelated_reason_fails_on_the_cause(self):
        """Detection alone is half a mark. An agent that says "something is wrong" and
        names the wrong thing has not done the job a supervisor needs."""
        vague = {**FOUND, "cause": "The render was unhealthy."}
        result = grade(scenario_named("missing_texture"), vague)
        assert result.detected is True
        assert result.cause_ok is False
        assert not result.passed

    def test_an_answer_with_no_evidence_fails_even_when_it_is_right(self):
        """The schema refuses this upstream; the eval refuses it here too, so a
        regression that loosened the schema would show as a score rather than as
        nothing at all."""
        assert not grade(scenario_named("missing_texture"), {**FOUND, "evidence": []}).passed

    def test_evidence_missing_a_query_is_not_evidence(self):
        half = {**FOUND, "evidence": [{"finding": "something happened"}]}
        assert grade(scenario_named("missing_texture"), half).evidence_ok is False

    def test_a_clean_render_must_still_show_its_working(self):
        """'Nothing is wrong' is a claim like any other."""
        clean = {"problem_found": False, "cause": "No failures recorded.", "evidence": []}
        assert grade(scenario_named("clean_render"), clean).evidence_ok is False


class TestTheAntiFabricationCase:
    def test_naming_a_file_it_was_never_shown_fails(self):
        """The case the harness exists for. Every query returned empty, so a cause that
        names jacket_diffuse.exr was invented whole, and it is the exact failure this
        product claims to catch in other people's pipelines."""
        invented = {
            "problem_found": True,
            "cause": "The asset /assets/jacket_diffuse.exr could not be opened.",
            "evidence": [{"query": "q", "finding": "f"}],
        }
        result = grade(scenario_named("no_telemetry"), invented)
        assert result.no_fabrication is False
        assert not result.passed

    @pytest.mark.parametrize(
        "cause",
        [
            "No telemetry was returned for this shot, so nothing can be concluded.",
            "The metrics were not available; this shot could not be read.",
        ],
    )
    def test_saying_it_could_not_read_the_shot_passes(self, cause):
        """The honest answer to no data is that there was no data. The verdict is not
        graded here, because "nothing is wrong that I can see" and "I cannot tell" are
        both defensible; what is graded is whether it admits what it had."""
        honest = {
            "problem_found": False,
            "cause": cause,
            "evidence": [{"query": "q", "finding": "empty"}],
        }
        assert grade(scenario_named("no_telemetry"), honest).passed

    def test_claiming_the_render_was_fine_from_no_data_fails(self):
        """The subtle failure, and the one the first real run produced.

        Given nothing at all the agent answered "the render for SH999 completed
        successfully without any logged errors". It invented no fault and still asserted
        an outcome from an empty result, which is the same error facing the other way.
        It is the more dangerous direction: a supervisor told a broken shot is fine stops
        looking, while one told about a fault that is not there merely wastes a minute.
        """
        overclaim = {
            "problem_found": False,
            "cause": "The render completed successfully without any logged errors.",
            "evidence": [{"query": "q", "finding": "no series returned"}],
        }
        assert grade(scenario_named("no_telemetry"), overclaim).cause_ok is False


class TestTheReplaySession:
    @pytest.mark.asyncio
    async def test_the_two_prometheus_calls_get_different_answers(self):
        """The stall scenario is the gap between expected and completed. A session keyed
        on the tool name alone would answer both with the same payload and the gap would
        vanish, which would make the scenario unfailable."""
        session = ReplaySession(scenario_named("stalled_no_errors"))
        expected = await session.call_tool(
            "query_prometheus", {"expr": "render_job_frames_expected"}
        )
        completed = await session.call_tool(
            "query_prometheus", {"expr": "render_job_frames_completed_total"}
        )
        first = json.loads(expected.content[0].text)["data"]["result"][0]["values"][0][1]
        second = json.loads(completed.content[0].text)["data"]["result"][0]["values"][0][1]
        assert (first, second) == ("40", "17")

    @pytest.mark.asyncio
    async def test_a_clean_shot_replays_genuinely_empty_logs(self):
        """Captured, not composed: SH200 logged nothing, and that silence is the thing
        the clean scenario tests."""
        session = ReplaySession(scenario_named("clean_render"))
        logs = await session.call_tool("query_loki_logs", {"logql": "{}"})
        assert json.loads(logs.content[0].text)["data"]["result"] == []

    @pytest.mark.asyncio
    async def test_an_unexpected_tool_answers_empty_rather_than_raising(self):
        session = ReplaySession(scenario_named("clean_render"))
        result = await session.call_tool("create_annotation", {})
        assert json.loads(result.content[0].text)["data"]["result"] == []


def test_every_scenario_explains_why_it_exists():
    """A case nobody can justify is a case nobody will maintain, and one that starts
    failing gets deleted rather than investigated."""
    for scenario in SCENARIOS:
        assert len(scenario.why) > 40, scenario.name
