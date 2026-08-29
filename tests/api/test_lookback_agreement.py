"""The board and the investigator must agree on what "recent" means.

Found by driving the deployed system: the board listed SH040 (4/4 frames, derived from
Prometheus over a 24h window) while the investigator, told to look at "the last 30 to 60
minutes", correctly reported no telemetry for it. The render was ~70 minutes old. Both
components were behaving exactly as written, and the pair was still incoherent: a board
that lists a shot the agent will always say has no data.

Two windows meant two definitions of recent. This pins them to one.
"""

from dailies_api.investigation import investigation_prompt
from dailies_api.shot_source import LOOKBACK


def test_the_prompt_quotes_the_window_the_board_actually_lists():
    prompt = investigation_prompt("dailies:SEQ01:SH040:job-v5-a")

    # Not a substring check on a hardcoded "24h": the point is that the two constants are
    # the same object, so widening the board's window cannot silently leave the agent
    # looking at a narrower one.
    assert LOOKBACK.lstrip("now-") in prompt


def test_the_prompt_does_not_tell_the_agent_a_narrower_window():
    """The specific regression: "last 30 to 60 minutes" while the board listed 24 hours."""
    prompt = investigation_prompt("dailies:SEQ01:SH040:job-v5-a")

    assert "30 to 60 minutes" not in prompt


def test_the_prompt_still_carries_the_shot_label_and_the_board_id():
    prompt = investigation_prompt("dailies:SEQ01:SH040:job-v5-a")

    assert "SH040" in prompt
    assert "dailies:SEQ01:SH040:job-v5-a" in prompt
