"""Finding the frame a shot most recently produced, so something can look at it.

The render job writes into a Cloud Storage bucket mounted at ``/frames``, one directory
per shot. This is the read side: the API pulls a frame's bytes and hands them to Gemini.

Both the listing and the read are injected. Neither belongs in a unit test, and the
interesting logic is not the storage call anyway - it is *which* frame gets looked at,
which is a real decision with a real wrong answer.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

__all__ = [
    "Frame",
    "bucket_name",
    "gcs_answer_io",
    "gcs_reader",
    "is_frame",
    "latest_frame",
    "newest_of",
]

_log = logging.getLogger(__name__)

#: Where the render job writes. Injected by Terraform; absent on a local run.
FRAMES_BUCKET_ENV = "DAILIES_FRAMES_BUCKET"

_TRAILING_NUMBER = re.compile(r"(\d+)(?!.*\d)")

#: Extensions that are a frame. Kept in step with visual_qa._MIME, which decides what
#: media type each one is sent to the model as.
_MIME_SUFFIXES: tuple[str, ...] = (".png", ".jpg", ".jpeg", ".webp")


@dataclass(frozen=True)
class Frame:
    """One rendered frame, with the path it came from so a verdict can cite it."""

    path: str
    data: bytes


def bucket_name(env: dict[str, str] | None = None) -> str | None:
    """The frames bucket, or ``None`` when this deployment has none."""
    values = os.environ if env is None else env
    return (values.get(FRAMES_BUCKET_ENV) or "").strip() or None


def is_frame(name: str) -> bool:
    """Whether an object in the bucket is actually a rendered frame.

    Two things in that directory are not. **Cloud Storage FUSE writes zero-byte directory
    placeholders**, so the bucket really contains ``SH200/`` beside
    ``SH200/frame_0001.png``; and a pipeline may drop a log or a sidecar next to the
    images.

    The placeholder is the one that bit. Its trailing number is 200, taken from the shot
    name, while the frame's is 1, taken from the zero-padded frame number, so the marker
    sorted highest and the API downloaded zero bytes. Gemini answered 400 "Provided image
    is not valid" on every shot, the failure was swallowed by design, and the board simply
    showed no visual verdict with nothing saying why.
    """
    return not name.endswith("/") and any(name.lower().endswith(ext) for ext in _MIME_SUFFIXES)


def newest_of(names: list[str]) -> str | None:
    """The highest-numbered frame in a list of object names.

    **Numerically, not lexically**, and the distinction is not hypothetical. Blender
    zero-pads, so string order happens to be right today; it stops being right the moment
    a pipeline writes ``frame_9.png`` beside ``frame_10.png``, and a board that quietly
    shows frame 9 of a forty-frame render because it sorted last is very hard to notice.

    Non-frames are filtered out first rather than ranked low, because ranking them low is
    what failed: a placeholder whose path happens to carry a bigger number than the frame
    number still won.

    Among real frames, one with no number sorts below every one that has one. A
    ``preview.png`` beside the frames is not the newest render, but it is also not a
    reason to return nothing when it is all there is.
    """
    frames = [name for name in names if is_frame(name)]
    if not frames:
        return None

    def key(name: str) -> tuple[int, int, str]:
        # Only the FILENAME's number counts. A number in the directory path - a shot
        # called SH200, a sequence called SEQ99 - has nothing to do with which frame is
        # newest, and letting it in is the same bug in a different coat.
        filename = name.rsplit("/", 1)[-1]
        match = _TRAILING_NUMBER.search(filename)
        return (1, int(match.group(1)), name) if match else (0, 0, name)

    return max(frames, key=key)


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


def gcs_reader(
    bucket: str,
) -> tuple[Callable[[str], Awaitable[list[str]]], Callable[[str], Awaitable[bytes]]]:
    """The production listing and read, against Cloud Storage.

    Returns the two callables :func:`latest_frame` takes, so the seam stays injectable
    and this is the only place that knows about buckets.

    ``google.cloud.storage`` is synchronous, so both calls are pushed to a thread. A
    blocking read inside the event loop would stall every other request on the instance,
    and the frames here are a megabyte each over a network.

    Imported inside the function for the same reason the ADK is: the read-only board
    routes must stay importable on an install without the ``agent`` extra.
    """
    from google.cloud import storage

    client = storage.Client()

    async def list_objects(prefix: str) -> list[str]:
        def _list() -> list[str]:
            return [b.name for b in client.list_blobs(bucket, prefix=prefix)]

        return await asyncio.to_thread(_list)

    async def read_object(name: str) -> bytes:
        def _read() -> bytes:
            return client.bucket(bucket).blob(name).download_as_bytes()

        return await asyncio.to_thread(_read)

    return list_objects, read_object


def gcs_answer_io(
    bucket: str,
) -> tuple[Callable[[str, bytes], Awaitable[None]], Callable[[str], Awaitable[bytes | None]]]:
    """Write and read the stored answers, against the same bucket the frames live in.

    Separate from :func:`gcs_reader` because the read here returns ``None`` for a missing
    object rather than raising. A shot nobody has asked about is the ordinary case, not
    an error, and making the caller catch NotFound to express that would put the normal
    path in an exception handler.
    """
    from google.cloud import storage
    from google.cloud.exceptions import NotFound

    client = storage.Client()

    async def write_object(name: str, data: bytes) -> None:
        def _write() -> None:
            client.bucket(bucket).blob(name).upload_from_string(
                data, content_type="application/json"
            )

        await asyncio.to_thread(_write)

    async def read_object(name: str) -> bytes | None:
        def _read() -> bytes | None:
            try:
                return client.bucket(bucket).blob(name).download_as_bytes()
            except NotFound:
                return None

        return await asyncio.to_thread(_read)

    return write_object, read_object
