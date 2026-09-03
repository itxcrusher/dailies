"""Scoring the visual check against frames whose answer is known.

The SPEC calls Visual QA the differentiator, and until now the entry's evidence for it was
three anecdotes on a board. This is the second of the four metrics the SPEC named:
**visual-defect recall**, the fraction of induced visual failures the check actually
catches, measured against real frames the farm rendered.

Both directions are scored and the second one is the harder claim. Catching every defect is
easy for a check that calls everything suspect, so a defect-only score is not a measurement.
The clean frames are what make the recall number mean anything.
"""

import pytest
from dailies_api.evals.visual import VISUAL_CASES, score_visual


class TestTheCases:
    def test_both_directions_are_represented(self):
        """A recall number from defect frames alone is unfalsifiable: a check that answers
        'suspect' to everything would score perfectly."""
        assert any(case.expect_defect for case in VISUAL_CASES)
        assert any(not case.expect_defect for case in VISUAL_CASES)

    def test_every_case_names_a_real_frame_the_farm_rendered(self):
        """Not a fixture drawn for the test. These are objects in the frames bucket,
        written by Blender during a real render."""
        for case in VISUAL_CASES:
            assert case.object_name.startswith(("SH200/", "SH201/"))
            assert case.object_name.endswith(".png")

    def test_the_defect_cases_are_the_shot_rendered_with_a_missing_texture(self):
        assert all(c.object_name.startswith("SH201/") for c in VISUAL_CASES if c.expect_defect)


class TestScoring:
    def test_calling_a_broken_frame_suspect_is_a_catch(self):
        assert score_visual(expect_defect=True, verdict="suspect") is True

    def test_calling_a_broken_frame_broken_is_also_a_catch(self):
        """The vocabulary has two words for wrong. Either is the check doing its job."""
        assert score_visual(expect_defect=True, verdict="broken") is True

    def test_missing_a_broken_frame_is_a_miss(self):
        assert score_visual(expect_defect=True, verdict="looks_correct") is False

    def test_calling_a_clean_frame_correct_is_right(self):
        assert score_visual(expect_defect=False, verdict="looks_correct") is True

    def test_calling_a_clean_frame_suspect_is_a_false_positive(self):
        """The expensive error. A check that cries wolf on working frames makes a
        supervisor stop reading it, which costs more than the defects it would have
        caught."""
        assert score_visual(expect_defect=False, verdict="suspect") is False

    @pytest.mark.parametrize("verdict", ["", None, "probably fine"])
    def test_an_unreadable_verdict_never_counts_as_a_catch(self, verdict):
        """A failed check is not a passed one. Counting an error as a catch would inflate
        recall exactly when the check is broken."""
        assert score_visual(expect_defect=True, verdict=verdict) is False
        assert score_visual(expect_defect=False, verdict=verdict) is False
