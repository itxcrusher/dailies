"""A coarse step makes a short render invisible, and the result looks like absent data.

Measured against the live stack on 2026-08-30, one query, four step sizes:

    step_seconds=3600  ->  0 series
    step_seconds= 900  ->  0 series
    step_seconds= 300  ->  1 series, value 3
    step_seconds=  60  ->  1 series, 5 points, value 3

Prometheus resolves a range query at each step boundary and only sees a series that was
present within its lookback delta, five minutes by default. A render lasts seconds, so a
step wider than that steps straight over it.

The investigator hit this on SH050. It queried at step_seconds=3600, got nothing twice,
and reported that "Prometheus metrics for SH050 are unavailable" and that the affected
frame count "cannot be determined due to the lack of Prometheus data" - while the board
directly above its own diagnosis read 3 of 3, from those exact series.

This is the third distinct way an empty result has meant a wrong query rather than
missing data on this stack, after the instant-query staleness trap and the Loki stream
selector. They share one lesson worth stating in the prompt: on this stack, empty is
much more often a query defect than an absence.
"""

from dailies_api.agent import INVESTIGATOR_INSTRUCTION

TEXT = INVESTIGATOR_INSTRUCTION.lower()


def test_it_is_given_a_step_ceiling():
    assert "step_seconds" in TEXT


def test_the_ceiling_is_at_or_below_prometheus_lookback_delta():
    """300s is the default lookback delta; anything wider can miss a short job."""
    assert "300" in TEXT or "60" in TEXT


def test_it_is_told_why_a_coarse_step_returns_nothing():
    assert "lookback" in TEXT or "steps over" in TEXT or "step over" in TEXT


def test_it_is_told_empty_usually_means_the_query_not_the_data():
    assert "empty" in TEXT
