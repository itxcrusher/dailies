"""Shot state: what the board renders and what the Guardian writes back to.

This is the one place that says what a shot *is* to the rest of the system. The telemetry
package owns render events as they happen; this owns the current standing of each shot,
which is a different thing with a different lifetime. An event is a fact about one moment
and is never revised; a shot's risk is a running verdict that changes as frames land.

The store is deliberately in-memory and deliberately small. A render is watched for the
hours before a review, not archived, and the durable record of what happened already
exists in Prometheus and Loki. Putting a database behind this would add an operational
dependency for data that is reconstructible from telemetry, so the store's whole job is to
hold the current picture and hand it to the API.
"""

from __future__ import annotations

from enum import StrEnum
from threading import RLock
from typing import Any

from pydantic import BaseModel, Field

__all__ = ["Risk", "Shot", "ShotStore"]


class Risk(StrEnum):
    """How a shot stands against its deadline.

    A closed vocabulary, not free-text, because two consumers read it and neither can
    handle a surprise: the board colours a row by it and the Guardian decides whether to
    escalate by it. A model that answered ``"probably fine"`` would render as an unstyled
    row and escalate nothing, silently.

    ``StrEnum`` so the value serialises as its own name over JSON and a client compares
    against the string it already knows. The members are ordered least to most severe,
    which is what makes ``list(Risk).index`` a usable sort key on the board.

    - ``ON_TRACK``: projected to finish with slack in hand. The default.
    - ``WATCH``: still projected to make it, but the margin has narrowed enough that it
      is worth a human glance. Nothing is wrong yet.
    - ``AT_RISK``: projected to miss unless something changes. This is the state the
      Guardian acts on, because it is the last one where acting still helps.
    - ``CRITICAL``: missing now looks more likely than not, or the shot is failing
      outright rather than running slow.
    - ``MISSED``: the deadline has passed with the shot unfinished. Terminal, and kept
      distinct from ``CRITICAL`` so a post-mortem can tell a near miss from a real one.
    """

    ON_TRACK = "ON_TRACK"
    WATCH = "WATCH"
    AT_RISK = "AT_RISK"
    CRITICAL = "CRITICAL"
    MISSED = "MISSED"


class Shot(BaseModel):
    """One shot's current standing.

    ``frames_done`` is not validated against ``frames_total``. It is tempting to require
    ``frames_done <= frames_total``, but both numbers arrive from a live farm where a
    retried frame can be counted twice and a re-scoped shot can shrink its range
    mid-render. Rejecting that would drop the update that tells a supervisor something is
    wrong, which is the opposite of what this layer is for. Both are held at ``>= 0``,
    which is the only bound that is a wiring bug rather than a render oddity.
    """

    id: str = Field(min_length=1, description="Shot identifier, e.g. 'SH040'")
    frames_total: int = Field(ge=0, description="Frames in the shot's range")
    frames_done: int = Field(default=0, ge=0, description="Frames finished so far")
    risk: Risk = Field(
        default=Risk.ON_TRACK,
        description="Standing against the deadline. Optimistic until told otherwise.",
    )
    diagnosis: dict[str, Any] | None = Field(
        default=None,
        description=(
            "The investigator's last answer for this shot, or None if it has not been "
            "asked. Held as a plain mapping rather than a typed model because the "
            "authority on its shape is the agent's response schema, and duplicating "
            "that here would give the project two definitions to keep in step."
        ),
    )


class ShotStore:
    """The current standing of every shot being watched, keyed by shot id.

    Insertion-ordered: the board shows shots in the order the render submitted them,
    which is the order a supervisor already has in their head. Re-upserting an existing
    shot updates it in place rather than moving it to the end, so a row does not jump
    around the board every time a frame lands.

    The store owns its data. ``upsert`` stores a copy and the readers hand back copies,
    so a caller holding a :class:`Shot` cannot mutate the store through it and a reader
    iterating the board cannot be surprised mid-render. Changing a shot means upserting
    it, which is the only path that takes the lock.

    Locked because FastAPI runs sync route handlers in a threadpool, so two requests
    genuinely do touch this concurrently. Individual dict operations are atomic under the
    GIL, but ``all()`` copying every value while another thread upserts is not, and that
    is the read the board makes on every poll. ``RLock`` rather than ``Lock`` so that a
    later method built out of the existing ones (a bulk upsert, a "get or create") cannot
    deadlock against itself; nothing here re-enters today.
    """

    __slots__ = ("_lock", "_shots")

    def __init__(self) -> None:
        self._shots: dict[str, Shot] = {}
        self._lock = RLock()

    def upsert(self, shot: Shot) -> Shot:
        """Insert ``shot`` or replace the existing one with the same id.

        Returns the stored copy, so a caller that wants to read back what was written
        does not have to go through :meth:`get`.
        """
        stored = shot.model_copy(deep=True)
        with self._lock:
            self._shots[stored.id] = stored
        return stored.model_copy(deep=True)

    def all(self) -> list[Shot]:
        """Every shot, in the order it was first upserted."""
        with self._lock:
            return [shot.model_copy(deep=True) for shot in self._shots.values()]

    def get(self, shot_id: str) -> Shot | None:
        """The shot with this id, or ``None`` if it is not being watched.

        ``None`` rather than a raise: "no such shot" is an ordinary answer here (the
        board polls for shots that may not have started yet), and the HTTP layer is what
        turns it into a 404.
        """
        with self._lock:
            shot = self._shots.get(shot_id)
        return None if shot is None else shot.model_copy(deep=True)

    def __len__(self) -> int:
        """How many shots are being watched. The 404 path reports this."""
        with self._lock:
            return len(self._shots)
