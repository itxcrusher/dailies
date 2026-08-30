"""Rate one shot against its deadline, from what telemetry observed.

The three pieces this composes already existed and were already tested, and none of them
was ever called. :mod:`dailies_graph.forecast` projects a completion time from observed
frame costs, :func:`dailies_api.guardian.assess` turns slack and remaining work into a
verdict, and :class:`dailies_api.state.Shot` has carried a ``risk`` field since the
beginning. Because nothing joined them, every row on the deployed board showed the model
default, ON_TRACK, including a shot with a missing asset and a shot with no frames done.

A column that says the same thing about every row is worse than no column. It asserts a
verdict nobody computed, inside a product whose whole argument is that a green signal has
to be earned. This module is the join.

**Slack is computed against the shot itself, not a dependency chain.**
:func:`dailies_graph.model.slack_seconds` can walk a production graph and account for
everything downstream waiting on this shot, which is the richer answer. It needs a
production definition: which shots exist, what depends on what, and when each is due.
Telemetry carries a deadline per render and nothing about ordering, so the honest
calculation available here is the single-shot one. That is a real limitation and is
recorded rather than papered over: a shot can be comfortable on its own date while a
sequence waiting behind it is not.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from dailies_graph.forecast import estimate_completion

from .guardian import assess
from .state import Shot

__all__ = ["DEFAULT_WORKERS", "rate"]

#: Frames the farm is assumed to render at once for a shot, when telemetry cannot say.
#:
#: One, deliberately, because it is the assumption that cannot flatter a shot. Remaining
#: work is divided by this, so guessing high shortens every ETA and turns a shot that
#: will miss its date into one that comfortably makes it. Being wrong in the pessimistic
#: direction costs an unnecessary amber row; being wrong the other way costs a missed
#: deadline nobody was warned about.
DEFAULT_WORKERS = 1


def rate(
    shot: Shot,
    telemetry: Mapping[str, Any] | None,
    *,
    now_epoch: int | None = None,
) -> Shot:
    """Return ``shot`` with its ETA, slack, confidence and risk filled in.

    Args:
        shot: The row as reconstructed from telemetry, carrying frame counts.
        telemetry: What else is known about this shot: ``deadline_epoch`` (int or None),
            ``durations`` (observed frame seconds, oldest first) and optionally
            ``workers``. ``None`` or empty means telemetry knows nothing beyond the frame
            counts, which is the ordinary state at the top of a render.
        now_epoch: The instant to rate against. Injected for tests; defaults to now.

    Returns:
        A copy of ``shot``. The original is not mutated, so a caller holding the
        pre-rating row still has it.

    Never raises. This runs behind a board that has to colour every row it is handed, and
    a traceback here would take down the whole page over one odd shot.
    """
    now = int(time.time()) if now_epoch is None else now_epoch
    detail = telemetry or {}

    durations = list(detail.get("durations") or [])
    deadline = detail.get("deadline_epoch")
    workers = int(detail.get("workers") or DEFAULT_WORKERS)

    forecast = estimate_completion(
        frames_total=shot.frames_total,
        frames_done=shot.frames_done,
        observed_durations=durations,
        workers=workers,
        now_epoch=now,
    )

    # Slack needs both halves. With no deadline there is nothing to be late for, and with
    # no ETA there is nothing to measure; either way the answer is "not known", which is
    # not the same as zero. Zero slack means landing exactly on the wire, which is a
    # claim, and painting it on a row nobody has estimated would be inventing one.
    slack: int | None = None
    if deadline is not None and forecast.eta_epoch is not None:
        slack = int(deadline) - forecast.eta_epoch

    risk = assess(
        # A shot with no deadline is rated on its own progress rather than against a date
        # it does not have. Passing 0 here would read as "due exactly now" and redden
        # every undated render; a large positive slack says only "nothing is pressing",
        # which is true.
        slack_seconds=float(slack) if slack is not None else float(_NO_DEADLINE_SLACK),
        remaining_seconds=forecast.remaining_seconds,
        confidence=forecast.confidence,
    )

    return shot.model_copy(
        update={
            "eta_epoch": forecast.eta_epoch,
            "deadline_epoch": int(deadline) if deadline is not None else None,
            "slack_seconds": slack,
            "confidence": forecast.confidence,
            "risk": risk,
        }
    )


#: Slack handed to the guardian for a shot with no deadline: a year, in seconds.
#:
#: Not infinity, which is not a float the arithmetic downstream should have to consider,
#: and not zero, which would mean "due right now" and redden every undated render. A year
#: is far enough out that the deadline term cannot decide the verdict, leaving the rating
#: to rest on progress and confidence alone, which is exactly what should decide it when
#: nothing has been promised.
_NO_DEADLINE_SLACK = 365 * 24 * 3600
