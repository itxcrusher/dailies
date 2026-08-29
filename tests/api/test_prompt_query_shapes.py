"""The prompt must give the agent the right query shape for each datasource.

Metrics and logs do not carry the same labels here, and the difference is invisible
until it silently returns nothing.

Verified against the live stack on 2026-08-29. In Prometheus, `shot` and `job` are both
ordinary labels and `{shot="SH050", job="dailies-render"}` selects the series. In Loki
they are not: OTLP puts them in structured metadata rather than the stream index, so

    {shot="SH050"}                   -> 0 entries
    {service_name="dailies-render"}  -> 1 entry, whose labels include shot=SH050

The agent, told only about `shot` and `job`, wrote the Prometheus shape for Loki, got an
empty result, and concluded there was "a complete absence of log data" for a shot whose
asset-missing warning was sitting in Loki the whole time. That is the exact failure this
project exists to catch, missed by the thing built to catch it.
"""

from dailies_api.investigation import investigation_prompt

PROMPT = investigation_prompt("dailies:SEQ01:SH050:job-v5-b")


def test_the_prompt_gives_the_loki_stream_selector_that_works():
    assert 'service_name="dailies-render"' in PROMPT


def test_the_prompt_says_shot_is_a_filter_in_loki_not_a_selector():
    """A stream selector on shot matches nothing; it has to be a filter expression."""
    assert "| shot=" in PROMPT


def test_the_prompt_still_gives_the_prometheus_shape():
    assert 'shot="SH050"' in PROMPT


def test_the_prompt_warns_that_an_empty_loki_result_may_be_the_wrong_selector():
    lowered = PROMPT.lower()
    assert "structured metadata" in lowered or "stream selector" in lowered
