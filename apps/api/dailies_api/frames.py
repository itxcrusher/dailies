"""Finding the frame a shot most recently produced, so something can look at it.

The render job writes into a Cloud Storage bucket mounted at ``/frames``, one directory
per shot. This is the read side: the API pulls a frame's bytes and hands them to Gemini.

Both the listing and the read are injected. Neither belongs in a unit test, and the
interesting logic is not the storage call anyway - it is *which* frame gets looked at,
which is a real decision with a real wrong answer.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

__all__ = ["Frame", "bucket_name", "latest_frame", "newest_of"]

_log = logging.getLogger(__name__)

#: Where the render job writes. Injected by Terraform; absent on a local run.
FRAMES_BUCKET_ENV = "DAILIES_FRAMES_BUCKET"

_TRAILING_NUMBER = re.compile(r"(\d+)(?!.*\d)")


@dataclass(frozen=True)
class Frame:
    """One rendered frame, with the path it came from so a verdict can cite it."""

    path: str
    data: bytes


def bucket_name(env: dict[str, str] | None = None) -> str | None:
    """The frames bucket, or ``None`` when this deployment has none."""
    values = os.environ if env is None else env
    return (values.get(FRAMES_BUCKET_ENV) or "").strip() or None


def newest_of(names: list[str]) -> str | None:
    """The highest-numbered frame in a list of object names.

    **Numerically, not lexically**, and the distinction is not hypothetical. Blender
    zero-pads, so string order happens to be right today; it stops being right the moment
    a pipeline writes ``frame_9.png`` beside ``frame_10.png``, and a board that quietly
    shows frame 9 of a forty-frame render because it sorted last is very hard to notice.

    A name with no number in it sorts below every name that has one, rather than being
    dropped: a ``preview.png`` sitting in the directory is not a frame, but it is also
    not a reason to return nothing.
    """
    if not names:
        return None

    def key(name: str) -> tuple[int, int, str]:
        match = _TRAILING_NUMBER.search(name)
        return (1, int(match.group(1)), name) if match else (0, 0, name)

    return max(names, key=key)


async def latest_frame(
    shot: str,
    *,
    list_objects: Callable[[str], Awaitable[list[str]]],
    read_object: Callable[[str], Awaitable[bytes]],
) -> Frame | None:
    """The most recent frame this shot produced, or ``None`` if it has produced none.

    The newest rather than the first, deliberately. On a partial render the early frames
    are the ones that succeeded, and the question worth asking is what the farm was
    producing when it stopped.
    """
    names = await list_objects(f"{shot}/")
    newest = newest_of(names)
    if newest is None:
        _log.info("No frames for %s; nothing to look at", shot)
        return None
    return Frame(path=newest, data=await read_object(newest))
