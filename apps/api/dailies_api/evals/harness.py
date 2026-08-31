"""Running the investigator against known telemetry and scoring what comes back.

Replaces "the agent works" with a number. Everything else in this project refuses a claim
with nothing behind it, and until now its own central capability was asserted from three
anecdotes on a board.

**Only the network is faked.** The session below replays captured responses; above it sit
the real ``GrafanaMCP`` wrapper, the real tool routing and argument marshalling, the real
prompt, a real Gemini call and the real schema validation. A harness that stubbed the
model would measure the plumbing and call it accuracy.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from .scenarios import SCENARIOS, Scenario

__all__ = ["Grade", "ReplaySession", "grade", "main", "run_scenario"]

#: Metric names the agent sees when it asks what exists. Real names from the live stack:
#: an agent told a metric exists that does not would be scored on a query it could never
#: have written against the farm.
_METRIC_NAMES = [
    "render_job_frames_expected",
    "render_job_frames_completed_total",
    "render_job_deadline_epoch_seconds",
    "render_frame_duration_seconds_bucket",
    "render_worker_memory_bytes",
]


class _Block:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _Result:
    def __init__(self, content: list[_Block], is_error: bool = False) -> None:
        self.content = content
        self.isError = is_error


class ReplaySession:
    """An MCP session that answers from a scenario instead of from Grafana.

    Routing is on the query itself, not on a tool name, because the agent calls
    ``query_prometheus`` more than once per investigation and the difference that matters
    is which metric it asked for. A session keyed only by tool name would hand the same
    answer to "how many frames were expected" and "how many completed", and the stall
    scenario is precisely the gap between those two.
    """

    def __init__(self, scenario: Scenario) -> None:
        self.scenario = scenario
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def list_tools(self) -> Any:
        names = ["query_prometheus", "list_prometheus_metric_names", "query_loki_logs"]
        return SimpleNamespace(tools=[SimpleNamespace(name=name) for name in names])

    async def call_tool(self, name: str, args: dict[str, Any], /) -> Any:
        self.calls.append((name, args))
        if name == "list_prometheus_metric_names":
            return _Result([_Block(json.dumps(_METRIC_NAMES))])
        if name == "query_loki_logs":
            return _Result([_Block(json.dumps(self.scenario.logs))])
        if name == "query_prometheus":
            expression = str(args.get("expr", ""))
            completed = "completed" in expression
            payload = self.scenario.prom_completed if completed else self.scenario.prom_expected
            return _Result([_Block(json.dumps(payload))])
        # Anything else answers empty rather than raising. An unexpected tool call is a
        # fact about the run worth seeing in the transcript, not a crash that hides the
        # three calls that came before it.
        return _Result([_Block('{"status":"success","data":{"result":[]}}')])


@dataclass
class Grade:
    """How one scenario scored."""

    scenario: str
    detected: bool | None
    cause_ok: bool | None
    evidence_ok: bool
    no_fabrication: bool | None
    cause: str
    queries: int
    error: str | None = None

    @property
    def passed(self) -> bool:
        checks = [self.detected, self.cause_ok, self.no_fabrication]
        return self.error is None and self.evidence_ok and all(c is not False for c in checks)


def grade(scenario: Scenario, diagnosis: dict[str, Any]) -> Grade:
    """Score one diagnosis. Pure, so the scoring can be tested without a model."""
    cause = str(diagnosis.get("cause") or "")
    lowered = cause.lower()
    evidence = diagnosis.get("evidence") or []

    detected: bool | None = None
    if scenario.expect_problem is not None:
        detected = diagnosis.get("problem_found") is scenario.expect_problem

    cause_ok: bool | None = None
    if scenario.cause_must_mention:
        cause_ok = any(word.lower() in lowered for word in scenario.cause_must_mention)

    no_fabrication: bool | None = None
    if scenario.cause_must_not_mention:
        no_fabrication = not any(
            word.lower() in lowered for word in scenario.cause_must_not_mention
        )

    # Evidence is graded on every scenario, including the clean one. "Nothing is wrong"
    # is a claim like any other, and this project's schema exists because a conclusion
    # nobody can check is not a conclusion.
    evidence_ok = bool(evidence) and all(
        isinstance(item, dict) and item.get("query") and item.get("finding") for item in evidence
    )

    return Grade(
        scenario=scenario.name,
        detected=detected,
        cause_ok=cause_ok,
        evidence_ok=evidence_ok,
        no_fabrication=no_fabrication,
        cause=cause,
        queries=len(evidence),
    )


async def run_scenario(scenario: Scenario, *, model: str | None = None) -> Grade:
    """Investigate one scenario with a real model over a replayed session."""
    from ..investigation import build_diagnoser

    session = ReplaySession(scenario)

    @asynccontextmanager
    async def session_factory(_url: str):
        yield session

    kwargs: dict[str, Any] = {}
    if model:
        kwargs["model"] = model
    try:
        diagnose = build_diagnoser(
            mcp_url="replay://evals",
            prometheus_uid="grafanacloud-prom",
            loki_uid="grafanacloud-logs",
            session_factory=session_factory,
            **kwargs,
        )
        diagnosis = await diagnose(scenario.shot_id)
    except Exception as exc:  # noqa: BLE001 - a failed run is a result, not a crash
        return Grade(
            scenario.name, None, None, False, None, "", 0, error=f"{type(exc).__name__}: {exc}"
        )
    return grade(scenario, diagnosis)


def _line(grade_: Grade) -> str:
    def mark(value: bool | None) -> str:
        return "  -  " if value is None else (" pass" if value else " FAIL")

    return (
        f"  {grade_.scenario:<20}"
        f"{mark(grade_.detected)}{mark(grade_.cause_ok)}"
        f"{mark(grade_.evidence_ok)}{mark(grade_.no_fabrication)}"
        f"   {grade_.queries:>2} queries"
    )


async def _run(model: str | None) -> int:
    print("  scenario            verdict cause evidence honest  evidence")
    grades = []
    for scenario in SCENARIOS:
        result = await run_scenario(scenario, model=model)
        grades.append(result)
        print(_line(result))
        if result.error:
            print(f"      error: {result.error}")
        elif result.cause:
            print(f"      cause: {result.cause[:110]}")

    passed = sum(1 for g in grades if g.passed)
    print(f"\n  {passed}/{len(grades)} scenarios passed")
    # A single run of a language model is a sample, not a measurement. The number above is
    # reported as what happened on this run, which is the honest form of it.
    print("  (one run; a model is sampled, not measured, so re-run before quoting a rate)")
    return 0 if passed == len(grades) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score the investigator against known telemetry.")
    parser.add_argument("--model", default=None, help="Gemini model id; defaults to the agent's.")
    args = parser.parse_args(argv)
    return asyncio.run(_run(args.model))


if __name__ == "__main__":
    sys.exit(main())
