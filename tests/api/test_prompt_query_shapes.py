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


def test_the_time_window_is_attached_to_loki_not_only_to_prometheus():
    """The window has to be stated where the Loki query is described.

    Driven on the deployed system: asked about SH201, the agent ran exactly the right
    Loki selector and reported "no log entries", while the same query run by hand over
    now-24h returned two asset_missing warnings. The window instruction lived inside the
    bullet about PromQL, so the agent applied it to Prometheus - both metric queries
    worked - and omitted it on Loki, which defaulted to a short recent window and found
    nothing for a sixteen-hour-old render.

    Empty rather than an error, for the sixth time on this stack, and this one made the
    agent report a clean shot that had a missing asset.
    """
    from dailies_api.investigation import investigation_prompt
    from dailies_api.shot_source import LOOKBACK

    prompt = investigation_prompt("dailies:SEQ01:SH201:vqa-bad")
    window = LOOKBACK.lstrip("now-")

    # The Loki paragraph specifically must carry the window, not just the document.
    loki_part = prompt[prompt.index("Loki:") :]
    assert window in loki_part, "the Loki instruction must name the same window"


def test_the_prompt_says_the_window_applies_to_both_datasources():
    from dailies_api.investigation import investigation_prompt

    lowered = investigation_prompt("dailies:SEQ01:SH201:vqa-bad").lower()
    assert "both" in lowered or "every query" in lowered
