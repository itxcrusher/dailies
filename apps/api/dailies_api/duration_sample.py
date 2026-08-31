"""Turn a Prometheus histogram back into a frame-duration sample.

The forecaster in :mod:`dailies_graph.forecast` wants the seconds each finished frame
took, because the *spread* of those numbers is how it decides whether to trust its own
ETA. Prometheus does not keep them. It keeps a histogram, and on a render that finishes
inside a single OTLP export interval every frame lands in the same export, so no range
query at any step resolves them individually.

There is an obvious shortcut here and it is a trap. ``_sum / _count`` gives the mean, and
handing the forecaster that mean repeated once per frame produces a sample with **zero
spread**, which is precisely what it reads as *high* confidence. The board's least
informed estimate would then wear its most confident badge. That is the exact failure
this project exists to argue against, so it is not available to us.

The buckets are the real distribution and are already recorded. Reconstructing a
representative sample from them is a genuine derivation: the spread that comes out is the
spread that actually happened, coarsened to the bucket width rather than invented. A shot
whose frames all landed in one bucket really does look steady *as far as the data can
say*, and one straddling two buckets registers as unsteady, which is the distinction the
confidence rating exists to make.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

__all__ = ["sample_from_buckets"]


def sample_from_buckets(buckets: Mapping[float, float]) -> list[float]:
    """Representative frame durations from cumulative histogram bucket counts.

    Args:
        buckets: Prometheus ``le`` upper bound to **cumulative** count, as
            ``render_frame_duration_seconds_bucket`` reports it. ``float("inf")`` is the
            overflow bucket. Cumulative is the part most often misread: ``le=5`` holding
            three means three frames took *at most* five seconds, not that three frames
            were between the previous boundary and five.

    Returns:
        One value per observed frame, ascending. Each is placed at the midpoint of the
        bucket it fell in, which is the least-wrong single number for a frame known only
        to lie between two boundaries.

        The overflow bucket is represented by the last finite boundary rather than a
        midpoint, because ``+Inf`` has none. That understates those frames, and
        understating is the safer direction: an ETA that arrives early embarrasses the
        board, one that arrives late has already cost someone the deadline it promised.

    Never raises. A non-monotonic histogram is a real thing a scrape race can produce,
    and the board has to draw a row either way, so a negative step is read as zero rather
    than as a reason to fail.
    """
    if not buckets:
        return []

    bounds = sorted(buckets)
    sample: list[float] = []
    previous_bound = 0.0
    previous_count = 0.0

    for bound in bounds:
        cumulative = buckets[bound]
        # max(..., 0): a cumulative series that goes backwards is a scrape artefact, not
        # a negative number of frames. Clamping keeps one bad bucket from subtracting
        # frames that other buckets legitimately counted.
        in_bucket = max(cumulative - previous_count, 0.0)
        previous_count = max(cumulative, previous_count)

        if in_bucket > 0:
            if math.isinf(bound):
                # No midpoint exists. The lower edge is the only honest floor.
                representative = previous_bound
            else:
                representative = (previous_bound + bound) / 2.0
            if representative > 0:
                sample.extend([representative] * round(in_bucket))

        if not math.isinf(bound):
            previous_bound = bound

    return sample
