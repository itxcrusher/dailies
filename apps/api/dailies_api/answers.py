"""Keeping what the agents concluded, so a restart does not erase it.

Cloud Run replaces the instance on every deploy and scales to zero when idle, so the
in-memory :class:`~dailies_api.state.ShotStore` loses every diagnosis and every visual
verdict. The board a first-time visitor opens then shows a farm nobody has ever asked
about, and pressing Diagnose spends a Vertex call rediscovering something the system
already knew an hour ago.

**Object storage rather than a database, and the trade is deliberate.** There is already
a bucket, a client and IAM for it. A database would be a service to provision, a
dependency to declare and a connection pool to manage, for a workload that is one small
write per diagnosis and one read per shot by exact key. Cloud Storage is strongly
consistent for a read of a known object name, which is the only access pattern here.

What object storage is bad at is concurrent writers to one key, and that is fine: the
diagnose route already holds a per-shot lock, so two investigations of the same shot
cannot race. If this ever grows a second writer, that assumption is the thing to revisit
first.

Everything here is best-effort. Persistence is a convenience, not part of the answer, so
a storage failure costs the history and never the diagnosis the supervisor just asked
for.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

__all__ = ["ANSWER_PREFIX", "AnswerStore", "object_name"]

_log = logging.getLogger(__name__)

#: Where answers live inside the frames bucket.
#:
#: The same bucket as the frames on purpose: it already exists, already has the right IAM
#: and already carries a thirty-day lifecycle, which is a sensible age for a diagnosis
#: about a render whose frames are being deleted on the same schedule. A second bucket
#: would be another Terraform resource for one small prefix.
ANSWER_PREFIX = "answers/"

#: Anything outside this is replaced, so an id can never create a directory or escape the
#: prefix. Colons are legal in a Cloud Storage object name, but a flat readable key
#: survives an id scheme that later admits a slash, and a bucket listing stays browsable.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def object_name(shot_id: str) -> str:
    """The object a shot's answers are stored at."""
    return f"{ANSWER_PREFIX}{_UNSAFE.sub('_', shot_id)}.json"


class AnswerStore:
    """Reads and writes what the agents concluded about a shot."""

    def __init__(
        self,
        *,
        write_object: Callable[[str, bytes], Awaitable[None]],
        read_object: Callable[[str], Awaitable[bytes | None]],
    ) -> None:
        self._write = write_object
        self._read = read_object

    async def save(
        self,
        shot_id: str,
        *,
        diagnosis: dict[str, Any] | None,
        visual: dict[str, Any] | None,
    ) -> None:
        """Store the answers for one shot. Never raises.

        A failure here means the board forgets sooner than it should. It must not mean a
        supervisor loses the diagnosis they are looking at.
        """
        payload = {
            "shot_id": shot_id,
            # When it was answered. A diagnosis read back tomorrow is about the render as
            # it was, and a reader needs to know how old the answer is to trust it.
            "saved_at": int(time.time()),
            "diagnosis": diagnosis,
            "visual": visual,
        }
        try:
            await self._write(object_name(shot_id), json.dumps(payload).encode())
        except Exception:  # noqa: BLE001 - see the docstring; this is best-effort
            _log.warning("Could not persist the answers for %s", shot_id, exc_info=True)

    async def load(self, shot_id: str) -> dict[str, Any] | None:
        """What was concluded about this shot before, or ``None``.

        ``None`` covers three cases that need no distinguishing here: nobody has asked,
        the object is gone, or what is stored cannot be read. All three mean the board
        has nothing to show, and a half-written object must cost one shot's history
        rather than the page.
        """
        try:
            raw = await self._read(object_name(shot_id))
        except Exception:  # noqa: BLE001 - a read failure is not a reason to fail a page
            _log.warning("Could not read stored answers for %s", shot_id, exc_info=True)
            return None
        if not raw:
            return None
        try:
            payload = json.loads(raw.decode())
        except (ValueError, UnicodeDecodeError):
            _log.warning("Stored answers for %s are not readable JSON; ignoring", shot_id)
            return None
        return payload if isinstance(payload, dict) else None
