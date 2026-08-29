"""Tests for the risk verdict.

Every case here is a pure function call: slack, work left, and how much the forecast that
produced them can be trusted. No clock, no store, no HTTP. What the tests pin is the
*shape* of the judgement rather than the exact thresholds, with two exceptions. The band
edges are pinned exactly, because they are a published contract that an innocent-looking
edit to a comparison operator would move. And the policies are asserted directly: a shot
nobody can forecast is never reported green, a shot whose frame costs are unsteady is not
reported green either, and a shot that has landed is never painted a delivery colour.

The last block composes ``estimate_completion`` into ``assess`` rather than hand-writing
the numbers, because the interesting disagreements between the two modules are invisible
to a suite that only ever feeds the guardian values a human chose.

The ratio cases matter more than they look. Ten minutes of slack is comfortable on a
five-minute shot and nearly gone on a five-hour one, so an absolute threshold would be
wrong on one of those two on every production.
"""

import math

import pytest
from dailies_api.guardian import assess
from dailies_api.state import Risk
from dailies_graph.forecast import estimate_completion


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
    """An ETA the system cannot stand behind does not get to paint a row green.

    ``low`` floors at WATCH and no harder, because ``forecast`` reports it both for a
    record that is erratic and for one that is merely thin. Escalating on the pair would
    escalate on every shot's first two frames; see the composition tests below.
    """
    assert assess(slack_seconds=99999, remaining_seconds=600, confidence="high") is Risk.ON_TRACK
    assert assess(slack_seconds=99999, remaining_seconds=600, confidence="low") is Risk.WATCH


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


def test_corrupt_numbers_are_refused_rather_than_read_as_green():
    """A nan compares false against every threshold, so an unguarded ratio reports green."""
    assert assess(slack_seconds=math.nan, remaining_seconds=600, confidence="high") is Risk.WATCH
    assert assess(slack_seconds=600, remaining_seconds=math.nan, confidence="high") is Risk.WATCH
    assert assess(slack_seconds=math.inf, remaining_seconds=600, confidence="high") is Risk.WATCH


def test_negative_infinite_slack_is_past_the_deadline_not_unreadable():
    """-inf is not ambiguous the way nan is; it is unambiguously past the deadline.

    Refusing it alongside nan would report the greener of the two possible errors on the
    most severe input the function can be handed.
    """
    assert assess(slack_seconds=-math.inf, remaining_seconds=600, confidence="high") is Risk.MISSED


def test_no_work_estimate_is_no_evidence_rather_than_a_traceback():
    """``Forecast.remaining_seconds`` is ``float | None``, and None is the ordinary state.

    A shot that has not rendered a frame yet, has no workers, or carries a corrupt
    duration in its record has no work estimate at all. The docstring promises this
    function never raises, so None takes the no-evidence floor like any other
    unreadable number.
    """
    assert assess(slack_seconds=99999, remaining_seconds=None, confidence="unknown") is Risk.WATCH
    assert assess(slack_seconds=99999, remaining_seconds=None, confidence="high") is Risk.WATCH


@pytest.mark.parametrize("confidence", ["high", "medium", "low", "unknown"])
def test_a_finished_shot_is_never_painted_with_a_delivery_colour(confidence):
    """MISSED means the deadline passed with the shot *unfinished* (see state.Risk).

    A landed shot is on track at every confidence, including the floored ones, and zero
    work left is also the division a ratio rule would fall over on, so this pins the
    answer rather than the traceback.

    ``forecast`` gives a finished shot a real confidence rather than ``unknown`` precisely
    so delivered work does not read as not-on-track. A floor applied after the finished
    check would defeat that for any shot whose spread band happens to land on ``low``.
    """
    assert assess(slack_seconds=-100, remaining_seconds=0, confidence=confidence) is Risk.ON_TRACK


@pytest.mark.parametrize(
    ("ratio", "expected"),
    [
        (0.09, Risk.CRITICAL),
        (0.10, Risk.AT_RISK),
        (0.24, Risk.AT_RISK),
        (0.25, Risk.WATCH),
        (0.99, Risk.WATCH),
        (1.00, Risk.ON_TRACK),
    ],
)
def test_the_band_edges_are_pinned(ratio, expected):
    """The table in the module docstring is written as strict ``<``; this is that table.

    Without these, flipping the comparisons to ``<=`` would shift every verdict at an edge
    by one band and no test would notice.
    """
    remaining = 1000.0
    assert (
        assess(slack_seconds=ratio * remaining, remaining_seconds=remaining, confidence="high")
        is expected
    )


def test_a_second_verdict_source_can_only_redden_the_row():
    """The seam Visual QA plugs into: a shot can be finished *and* broken.

    Delivery arithmetic alone would report a shot that has rendered every frame as
    ON_TRACK, which is the wrong answer for one whose output is rejected.
    """
    assert (
        assess(
            slack_seconds=-100,
            remaining_seconds=0,
            confidence="high",
            failure_risk=Risk.CRITICAL,
        )
        is Risk.CRITICAL
    )
    # Never the other way: a second opinion floors the verdict, it does not replace it.
    assert (
        assess(
            slack_seconds=-100,
            remaining_seconds=600,
            confidence="high",
            failure_risk=Risk.WATCH,
        )
        is Risk.MISSED
    )


# --------------------------------------------------------------------------------------
# The forecast -> guardian seam.
#
# Every case above hands ``assess`` numbers a human chose. These hand it the numbers a
# forecast actually produces, which is where the interesting disagreements live: the
# guardian's docstring cites forecast's contract in four places, so the coupling is real
# and belongs under test rather than under commentary.
# --------------------------------------------------------------------------------------

_TWELVE_HOURS = 12 * 60 * 60


def _verdict_for(frames_total, frames_done, durations, workers, slack_seconds):
    """Forecast a shot, then rate it, passing the forecast straight through."""
    forecast = estimate_completion(
        frames_total=frames_total,
        frames_done=frames_done,
        observed_durations=durations,
        workers=workers,
        now_epoch=1_700_000_000,
    )
    return assess(
        slack_seconds=slack_seconds,
        remaining_seconds=forecast.remaining_seconds,
        confidence=forecast.confidence,
    )


def test_a_shot_that_has_not_started_is_watched_not_alarmed():
    """No frames observed is an absence of evidence, and absence is not trouble."""
    assert (
        _verdict_for(
            frames_total=60, frames_done=0, durations=[], workers=4, slack_seconds=_TWELVE_HOURS
        )
        is Risk.WATCH
    )


def test_a_shot_one_frame_in_with_a_day_of_room_is_not_escalated():
    """The regression this pair of modules is most likely to produce.

    ``forecast`` reports ``low`` for fewer than three observations as well as for erratic
    ones, so a floor that treats ``low`` as a symptom escalates every shot on the board
    for its first two frames. Ten minutes of work against twelve hours of slack is not a
    shot anyone should be paged about.
    """
    verdict = _verdict_for(
        frames_total=61, frames_done=1, durations=[10.0], workers=1, slack_seconds=_TWELVE_HOURS
    )
    assert verdict is Risk.WATCH, verdict


def test_a_delivered_shot_reads_as_delivered_however_its_spread_landed():
    """frames_done == frames_total gives remaining_seconds 0.0 and a real confidence."""
    forecast = estimate_completion(
        frames_total=2,
        frames_done=2,
        observed_durations=[10.0, 30.0],
        workers=1,
        now_epoch=1_700_000_000,
    )
    assert forecast.remaining_seconds == 0.0
    assert forecast.confidence == "low"
    assert (
        assess(
            slack_seconds=-1000,
            remaining_seconds=forecast.remaining_seconds,
            confidence=forecast.confidence,
        )
        is Risk.ON_TRACK
    )


def test_a_corrupt_duration_greys_the_row_rather_than_taking_the_board_down():
    """forecast refuses the record and reports None; the guardian must survive that."""
    assert (
        _verdict_for(
            frames_total=60,
            frames_done=10,
            durations=[10.0, math.nan, 12.0],
            workers=4,
            slack_seconds=_TWELVE_HOURS,
        )
        is Risk.WATCH
    )
