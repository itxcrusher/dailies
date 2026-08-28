"""Turn a Blender stdout stream into render events.

The per-line classification lives in ``dailies_telemetry.parser``; this module adds
the only thing a single line cannot know, which is what came before it.
"""

import re
from collections.abc import Iterable, Iterator
from typing import Final

from dailies_telemetry.parser import parse_line
from dailies_telemetry.schema import RenderEvent

# Blender 4.5 prints a completed frame across TWO lines:
#
#     Saved: '/tmp/dailies/frame_0001.png'
#      Time: 00:00.76 (Saving: 00:00.48)
#
# The parser matches the single-line form, which is what the plan's invented fixture
# used and what some builds emit. Against real output the two-line shape produced zero
# FRAME_COMPLETE events across a 97-line render, so render_frame_duration_seconds never
# reached Grafana while Fra: lines kept flowing and the pipeline looked healthy.
#
# Joining the pair here rather than loosening the parser keeps the parser pure and
# single-line, and reuses its already-tested extraction instead of duplicating it.
_SAVED_PATH_ONLY: Final = re.compile(r"^\s*Saved:\s+'(?P<path>[^']+)'\s*$")
_TIME_ONLY: Final = re.compile(r"^\s*Time:\s*(?:\d+:)?\d+:[\d.]+")

__all__ = ["render_from_stream"]


def render_from_stream(lines: Iterable[str], shot: str) -> Iterator[RenderEvent]:
    """Turn a stream of Blender stdout lines into ``RenderEvent``s.

    Tracks the most recent frame number so events that do not carry one (warnings,
    crashes) are still attributed to the right frame.

    A generator, not a list: this is fed by a live render's stdout, so consumers
    have to see a failure as it is printed rather than when the job ends.
    """
    # ``None``, not 0: until a ``Fra:`` line arrives no frame is known, and 0 is a frame
    # Blender genuinely renders (``--frame-start 0`` for hold and reference frames). A
    # zero here would file a scene-load failure against a real frame 0.
    frame_hint: int | None = None
    # A ``Saved:`` line whose ``Time:`` has not arrived yet. Held for exactly one line:
    # if the next line is not the matching ``Time:``, the save is discarded rather than
    # emitted without a duration, because a FRAME_COMPLETE carrying no duration would
    # register in Grafana as a completed frame that took no time.
    pending_save: str | None = None

    for line in lines:
        if pending_save is not None:
            held, pending_save = pending_save, None
            if _TIME_ONLY.match(line):
                # Rejoin into the single-line form the parser already understands.
                line = f"{held.rstrip()}  {line.strip()}"

        if _SAVED_PATH_ONLY.match(line):
            pending_save = line
            continue

        event = parse_line(line, shot=shot, frame_hint=frame_hint)
        if event is None:
            continue
        # Unconditional, and deliberately not ``if event.frame:``: that truthiness test
        # discarded a genuine frame 0 and then misattributed every following unnumbered
        # line to the stale hint. ``parse_line`` has already substituted the hint for
        # lines that name no frame, so ``event.frame`` is the best answer available and
        # there is nothing to assign back onto the model (which would bypass validation
        # anyway - the model has no ``validate_assignment``).
        frame_hint = event.frame
        yield event
