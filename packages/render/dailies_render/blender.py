"""Turn a Blender stdout stream into render events.

The per-line classification lives in ``dailies_telemetry.parser``; this module adds
the only thing a single line cannot know, which is what came before it.
"""

from collections.abc import Iterable, Iterator

from dailies_telemetry.parser import parse_line
from dailies_telemetry.schema import RenderEvent

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
    for line in lines:
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
