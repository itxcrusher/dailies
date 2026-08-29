"""Tests for completion forecasting.

The forecaster's job is to answer "when will this shot finish" from a shot that is still
running, so every case here is a partial record: some frames observed, most not. What is
pinned below is the *direction* of the two judgement calls, not their arithmetic:

- A shot that got heavier must forecast from what it costs **now**, not from the cheap
  opening frames. The test asserts the estimate beats the flat mean; it deliberately does
  not assert a particular weighting.
- Confidence must fall out of how **steady** the observations are, not how many there
  are. Eight frames at a flat 10s is a better forecast than forty erratic ones.

No test reads a clock. ``now_epoch`` is an argument, so an ETA asserted today is the same
ETA next year.
"""

from dailies_graph.forecast import Forecast, estimate_completion


def test_estimates_from_observed_frame_durations():
    f = estimate_completion(
        frames_total=100,
        frames_done=20,
        observed_durations=[10.0] * 20,
        workers=1,
        now_epoch=1000,
    )
    assert f.eta_epoch == 1000 + 800  # 80 frames left at 10s each
    assert f.confidence == "high"


def test_parallelism_divides_the_remaining_work():
    f = estimate_completion(
        frames_total=100,
        frames_done=20,
        observed_durations=[10.0] * 20,
        workers=4,
        now_epoch=1000,
    )
    assert f.eta_epoch == 1000 + 200


def test_no_completed_frames_yet_returns_low_confidence_not_a_crash():
    """A cold start must still produce a usable answer, honestly labelled."""
    f = estimate_completion(
        frames_total=100,
        frames_done=0,
        observed_durations=[],
        workers=1,
        now_epoch=1000,
    )
    assert f.eta_epoch is None
    assert f.confidence == "unknown"


def test_recent_frames_weigh_more_than_early_ones():
    """A scene that got heavier must not be forecast from its cheap opening frames."""
    slow_recent = [1.0] * 10 + [10.0] * 10
    f = estimate_completion(
        frames_total=100,
        frames_done=20,
        observed_durations=slow_recent,
        workers=1,
        now_epoch=0,
    )
    naive_mean = sum(slow_recent) / len(slow_recent)  # 5.5
    assert f.seconds_per_frame > naive_mean


def test_wide_variance_lowers_confidence():
    erratic = [1.0, 30.0, 2.0, 45.0, 3.0, 60.0, 1.0, 50.0]
    steady = [10.0] * 8
    assert estimate_completion(100, 8, erratic, 1, 0).confidence != "high"
    assert estimate_completion(100, 8, steady, 1, 0).confidence == "high"


def test_a_single_observation_is_not_yet_evidence_of_a_steady_rate():
    """One frame has no spread, which is not the same as having a small one."""
    f = estimate_completion(100, 1, [10.0], 1, 0)
    assert f.eta_epoch == 990
    assert f.confidence != "high"


def test_no_workers_means_no_eta_rather_than_a_division_by_zero():
    """Nothing is rendering it, so nothing can be said about when it lands."""
    f = estimate_completion(100, 20, [10.0] * 20, 0, 1000)
    assert f.eta_epoch is None
    assert f.confidence == "unknown"
    assert f.seconds_per_frame == 10.0


def test_a_shot_with_no_frames_left_lands_now():
    """The last frame has landed; the ETA is this instant, not a division by zero."""
    f = estimate_completion(100, 100, [10.0] * 100, 4, 1000)
    assert f.eta_epoch == 1000


def test_a_part_second_estimate_rounds_up_rather_than_promising_a_frame_early():
    """7 frames at 3s over 2 workers is 10.5s; a board must not advertise 10."""
    f = estimate_completion(10, 3, [3.0, 3.0, 3.0], 2, 500)
    assert f.eta_epoch == 511


def test_an_unknown_forecast_never_carries_an_eta():
    """``unknown`` and ``eta_epoch is None`` are the same state seen from two sides."""
    for f in (
        estimate_completion(100, 0, [], 1, 0),
        estimate_completion(100, 20, [10.0] * 20, 0, 0),
    ):
        assert isinstance(f, Forecast)
        assert (f.confidence == "unknown") is (f.eta_epoch is None)


def test_a_nonsense_frame_duration_is_refused_rather_than_crashing_the_board():
    """nan slips past every ``<= 0`` check and then blows up in ``ceil``."""
    f = estimate_completion(100, 2, [10.0, float("nan")], 1, 0)
    assert f.eta_epoch is None
    assert f.seconds_per_frame is None
    assert f.confidence == "unknown"


def test_an_infinite_frame_duration_is_refused_the_same_way_nan_is():
    """The guard covers both non-finite values, not only the one that reaches ``ceil``."""
    f = estimate_completion(100, 2, [10.0, float("inf")], 1, 0)
    assert f.eta_epoch is None
    assert f.seconds_per_frame is None
    assert f.confidence == "unknown"


def test_a_negative_frame_duration_is_refused_rather_than_shortening_the_eta():
    """Corrupt telemetry must not buy a shot time it does not have.

    A frame cannot cost less than nothing, so a negative duration is the same class of
    nonsense as a nan: a clock skew, a parser reading ``end - start`` backwards, a
    sentinel that escaped. It is more dangerous than the nan, because it does not crash.
    One -50s sample among nineteen honest 10s frames drags the weighted rate to ~2.2s and
    reports an ETA four times too early, which is the direction that gets a board trusted
    right up to the moment the shot misses.
    """
    f = estimate_completion(100, 20, [10.0] * 19 + [-50.0], 1, 0)
    assert f.eta_epoch is None
    assert f.seconds_per_frame is None
    assert f.confidence == "unknown"


def test_a_finished_shot_lands_now_even_after_the_farm_released_its_workers():
    """Zero workers is the *normal* state of a delivered shot, not an unknown one.

    Whether anything is still rendering cannot change the answer once there is nothing
    left to render, so this must agree with the same shot seen while it still held four
    workers.
    """
    drained = estimate_completion(100, 100, [10.0] * 100, 0, 1000)
    still_held = estimate_completion(100, 100, [10.0] * 100, 4, 1000)
    assert drained.eta_epoch == 1000
    assert drained.confidence == still_held.confidence != "unknown"
    assert drained.remaining_seconds == 0.0


def test_a_frames_done_overrun_is_treated_as_a_finished_shot():
    """Live telemetry double-counts a retried frame; that is not 20 frames of new work."""
    f = estimate_completion(100, 120, [10.0] * 20, 4, 1000)
    assert f.eta_epoch == 1000
    assert f.remaining_seconds == 0.0


def test_a_shot_with_no_frames_at_all_lands_now():
    """An empty range has nothing to wait for, and the observed rate still reports."""
    f = estimate_completion(0, 0, [10.0] * 5, 1, 1000)
    assert f.eta_epoch == 1000
    assert f.seconds_per_frame == 10.0


def test_a_negative_workers_count_is_refused_like_a_zero_one():
    """Below zero is no more renderable than zero, and must not flip the ETA's sign."""
    f = estimate_completion(100, 20, [10.0] * 20, -1, 1000)
    assert f.eta_epoch is None
    assert f.confidence == "unknown"


def test_a_negative_frames_done_does_not_invent_work_that_does_not_exist():
    """``state.Shot`` pins both counts at ``ge=0``; a wiring bug must not inflate the ETA.

    Forecasting 110 frames of work on a 100-frame shot is the same overstatement in the
    other direction, and it would push a healthy shot onto the board as at risk.
    """
    f = estimate_completion(100, -10, [10.0] * 5, 1, 0)
    assert f.eta_epoch == 1000


def test_the_forecast_reports_the_remaining_work_it_already_computed():
    """Task 15's ``assess`` and ``ShotNode.estimated_seconds`` both need this number.

    Re-deriving it as ``eta_epoch - now_epoch`` would make every caller carry ``now_epoch``
    alongside the forecast and inherit the ETA's rounding second-hand.
    """
    f = estimate_completion(100, 20, [10.0] * 20, 4, 1000)
    assert f.remaining_seconds == 200.0
    assert f.eta_epoch == 1000 + 200


def test_an_unforecastable_shot_reports_no_remaining_work_either():
    """``remaining_seconds`` is a projection like the ETA, so it goes when the ETA goes."""
    for f in (
        estimate_completion(100, 0, [], 1, 0),
        estimate_completion(100, 20, [10.0] * 20, 0, 0),
        estimate_completion(100, 2, [10.0, float("nan")], 1, 0),
    ):
        assert f.remaining_seconds is None


def test_an_arithmetic_overflow_greys_the_cell_out_rather_than_raising():
    """``Never raises`` has to hold for finite-but-enormous inputs too.

    ``frames_left * seconds_per_frame`` can overflow to infinity from values that each
    pass the finite check, and ``math.ceil`` raises ``OverflowError`` on that, which is
    the same board-down failure the nan guard exists to prevent.
    """
    f = estimate_completion(10**18, 0, [1e308] * 3, 1, 0)
    assert f.eta_epoch is None
    assert f.remaining_seconds is None
    assert f.seconds_per_frame == 1e308
    assert f.confidence == "unknown"
