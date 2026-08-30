"""Silence in Loki means no failure, not a broken log pipeline.

The emitter ships only render-domain failure conditions - asset_missing, oom,
engine_crash, frame_failed - and deliberately drops frame starts and completions, so a
97-line Blender render does not become 97 log records (see
dailies_telemetry.log_emitter.LOGGED_KINDS). A shot that rendered cleanly therefore has
no logs at all, by design.

The investigator cannot know that, and it showed. Diagnosing the healthy SH040 it
answered at MEDIUM confidence that "no corresponding logs were found in Loki, preventing
verification of render quality", and recommended investigating why logs were not
appearing for the shot. Every word of that is reasonable from where it was standing, and
it is the wrong answer: the silence was the good news.

It also costs the demo the thing it exists to show. The ablation is a healthy shot and a
broken one told apart confidently; a healthy shot reported as unverifiable blunts it.
"""

from dailies_api.agent import INVESTIGATOR_INSTRUCTION

TEXT = INVESTIGATOR_INSTRUCTION.lower()


def test_it_is_told_the_pipeline_logs_only_failures():
    assert "only" in TEXT and "loki" in TEXT


def test_it_is_told_absence_of_logs_is_a_good_sign_for_a_complete_shot():
    assert "no logs" in TEXT or "silence" in TEXT


def test_it_still_knows_an_empty_query_result_can_be_a_query_defect():
    """The two rules must coexist: empty CAN be a bad query, and CAN be real silence.

    They are distinguished by which datasource: an empty Prometheus result for a shot on
    the board is a query defect, because the board was built from those very series. An
    empty Loki result, once the selector is right, is a real and meaningful absence.
    """
    assert "empty" in TEXT
    assert "structured metadata" in TEXT or "service_name" in TEXT
