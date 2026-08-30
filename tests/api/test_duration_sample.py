"""Reconstruct a frame-duration sample from what Prometheus actually keeps.

The forecaster wants per-frame durations so it can measure spread and say how much to
trust its own ETA. Prometheus does not keep them: it keeps a histogram. And on a render
that finishes inside one OTLP export interval, every frame lands in the same export, so
no range query at any step will resolve them individually.

The tempting shortcut is to take `_sum / _count` and hand the forecaster that mean
repeated once per frame. It must not be done. Identical values have zero spread, and
zero spread is exactly what the forecaster reads as HIGH confidence, so a fabricated
sample would make the board's least-informed estimate look like its best one. That is
the failure this whole project argues against, committed by us.

The histogram buckets are the real distribution and are already being recorded. Turning
cumulative bucket counts back into a representative sample is a genuine derivation from
observed data: the spread it produces is the spread that actually occurred, coarsened to
the bucket width rather than invented.
"""

from dailies_api.duration_sample import sample_from_buckets


def test_one_frame_in_one_bucket_gives_one_observation():
    # le -> cumulative count, as Prometheus reports it
    sample = sample_from_buckets({1.0: 0, 5.0: 1, 15.0: 1, float("inf"): 1})
    assert len(sample) == 1
    assert 1.0 <= sample[0] <= 5.0


def test_counts_are_cumulative_not_per_bucket():
    """The classic misreading. le=5 holding 3 means three frames were AT MOST 5s."""
    sample = sample_from_buckets({1.0: 0, 5.0: 3, 15.0: 4, float("inf"): 4})
    assert len(sample) == 4, "four frames total, not 0+3+4+4"
    assert sum(1 for d in sample if d <= 5.0) == 3
    assert sum(1 for d in sample if 5.0 < d <= 15.0) == 1


def test_frames_spread_across_buckets_produce_real_spread():
    """The point of the exercise: a shot whose frames vary must not look steady."""
    steady = sample_from_buckets({5.0: 4, 15.0: 4, 60.0: 4, float("inf"): 4})
    varied = sample_from_buckets({5.0: 2, 15.0: 2, 60.0: 4, float("inf"): 4})

    def cv(xs):
        m = sum(xs) / len(xs)
        return (sum((x - m) ** 2 for x in xs) / len(xs)) ** 0.5 / m

    assert cv(steady) == 0.0, "all in one bucket really is steady, as far as we can tell"
    assert cv(varied) > 0.3, "two buckets apart must register as unsteady"


def test_an_empty_histogram_yields_nothing_rather_than_a_zero():
    assert sample_from_buckets({}) == []
    assert sample_from_buckets({1.0: 0, 5.0: 0, float("inf"): 0}) == []


def test_the_overflow_bucket_is_represented_by_its_lower_bound():
    """+Inf has no midpoint. Using the last finite boundary understates rather than
    inventing a number, and understating an ETA is the safer direction to be wrong."""
    sample = sample_from_buckets({1200.0: 0, 3600.0: 0, float("inf"): 1})
    assert sample == [3600.0]


def test_counts_that_go_backwards_are_tolerated_not_fatal():
    """A scrape race can hand back a non-monotonic histogram. The board must still draw."""
    sample = sample_from_buckets({1.0: 5, 5.0: 2, float("inf"): 5})
    assert len(sample) >= 1
    assert all(d > 0 for d in sample)
