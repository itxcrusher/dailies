"""The risk verdict: does this shot still make the deadline, and do we believe ourselves.

:mod:`dailies_graph.model` says how much room a shot has and :mod:`dailies_graph.forecast`
says how much work is left. Neither of them says whether anyone should worry, and that is
the only thing a supervisor actually reads off the board. This module turns the two
numbers into one of the five :class:`dailies_api.state.Risk` states, and does it as a pure
function so the verdict is reproducible from its inputs alone: same slack, same work, same
confidence, same colour, today and in a replay next month.

**Thresholds are ratios, never durations.** Ten minutes of slack is comfortable on a
five-minute shot and nearly gone on a five-hour one, so any absolute threshold is wrong
about one of those two on every production. What the board is really asking is "how many
times over could this shot's remaining work still fit in the room it has left", and that
question has the same answer at every scale.

===================== ============ =========================================
``slack / remaining`` Risk         Read as
===================== ============ =========================================
slack < 0             ``MISSED``   the window is gone, work is outstanding
< 0.1                 ``CRITICAL`` a 10% overrun ends it
< 0.25                ``AT_RISK``  one bad frame batch ends it
< 1.0                 ``WATCH``    it fits, with no room for a second run
>= 1.0                ``ON_TRACK`` it could render again and still land
===================== ============ =========================================

The bands are round numbers picked for what they are used for, not fitted to a
distribution. 0.25 is the interesting one: it is the line the Guardian escalates on,
placed where a shot can still be helped (re-prioritised, given workers, re-scoped) rather
than where the miss is already arithmetic. 0.1 marks the point where the only honest
report is that the shot needs a perfect run, nothing going wrong at all. 1.0 is the one
threshold that is not a taste call: below it, a single re-render of what is left no longer
fits in the room the shot has. The comparisons are strict, so a ratio of exactly 0.25 is
``WATCH`` and exactly 1.0 is ``ON_TRACK``; a test pins all three edges, because flipping
one of them to ``<=`` would move every verdict sitting on a boundary by a band and change
nothing else visible.

**Confidence is a floor on the verdict, not a modifier of it.** A forecast the system
cannot stand behind must not be allowed to paint a row green. Both floors that exist today
are ``WATCH``:

- ``unknown`` never reports better than ``WATCH``. Nothing has been observed, so there is
  no evidence the shot is fine, and reporting green on no evidence is the exact failure
  this project exists to argue against.
- ``low`` never reports better than ``WATCH`` either, and deliberately no harder than
  that, because ``low`` currently means two opposite things.
  :mod:`dailies_graph.forecast` returns it both for frame costs that are swinging wildly,
  which is a symptom worth escalating on (a farm thrashing, a scene whose cost is
  exploding, a node about to fail), and for **fewer than three observations**, which is
  merely a shot that has just started. Flooring at ``AT_RISK`` on a signal that covers
  both would report "projected to miss unless something changes" for every shot on the
  board for its first two frames, with a day of slack in hand. Until ``forecast``
  distinguishes a thin record from an erratic one, the honest floor for the pair is the
  one that means "worth a human glance": a supervisor can tell the two apart by opening
  the shot, and the board must not spend its alarm on the ordinary case.
- An unrecognised confidence string gets the same floor. A typo must not be the thing that
  lets a row go green.

Floors, rather than a downgrade applied only to ``ON_TRACK``, because a floor keeps the
verdict monotone: more slack can never produce a redder row. A rule that reddened only the
best case would rank a shot with an hour of slack worse than one with a minute of it, and
a board whose colours invert under some conditions teaches a supervisor to stop trusting
the colours, which costs more than any single wrong verdict. A test pins the monotonicity.

**A landed shot is answered before the floors run.** Zero remaining work means there is
nothing left to be late with, and :class:`dailies_api.state.Risk` defines ``MISSED`` as the
deadline passing with a shot *unfinished*, so a delivered shot is not painted with it
however far past the deadline the clock has gone. The floors are skipped rather than merely
outranked, because a confidence floor is a statement about an ETA and a shot that has
landed no longer has one: :mod:`dailies_graph.forecast` gives a finished shot a real
confidence rather than ``unknown`` precisely so delivered work does not read as
not-on-track, and a floor applied afterwards would take that back for any shot whose spread
happened to land on ``low``. That check also keeps the ratio from dividing by zero.

**Degenerate numbers are refused rather than judged.** A ``nan`` compares false against
every threshold, so an unguarded ratio walks the whole table and falls out of the bottom as
``ON_TRACK``: corrupt arithmetic would show as the safest colour on the board. So would a
missing work estimate, and missing is ordinary rather than exotic:
:attr:`dailies_graph.forecast.Forecast.remaining_seconds` is ``float | None`` and is
``None`` for every shot that has not rendered a frame yet, has no workers, or carries a
corrupt duration in its record. ``None``, ``nan`` and ``+inf`` are therefore all read as no
evidence at all and take the no-evidence floor, the same stance
:mod:`dailies_graph.forecast` takes on a corrupt frame duration. ``-inf`` slack is the one
non-finite input that is not ambiguous: it is unambiguously past the deadline, so with work
outstanding it reads ``MISSED``, rather than being rounded back to the greener answer on
the most severe input this function can be handed.

**One verdict, several assessors.** What this module computes is a *delivery* verdict:
whether the arithmetic of work against room says the shot lands. That is not the only way a
shot fails. The Visual QA pass produces the case this project exists to show, infrastructure
reporting a shot finished while the rendered output is broken, and delivery arithmetic alone
answers ``ON_TRACK`` for it, because there is genuinely no work left. ``assess`` therefore
takes an optional ``failure_risk``, a verdict from another assessor, and returns the more
severe of the two; :attr:`dailies_api.state.Shot.risk` is the max-severity combination of
every assessor with an opinion on the shot, not the output of this one function. The seam
is a parameter with a default rather than a later signature change, so that adding the
second source touches no existing call site.

Pure by construction: no clock, no I/O, no store. The instant is already baked into the
slack the caller passes in, which is what lets the same shot be re-assessed at a past
timestamp during a post-mortem and give the same answer it gave live.
"""

from __future__ import annotations

import math

from dailies_graph.forecast import Confidence

from dailies_api.state import Risk

__all__ = ["assess"]

#: The Risk members least to most severe. ``state.Risk`` documents its declaration order
#: as exactly that and offers ``list(Risk).index`` as a sort key; this is that key, built
#: once rather than per comparison.
_SEVERITY: tuple[Risk, ...] = tuple(Risk)

#: Ratio of slack to remaining work below which each verdict applies, ordered most to
#: least severe and read in that order, so the first band that matches wins.
_BANDS: tuple[tuple[float, Risk], ...] = (
    (0.10, Risk.CRITICAL),
    (0.25, Risk.AT_RISK),
    (1.00, Risk.WATCH),
)

#: What an unrecognised confidence string gets, and what an unreadable or missing number
#: gets: worth a glance, never green. A value this module cannot read is not one it may
#: treat as evidence of safety.
_NO_EVIDENCE_FLOOR = Risk.WATCH

#: The best verdict each confidence level is allowed to produce. See the module docstring
#: for why ``low`` floors no harder than ``unknown`` does, while ``forecast`` reports it
#: for both a thin record and an erratic one.
_FLOOR: dict[str, Risk] = {
    "high": Risk.ON_TRACK,
    "medium": Risk.ON_TRACK,
    "low": _NO_EVIDENCE_FLOOR,
    "unknown": _NO_EVIDENCE_FLOOR,
}


def assess(
    slack_seconds: float,
    remaining_seconds: float | None,
    confidence: Confidence,
    failure_risk: Risk | None = None,
) -> Risk:
    """Rate one shot against its deadline.

    Args:
        slack_seconds: Room between now and the deadline after the dependency chain
            through this shot, from :func:`dailies_graph.model.slack_seconds`. Negative is
            a real answer and the most important one, so it is never clamped. Typed as a
            float although the graph returns an int, so that a caller may weigh a
            fractional estimate without rounding it first and losing the sign near zero.
        remaining_seconds: Work left on the shot in seconds, already divided by the
            workers rendering it, as
            :attr:`dailies_graph.forecast.Forecast.remaining_seconds` reports it. Zero or
            less means the shot has landed. ``None`` arrives from a forecast with no
            estimate to give and is read as no evidence rather than refused, because a
            shot that has not rendered a frame yet is the ordinary state at the top of a
            render, not an exceptional one.
        confidence: How steady the observations behind ``remaining_seconds`` were, from
            :attr:`dailies_graph.forecast.Forecast.confidence`. This is the ETA's
            confidence, not the investigator's diagnosis confidence; the two are different
            quantities that share a word, as :mod:`dailies_graph.forecast` sets out.
            Typed to the literal a forecast actually produces; a string outside it is
            still handled at runtime rather than trusted, because nothing type-checks
            a value that arrives over JSON.
        failure_risk: A verdict from an assessor that is not delivery arithmetic, such as
            the Visual QA pass rejecting a rendered frame. ``None``, the default, means
            nothing else has an opinion on this shot. The result is never better than this
            value, so a second source can redden a row but never green one.

    Returns:
        One :class:`dailies_api.state.Risk`: the more severe of the delivery verdict and
        ``failure_risk``, and, for a shot still running, never better than what
        ``confidence`` allows.

    This function never raises. It runs behind a board that must colour every row it is
    handed, so a degenerate input produces the most honest verdict available instead of a
    traceback that takes the whole board down with it.
    """
    delivery = _delivery_verdict(slack_seconds, remaining_seconds, confidence)
    if failure_risk is None:
        return delivery
    return _no_better_than(delivery, failure_risk)


def _delivery_verdict(
    slack_seconds: float,
    remaining_seconds: float | None,
    confidence: Confidence,
) -> Risk:
    """What the delivery arithmetic says on its own, confidence floors included."""
    if _has_landed(remaining_seconds):
        # Nothing left to be late with. Answered before the slack check so a shot that has
        # landed is never painted MISSED, and before the floors so it is never held amber
        # by the confidence of an ETA it no longer has.
        return Risk.ON_TRACK

    floor = _FLOOR.get(confidence, _NO_EVIDENCE_FLOOR)

    if remaining_seconds is None or not math.isfinite(remaining_seconds):
        # No readable estimate of the work outstanding, so there is no ratio to take and
        # the floors are the whole answer.
        return _no_better_than(floor, _NO_EVIDENCE_FLOOR)

    if slack_seconds == -math.inf:
        # Work outstanding, and the deadline not merely past but unreachably so. Unlike a
        # nan this is not an unreadable number, it is the most severe readable one, and
        # refusing it alongside nan would report the greener of the two possible errors on
        # the worst input this function takes.
        return Risk.MISSED

    if not math.isfinite(slack_seconds):
        return _no_better_than(floor, _NO_EVIDENCE_FLOOR)

    return _no_better_than(_from_arithmetic(slack_seconds, remaining_seconds), floor)


def _has_landed(remaining_seconds: float | None) -> bool:
    """True when the shot has no work outstanding, as opposed to none we can read.

    ``None`` and ``nan`` are not zero however they compare: they are the absence of an
    estimate, and a shot nobody can estimate has not been observed to finish.
    """
    return (
        remaining_seconds is not None
        and math.isfinite(remaining_seconds)
        and remaining_seconds <= 0
    )


def _from_arithmetic(slack_seconds: float, remaining_seconds: float) -> Risk:
    """The verdict the two numbers imply, for a shot with work genuinely outstanding.

    Both are finite and ``remaining_seconds`` is positive; the landed and unreadable cases
    are answered by the caller, which is what keeps the division here safe.
    """
    if slack_seconds < 0:
        return Risk.MISSED
    ratio = slack_seconds / remaining_seconds
    for below, risk in _BANDS:
        if ratio < below:
            return risk
    return Risk.ON_TRACK


def _no_better_than(verdict: Risk, floor: Risk) -> Risk:
    """Whichever of the two is the more severe."""
    return max(verdict, floor, key=_SEVERITY.index)
