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
