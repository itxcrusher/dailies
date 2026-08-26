"""Turn a Blender stdout stream into render events.

The per-line classification lives in ``dailies_telemetry.parser``; this module adds
the only thing a single line cannot know, which is what came before it.
"""

from collections.abc import Iterable, Iterator

from dailies_telemetry.parser import parse_line
from dailies_telemetry.schema import RenderEvent

__all__ = ["render_from_stream"]


def render_from_stream(lines: Iterable[str], shot: str, **identity: str) -> Iterator[RenderEvent]:
    """Turn a stream of Blender stdout lines into ``RenderEvent``s.

    Tracks the most recent frame number so events that do not carry one (warnings,
    crashes) are still attributed to the right frame.

    A generator, not a list: this is fed by a live render's stdout, so consumers
    have to see a failure as it is printed rather than when the job ends.

    ``identity`` is forwarded to the parser (``project``, ``sequence``,
    ``render_job``, ``worker``, and the descriptive ``renderer``/``scene``/
    ``priority``). Anything the caller omits lands in the parser's obviously-fake
    ``unknown`` series rather than in some other worker's.
    """
    frame_hint = 0
    for line in lines:
        event = parse_line(line, shot=shot, frame_hint=frame_hint, **identity)
        if event is None:
            continue
        if event.frame:
            frame_hint = event.frame
        else:
            # The parser applies the hint itself today, so this is belt-and-braces:
            # it keeps the stream correct if a future producer stops doing so.
            event.frame = frame_hint
        yield event
