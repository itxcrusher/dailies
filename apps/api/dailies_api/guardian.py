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
fits in the room the shot has.

**Confidence is a floor on the verdict, not a modifier of it.** A forecast the system
cannot stand behind must not be allowed to paint a row green:

- ``unknown`` never reports better than ``WATCH``. Nothing has been observed, so there is
  no evidence the shot is fine, and reporting green on no evidence is the exact failure
  this project exists to argue against.
- ``low`` never reports better than ``AT_RISK``. That is deliberately *more* severe than
  ``unknown``, which reads backwards until you see what the two mean. ``unknown`` is an
  absence of data, and a shot that has not started rendering yet is not in trouble.
  ``low`` is data, and what it says is that frame costs are swinging wildly, which is
  itself a symptom: a farm thrashing, a scene whose cost is exploding, a node about to
  fail. An ETA averaged out of that is a number with no shot behind it.
- An unrecognised confidence string gets the ``unknown`` floor. A typo must not be the
  thing that lets a row go green.

Floors, rather than a downgrade applied only to ``ON_TRACK``, because a floor keeps the
verdict monotone: more slack can never produce a redder row. A rule that reddened only the
best case would rank a shot with an hour of slack worse than one with a minute of it, and
a board whose colours invert under some conditions teaches a supervisor to stop trusting
the colours, which costs more than any single wrong verdict. A test pins the monotonicity.

**Two inputs are refused rather than judged.** A ``nan`` compares false against every
threshold, so an unguarded ratio walks the whole table and falls out of the bottom as
``ON_TRACK``: corrupt arithmetic would show as the safest colour on the board. Non-finite
slack or remaining work is therefore treated as no evidence at all and takes the
``unknown`` floor, the same stance :mod:`dailies_graph.forecast` takes on a corrupt frame
duration. And zero remaining work means the shot has landed; there is nothing left to be
late with, and :class:`dailies_api.state.Risk` defines ``MISSED`` as the deadline passing
with a shot *unfinished*, so a delivered shot is not painted with it however far past the
deadline the clock has gone. That check also keeps the ratio from dividing by zero.

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

#: The best verdict each confidence level is allowed to produce. See the module docstring
#: for why ``low`` floors harder than ``unknown`` does.
_FLOOR: dict[str, Risk] = {
    "high": Risk.ON_TRACK,
    "medium": Risk.ON_TRACK,
    "low": Risk.AT_RISK,
    "unknown": Risk.WATCH,
}

#: What an unrecognised confidence string gets, and what corrupt arithmetic gets: the
#: ``unknown`` floor. A value this module does not recognise is not one it may read as
#: evidence of safety.
_NO_EVIDENCE_FLOOR = Risk.WATCH


def assess(
    slack_seconds: float,
    remaining_seconds: float,
    confidence: Confidence,
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
            less means the shot has landed.
        confidence: How steady the observations behind ``remaining_seconds`` were, from
            :attr:`dailies_graph.forecast.Forecast.confidence`. This is the ETA's
            confidence, not the investigator's diagnosis confidence; the two are different
            quantities that share a word, as :mod:`dailies_graph.forecast` sets out.
            Typed to the literal a forecast actually produces; a string outside it is
            still handled at runtime rather than trusted, because nothing type-checks
            a value that arrives over JSON.

    Returns:
        One :class:`dailies_api.state.Risk`, never better than what ``confidence`` allows.

    This function never raises. It runs behind a board that must colour every row it is
    handed, so a degenerate input produces the most honest verdict available instead of a
    traceback that takes the whole board down with it.
    """
    floor = _FLOOR.get(confidence, _NO_EVIDENCE_FLOOR)

    if not (math.isfinite(slack_seconds) and math.isfinite(remaining_seconds)):
        # The arithmetic is refused, not attempted, so the verdict is whatever the floors
        # allow. Both floors apply: a corrupt number is no evidence of safety, and a
        # confidence that was already floored harder than that keeps its own floor.
        return _no_better_than(floor, _NO_EVIDENCE_FLOOR)

    return _no_better_than(_from_arithmetic(slack_seconds, remaining_seconds), floor)


def _from_arithmetic(slack_seconds: float, remaining_seconds: float) -> Risk:
    """The verdict the two numbers imply on their own, before confidence has its say."""
    if remaining_seconds <= 0:
        # Nothing left to be late with. Answered before the slack check so that a shot
        # which has landed is never painted MISSED, which state.Risk reserves for a
        # deadline passing with the shot unfinished.
        return Risk.ON_TRACK
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
