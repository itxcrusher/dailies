"""Parse Blender CLI output into ``RenderEvent``s.

Pure text in, events out: no clock, no I/O, no state between calls. Everything the
system claims about a render (is this shot on pace, why did it fail, did it finish)
is downstream of these classifications, so the render-domain knowledge lives here and
the tests are correspondingly heavy.

Three rules earn their own note.

**Branch order is load-bearing.** A GPU out-of-memory message names the source file it
failed in, and a crash can be reported on the same line as the file it was reading, so
both would be claimed by the missing-asset pattern. The specific causes are therefore
matched first: OOM, then crash, then missing asset, and only then the healthy
progress/complete lines. An OOM misfiled as a missing asset sends a diagnosis agent
hunting for a file that is sitting right there on disk.

**The parser cannot know who is rendering.** A line of stdout carries a frame number
at best. ``RenderEvent`` deliberately refuses to default the identity labels, because
one worker's samples silently landing in another worker's series is worse than a loud
failure. So the caller passes what it knows, and anything it does not pass lands in an
obviously-fake ``unknown`` series rather than in some real worker's. That applies to
the descriptive labels too: the renderer, scene and priority are not recoverable from
a line of text either, so they are caller-supplied and default to the same sentinel
rather than to the model's plausible-looking ``cycles``/``Scene``/``normal``.

**A sentinel must not collide with a real reading.** A missing measurement is ``None``,
never zero: Blender legitimately reports ``Mem:0.00M`` while synchronizing, so a zero
memory reading has to stay a reading. The one surviving sentinel of that shape is
``frame_hint``, which is documented at the parameter.
"""

import re
from typing import Final, NamedTuple

from .schema import UNKNOWN, EventKind, RenderEvent

__all__ = ["UNKNOWN", "Progress", "parse_line"]

# "Fra:12 Mem:245.31M (Peak 512.00M) | Time:00:04.21 | Rendering 3 / 16 samples".
# Anchored on the current memory reading, not the peak in parentheses.
_FRA: Final = re.compile(r"Fra:(?P<frame>\d+)\s+Mem:(?P<mem>[\d.]+)(?P<unit>[KMG])")
# "Saved: '/out/SH010_0012.png'  Time: 00:04.55 (Saving: 00:00.03)". The first Time:
# is the frame's render time; the parenthesised one is the file write. Blender widens
# the field to HH:MM:SS once a frame passes the hour, so the hours group is optional
# and a two-field reading backtracks into mm:ss.
_SAVED: Final = re.compile(
    r"Saved:\s+'(?P<path>[^']+)'\s+Time:\s*(?:(?P<hh>\d+):)?(?P<mm>\d+):(?P<ss>[\d.]+)"
)
# Two shapes, because "missing" on its own is far too weak a signal. Blender's own
# phrasings ("Unable to open file", "Cannot read file") are specific enough to trust
# with a bare path, but the bare word appears all over healthy output ("has missing
# UVs", "dismissing the modal operator"), so it only counts when it is followed by a
# quoted path.
_MISSING: Final = re.compile(
    r"(?:Unable to open file|Cannot read file)\s+(?:'(?P<quoted>[^']+)'|(?P<bare>[^'\s]+))"
    r"|\bmissing\s+'(?P<named>[^']+)'",
    re.IGNORECASE,
)
# Cycles reports host and device exhaustion differently ("out of memory",
# "System is out of GPU memory"), and an allocation failure that escapes C++ surfaces
# as bad_alloc. The separator class covers the underscored driver constants that
# appear verbatim in CUDA/HIP logs (CUDA_ERROR_OUT_OF_MEMORY, OUT_OF_MEMORY).
_OOM: Final = re.compile(r"out[ _]of[ _](?:\w+[ _])?memory|std::bad_alloc", re.IGNORECASE)
_CRASH: Final = re.compile(
    r"(Segmentation fault|Error: engine|EXCEPTION_ACCESS_VIOLATION)", re.IGNORECASE
)

_UNIT: Final = {"K": 1024, "M": 1024**2, "G": 1024**3}
#: Blender names output files ``<base>_<frame>.<ext>``, ``<base>.<frame>.<ext>`` (the
#: dot-delimited convention most VFX pipelines standardise on), or bare
#: ``<frame>.<ext>`` when no filename prefix is configured. The leading separator is
#: required: without it the digits in a shot name ("SH010.png") would be read as
#: frame 10.
_FRAME_IN_PATH: Final = re.compile(r"[_./\\](\d+)\.\w+$")


class Progress(NamedTuple):
    """A ``Fra:`` reading: which frame is in flight and how much memory it is using."""

    frame: int
    memory_bytes: int


def _progress(line: str) -> Progress | None:
    """Return the ``Fra:`` reading on this line, if it has one.

    Cycles prefixes its error lines with the same reading, so this is useful on
    failure branches too, not only on healthy progress lines.
    """
    m = _FRA.search(line)
    if m is None:
        return None
    return Progress(int(m["frame"]), int(float(m["mem"]) * _UNIT[m["unit"]]))


def parse_line(
    line: str,
    shot: str,
    frame_hint: int = 0,
    *,
    project: str = UNKNOWN,
    sequence: str = UNKNOWN,
    render_job: str = UNKNOWN,
    worker: str = UNKNOWN,
    renderer: str = UNKNOWN,
    scene: str = UNKNOWN,
    priority: str = UNKNOWN,
) -> RenderEvent | None:
    """Parse one line of Blender CLI output into a ``RenderEvent``, or ``None``.

    ``frame_hint`` is the frame the caller believes is in flight; it is used when the
    line itself does not name one (most failure lines do not).
    """
    identity = {
        "shot": shot,
        "project": project,
        "sequence": sequence,
        "render_job": render_job,
        "worker": worker,
        "renderer": renderer,
        "scene": scene,
        "priority": priority,
    }
    progress = _progress(line)
    frame = progress.frame if progress else frame_hint

    # Specific causes first: see the branch-order note in the module docstring.
    if _OOM.search(line):
        return RenderEvent(
            kind=EventKind.OOM,
            frame=frame,
            # None, not zero: the engine did not report a reading on this line, and
            # zero is a reading Blender genuinely emits.
            memory_bytes=progress.memory_bytes if progress else None,
            message=line.strip(),
            **identity,
        )
    if _CRASH.search(line):
        return RenderEvent(
            kind=EventKind.ENGINE_CRASH, frame=frame, message=line.strip(), **identity
        )
    if m := _MISSING.search(line):
        return RenderEvent(
            kind=EventKind.ASSET_MISSING,
            frame=frame,
            asset_path=m["quoted"] or m["bare"] or m["named"],
            message=line.strip(),
            **identity,
        )
    if m := _SAVED.search(line):
        in_path = _FRAME_IN_PATH.search(m["path"])
        return RenderEvent(
            kind=EventKind.FRAME_COMPLETE,
            frame=int(in_path.group(1)) if in_path else frame,
            duration_seconds=int(m["hh"] or 0) * 3600 + int(m["mm"]) * 60 + float(m["ss"]),
            **identity,
        )
    if progress:
        # The Time: on a progress line is elapsed-so-far, not the frame's duration, so
        # it is deliberately dropped: only the Saved: line closes a frame.
        return RenderEvent(
            kind=EventKind.FRAME_START,
            frame=progress.frame,
            memory_bytes=progress.memory_bytes,
            **identity,
        )
    return None
