"""When will this shot finish, given only the frames it has rendered so far.

A render farm reports an ETA when a job completes, which is the one moment nobody needs
one. Everything useful about delivery risk happens while the shot is still running, so
this module forecasts from the partial record: some frames observed, most not.

Two judgement calls sit inside, and both are made in the direction that costs least when
they are wrong.

**Recent frames weigh more.** Render cost drifts *within* a shot: geometry enters, a light
rig gets denser, a volumetric starts halfway through the sequence. A flat mean over the
whole record lags that drift in exactly the wrong direction, reporting the cheap opening
frames as if they still described the work. The estimate here is an exponentially weighted
mean with a **half-life of five frames**: an observation five frames back counts half as
much as the newest one, ten frames back a quarter, and so on. Five is short enough to
follow a genuine complexity ramp within a shot and long enough that one slow frame (a
cache miss, a noisy neighbour on the farm) carries about 13% of the weight rather than
dominating. It is a tunable, not a law; what the tests pin is the direction, that a shot
which got heavier does not get forecast from when it was cheap.

**Confidence comes from steadiness, not from sample count.** Eight frames at a flat 10s
support a better forecast than forty frames scattered between 1s and 60s, and reporting
``high`` on the scattered ones is worse than reporting ``low``: a board that overstates
its own certainty gets a supervisor to stop checking. The measure is the coefficient of
variation, the standard deviation over the mean, because it is scale-free. A 2-second
spread is tight on 400-second frames and wild on 4-second ones, and an absolute threshold
would call one of those two wrong on every production.

The bands: within 15% spread is ``high``, within 50% is ``medium``, beyond that is
``low``. They are round numbers chosen for what the ETA is used for rather than derived
from a distribution: the board sorts shots by delivery risk, so what matters is separating
"this rate is a fact" from "this rate is an average of two different behaviours", and the
middle band is where a supervisor should look at the shot rather than at the number.

Sample count is not ignored, it is a floor: **fewer than three observations caps
confidence at** ``low``. One frame has no spread at all, which is not the same as having a
small one, and calling that ``high`` would mean the board is most confident at the moment
it knows least.

``unknown`` is a first-class answer. With nothing observed, or with nothing rendering the
remaining frames, there is no honest ETA and the module returns ``None`` rather than a
guess; the board draws a dash. ``eta_epoch is None``, ``remaining_seconds is None`` and
``confidence == "unknown"`` are the same state seen from three sides, and that equivalence
is pinned by a test.

**Corrupt input is refused, not averaged.** A duration that is nan, infinite or negative
is not a slow frame, it is a broken measurement: a clock skew, a parser reading
``end - start`` backwards, a sentinel that escaped. Averaging one in moves the rate
silently, and the negative case moves it *down*, which buys the shot time it does not have
and is the one direction a delivery-risk board must never fail in. Any such value makes
the whole record unusable and the forecast ``unknown``.

**On the word** ``confidence``. :data:`dailies_api.agent.DIAGNOSIS_SCHEMA` also carries a
``confidence``, and it is a different quantity with a narrower value set: high/medium/low,
describing how sure the investigator is of a *cause*, while this one is
high/medium/low/``unknown`` and describes how steady the observed frame *rate* has been. A
shot can have a rock-solid ETA and a guessed cause, or the reverse. Both land on the same
board row, so a surface rendering the two should label them apart (``eta_confidence`` and
``diagnosis_confidence``) rather than print ``confidence`` twice and leave a reader to work
out which is which. Same reasoning as the ``ShotNode``/``Shot`` note in
:mod:`dailies_graph.model`: one name per concept, or say plainly that there are two.

Like :mod:`dailies_graph.model`, this module is **pure**: no clock, no I/O, no network.
``now_epoch`` is a parameter, so a forecast is reproducible from its inputs alone, which
is what lets a risk verdict be trusted rather than merely displayed.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, Field

__all__ = ["Confidence", "Forecast", "estimate_completion"]

Confidence = Literal["high", "medium", "low", "unknown"]

# An observation this many frames back carries half the weight of the newest one. See the
# module docstring: short enough to track drift inside a shot, long enough that a single
# outlier frame does not become the forecast.
_WEIGHT_HALF_LIFE_FRAMES = 5

# Coefficient-of-variation bands. Scale-free on purpose, so the same thresholds hold for
# 4-second frames and 400-second ones.
_STEADY_ENOUGH_FOR_HIGH = 0.15
_STEADY_ENOUGH_FOR_MEDIUM = 0.50

# Below this many observations there is no spread to measure, only an absence of one.
_MIN_OBSERVATIONS_FOR_SPREAD = 3


class Forecast(BaseModel):
    """An estimated completion time and how much the estimate should be trusted.

    ``seconds_per_frame`` is reported even when ``eta_epoch`` is ``None`` (no workers, for
    instance): the observed rate is a measurement and stays useful, while the ETA is a
    projection that needs something to project onto. ``remaining_seconds`` is a projection
    too, so it travels with the ETA rather than with the rate.
    """

    eta_epoch: int | None = Field(
        description=(
            "Absolute epoch second the shot is expected to finish, or None when no honest "
            "estimate exists. Rounded up, so the board never advertises a frame early."
        )
    )
    remaining_seconds: float | None = Field(
        description=(
            "Work left on this shot in seconds, after dividing by the workers rendering "
            "it. None whenever eta_epoch is None. Reported rather than left to callers to "
            "re-derive as eta_epoch - now_epoch, which would make every caller carry "
            "now_epoch alongside the forecast and inherit the ETA's rounding second-hand. "
            "This is the quantity a risk assessment weighs against slack, and the one "
            "dailies_graph.model.ShotNode.estimated_seconds wants."
        )
    )
    seconds_per_frame: float | None = Field(
        description=(
            "Recency-weighted cost of one frame, from the observations supplied. None "
            "when nothing has been observed, or when the record holds a value that is not "
            "a duration at all."
        )
    )
    confidence: Confidence = Field(
        description=(
            "How steady the observed frame costs are: high under 15% spread, medium under "
            "50%, low beyond that or with fewer than three observations, unknown when "
            "there is no ETA at all. Not the investigator's diagnosis confidence, which "
            "has no 'unknown' and rates a cause rather than a rate."
        )
    )


def estimate_completion(
    frames_total: int,
    frames_done: int,
    observed_durations: Sequence[float],
    workers: int,
    now_epoch: int,
) -> Forecast:
    """Forecast when a shot finishes from the frames it has rendered so far.

    Args:
        frames_total: Frames in the shot. Clamped at zero, matching the ``ge=0`` bound
            :class:`dailies_api.state.Shot` puts on the same number.
        frames_done: Frames already rendered. Clamped at zero for the same reason: a
            negative count is a wiring bug, and taken literally it would inflate the
            remaining work past the length of the shot.
        observed_durations: Seconds each finished frame took, **oldest first**. Order is
            load-bearing: the weighting reads the tail as the current rate, so a reversed
            sequence forecasts the shot backwards.
        workers: How many frames the farm renders at once for this shot. Remaining work is
            divided by this, which is the only place farm capacity enters the system;
            :mod:`dailies_graph.model` deliberately keeps it out of the ordering graph.
        now_epoch: The instant the forecast is made, as an absolute epoch second.

    Returns:
        A :class:`Forecast`, in one of two honest shapes.

        ``unknown``, carrying no ETA, when the rate cannot be projected: no observations at
        all, a nan, infinite or negative duration anywhere in the record, a weighted rate
        of zero or less, no workers to render what is left, or remaining work large enough
        to overflow to infinity.

        A confident ETA of ``now_epoch``, with ``remaining_seconds`` of zero, when there is
        nothing left to wait for: ``frames_done`` has reached ``frames_total``, or overrun
        it, which live telemetry does produce when a retried frame is counted twice. That
        holds however many workers the shot has, including none, because a delivered shot
        has normally had its workers taken back.

    This function never raises. It runs behind a board that must render something for every
    shot it is handed, so a degenerate input greys one cell out instead.
    """
    if not observed_durations:
        return Forecast(
            eta_epoch=None, remaining_seconds=None, seconds_per_frame=None, confidence="unknown"
        )

    seconds_per_frame = _recency_weighted_mean(observed_durations)
    if _has_impossible_duration(observed_durations) or not math.isfinite(seconds_per_frame):
        # nan, infinity and negative durations are refused together, before the mean they
        # produced is trusted. The non-finite half would otherwise reach ``math.ceil``,
        # which raises on it; the negative half is worse, because it passes every later
        # check and quietly returns a short ETA. One bad sample among nineteen good ones is
        # enough to report a shot as running four times faster than it is.
        return Forecast(
            eta_epoch=None, remaining_seconds=None, seconds_per_frame=None, confidence="unknown"
        )

    # Clamped, then floored at zero: a finished shot and a frames_done that has overrun
    # frames_total both mean the same thing to a board, that there is no work left to wait
    # for, and a negative count means the caller is broken rather than the shot enormous.
    frames_left = max(max(frames_total, 0) - max(frames_done, 0), 0)
    if frames_left == 0:
        # Answered before the worker check on purpose. Whether anything is still rendering
        # cannot change when a shot lands once nothing is left to render, and zero workers
        # is the ordinary state of a delivered shot, because the farm takes them back.
        # Deciding this on worker count would put finished work on the board as unknown,
        # which can never read as on-track, and send a supervisor to look at a shot that
        # already landed.
        return Forecast(
            eta_epoch=now_epoch,
            remaining_seconds=0.0,
            seconds_per_frame=seconds_per_frame,
            confidence=_confidence_from_spread(observed_durations),
        )

    if seconds_per_frame <= 0 or workers <= 0:
        # A frame that costs nothing is not a measurement, and no worker means no
        # progress. Either way the rate cannot be projected forward, so there is no ETA to
        # label. The observed rate is still reported: it is data, the ETA is the inference.
        return Forecast(
            eta_epoch=None,
            remaining_seconds=None,
            seconds_per_frame=seconds_per_frame,
            confidence="unknown",
        )

    remaining_seconds = frames_left * seconds_per_frame / workers
    if not math.isfinite(remaining_seconds):
        # Two finite numbers can multiply to infinity, and ``math.ceil`` raises on that.
        # Rare, but it is the same board-down failure the guard above exists to prevent,
        # so it gets the same answer.
        return Forecast(
            eta_epoch=None,
            remaining_seconds=None,
            seconds_per_frame=seconds_per_frame,
            confidence="unknown",
        )

    return Forecast(
        eta_epoch=now_epoch + math.ceil(remaining_seconds),
        remaining_seconds=remaining_seconds,
        seconds_per_frame=seconds_per_frame,
        confidence=_confidence_from_spread(observed_durations),
    )


def _has_impossible_duration(durations: Sequence[float]) -> bool:
    """True if any observation is not a duration a frame could actually have taken.

    Negative belongs here beside nan and infinity rather than being clamped away: a frame
    that took less than no time is not a fast frame, it is evidence the record is wrong,
    and the rest of that record is no more trustworthy for it.
    """
    return any(not math.isfinite(d) or d < 0 for d in durations)


def _recency_weighted_mean(durations: Sequence[float]) -> float:
    """Exponentially weighted mean of ``durations``, the newest observation weighing most.

    Seeded with the oldest value and folded forward, which is the standard EWMA recurrence
    and needs no separate normalisation pass: the weights it implies already sum to one.
    """
    alpha = 1 - 2 ** (-1 / _WEIGHT_HALF_LIFE_FRAMES)
    weighted = durations[0]
    for duration in durations[1:]:
        weighted = alpha * duration + (1 - alpha) * weighted
    return weighted


def _confidence_from_spread(durations: Sequence[float]) -> Confidence:
    """Band the coefficient of variation of ``durations``.

    Unweighted on purpose. The estimate asks "what does a frame cost right now"; this asks
    "has this shot behaved consistently at all", and discounting the early frames here
    would hide exactly the instability the answer is supposed to report.
    """
    if len(durations) < _MIN_OBSERVATIONS_FOR_SPREAD:
        return "low"
    mean = sum(durations) / len(durations)
    if mean <= 0:
        return "low"
    variance = sum((d - mean) ** 2 for d in durations) / len(durations)
    spread = math.sqrt(variance) / mean
    if spread <= _STEADY_ENOUGH_FOR_HIGH:
        return "high"
    if spread <= _STEADY_ENOUGH_FOR_MEDIUM:
        return "medium"
    return "low"
