"""Visual-defect recall, measured against frames the farm actually rendered.

The SPEC names Visual QA the differentiator and lists **visual-defect recall** as one of
four metrics the project should publish. Until now the evidence for the differentiator was
three anecdotes on a board, which is a lower standard than the diagnosis schema imposes on
the agent it grades.

**Real frames, read from the bucket, not fixtures drawn to be obvious.** SH200 is the demo
cube rendered normally; SH201 is the same scene with the jacket texture deliberately
missing, which Blender resolves to a flat magenta and reports as a success. Both were
written by Blender during real renders. A synthetic magenta square would test whether
Gemini can see magenta, which nobody doubts, rather than whether this check works on this
farm's output.

**Both directions, and the clean frames are the load-bearing half.** A check that answers
"suspect" to everything catches every defect, so a recall number computed only over broken
frames is unfalsifiable. The false-positive count is what makes the recall mean something,
and it is also the more expensive error in practice: a check that cries wolf on working
frames gets ignored, which costs more than the defects it would have caught.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

__all__ = ["VISUAL_CASES", "VisualCase", "run_visual_eval", "score_visual"]

_log = logging.getLogger(__name__)

#: Verdicts that mean the check saw something wrong. The schema's vocabulary has two words
#: for it, and either is the check doing its job.
_DEFECT_VERDICTS = frozenset({"suspect", "broken"})

#: Seconds between vision calls. Single-shot rather than an agent loop, so far cheaper than
#: an investigation, but the project's Vertex allowance is small enough that eight in a
#: burst still trips it.
_SETTLE_SECONDS = 4.0


@dataclass(frozen=True)
class VisualCase:
    """One frame with a known answer."""

    object_name: str
    expect_defect: bool
    why: str


VISUAL_CASES: tuple[VisualCase, ...] = tuple(
    [
        VisualCase(
            object_name=f"SH201/frame_{index:04d}.png",
            expect_defect=True,
            why=(
                "The jacket texture failed to resolve, so Blender substituted a flat "
                "colour and exited 0. Every number about this render reads success."
            ),
        )
        for index in range(1, 5)
    ]
    + [
        VisualCase(
            object_name=f"SH200/frame_{index:04d}.png",
            expect_defect=False,
            why="The same scene rendered normally. Calling this suspect is the expensive error.",
        )
        for index in range(1, 5)
    ]
)


def score_visual(*, expect_defect: bool, verdict: str | None) -> bool:
    """Whether the check answered correctly for a frame whose answer is known.

    An unreadable verdict is never a catch. Counting a failed check as a pass would inflate
    recall at exactly the moment the check is broken, which is the one time the number
    needs to be trusted.
    """
    if not verdict or verdict not in (_DEFECT_VERDICTS | {"looks_correct"}):
        return False
    saw_defect = verdict in _DEFECT_VERDICTS
    return saw_defect is expect_defect


def _default_model() -> str:
    from ..agent import INVESTIGATOR_MODEL

    return INVESTIGATOR_MODEL


async def run_visual_eval(*, bucket: str, model: str | None = None) -> dict[str, Any]:
    """Run every visual case and return the two numbers that matter."""
    from ..frames import gcs_reader
    from ..visual_qa import check_frame, gemini_vision

    read = gcs_reader(bucket)
    vision = gemini_vision(model or _default_model())

    caught = missed = correct_clean = false_alarm = 0
    rows: list[str] = []

    for index, case in enumerate(VISUAL_CASES):
        if index:
            await asyncio.sleep(_SETTLE_SECONDS)
        verdict: str | None = None
        try:
            image = await read(case.object_name)
            if image:
                answer = await check_frame(
                    image,
                    shot=case.object_name.split("/")[0],
                    model=vision,
                    path=case.object_name,
                )
                verdict = answer.get("verdict")
        except Exception as exc:  # noqa: BLE001 - a failed check is a result, not a crash
            _log.warning("Visual case %s failed: %s", case.object_name, exc)

        right = score_visual(expect_defect=case.expect_defect, verdict=verdict)
        if case.expect_defect:
            caught += right
            missed += not right
        else:
            correct_clean += right
            false_alarm += not right
        rows.append(f"  {'ok   ' if right else 'MISS '} {case.object_name:<24} {verdict}")

    defects = caught + missed
    cleans = correct_clean + false_alarm
    return {
        "rows": rows,
        "recall": f"{caught}/{defects}",
        "false_positives": f"{false_alarm}/{cleans}",
        "passed": missed == 0 and false_alarm == 0,
    }
