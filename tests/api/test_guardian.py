"""Tests for the risk verdict.

Every case here is a pure function call: slack, work left, and how much the forecast that
produced them can be trusted. No clock, no store, no HTTP. What the tests pin is the
*shape* of the judgement rather than the exact thresholds, with two exceptions that are
policy rather than arithmetic and are asserted directly: a shot nobody can forecast is
never reported green, and a shot whose frame costs are erratic is not reported green
either.

The ratio cases matter more than they look. Ten minutes of slack is comfortable on a
five-minute shot and nearly gone on a five-hour one, so an absolute threshold would be
wrong on one of those two on every production.
"""

import math

import pytest
from dailies_api.guardian import assess
from dailies_api.state import Risk


def test_comfortable_slack_is_on_track():
    assert assess(slack_seconds=3000, remaining_seconds=600, confidence="high") is Risk.ON_TRACK


def test_past_the_deadline_with_work_left_is_missed():
    assert assess(slack_seconds=-100, remaining_seconds=600, confidence="high") is Risk.MISSED


def test_no_slack_left_but_not_yet_past_is_critical():
    assert assess(slack_seconds=30, remaining_seconds=600, confidence="high") is Risk.CRITICAL


def test_unknown_confidence_never_reports_on_track():
    """Absence of evidence is not evidence of safety."""
    assert assess(slack_seconds=99999, remaining_seconds=600, confidence="unknown") is Risk.WATCH


def test_thresholds_are_relative_to_remaining_work_not_absolute():
    """Ten minutes of slack means different things for a 5-minute and a 5-hour shot."""
    assert assess(slack_seconds=600, remaining_seconds=300, confidence="high") is Risk.ON_TRACK
    assert assess(slack_seconds=600, remaining_seconds=18000, confidence="high") in (
        Risk.AT_RISK,
        Risk.CRITICAL,
    )


def test_low_confidence_never_reports_on_track():
    """Erratic frame costs are evidence of trouble, not merely an absence of evidence."""
    assert assess(slack_seconds=99999, remaining_seconds=600, confidence="high") is Risk.ON_TRACK
    assert assess(slack_seconds=99999, remaining_seconds=600, confidence="low") is not Risk.ON_TRACK


@pytest.mark.parametrize("confidence", ["high", "medium", "low", "unknown"])
def test_more_slack_never_produces_a_redder_verdict(confidence):
    """Monotonicity, the invariant a supervisor reads the board by.

    A row that gets redder as the shot gets safer would teach a supervisor to distrust the
    colour, which costs more than any single wrong verdict.
    """
    severity = list(Risk)
    verdicts = [
        assess(slack_seconds=slack, remaining_seconds=600, confidence=confidence)
        for slack in (-1000, -1, 0, 30, 100, 300, 600, 1200, 99999)
    ]
    ranks = [severity.index(v) for v in verdicts]
    assert ranks == sorted(ranks, reverse=True), list(zip(ranks, verdicts))
    # Non-vacuity: a verdict that never moved would satisfy the ordering above while
    # telling us nothing, so the slack values must actually span several bands.
    assert len(set(verdicts)) >= 3, verdicts


def test_a_finished_shot_is_not_reported_as_missing():
    """MISSED means the deadline passed with the shot *unfinished* (see state.Risk).

    Zero work left is also the division that a ratio rule would fall over on, so this
    pins the answer rather than the traceback.
    """
    assert assess(slack_seconds=-100, remaining_seconds=0, confidence="high") is Risk.ON_TRACK


def test_corrupt_numbers_are_refused_rather_than_read_as_green():
    """A nan compares false against every threshold, so an unguarded ratio reports green."""
    assert assess(slack_seconds=math.nan, remaining_seconds=600, confidence="high") is Risk.WATCH
    assert assess(slack_seconds=600, remaining_seconds=math.nan, confidence="high") is Risk.WATCH
    assert assess(slack_seconds=math.inf, remaining_seconds=600, confidence="high") is Risk.WATCH
