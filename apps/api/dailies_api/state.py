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

import re
from enum import StrEnum
from threading import RLock
from typing import Any, ClassVar

from pydantic import BaseModel, Field

__all__ = ["Risk", "Shot", "ShotStore"]

#: One component of a shot id: what a project, sequence, shot or job may be spelled with.
#: Deliberately narrow. These characters survive a URL path segment untouched, so an id
#: built from them reads the same in a log line, a browser address bar and a `curl`.
_ID_COMPONENT = re.compile(r"[A-Za-z0-9._-]+")

#: A whole shot id: one or more components joined by ``:``. Written out rather than
#: derived from ``_ID_COMPONENT`` because pydantic hands the pattern to a Rust regex
#: engine, and a pattern assembled at import time is one nobody can read in the schema.
#: A single component is legal so a bare label stays constructible in a test or a
#: fixture; production ids come from :meth:`Shot.make_id`.
_ID_PATTERN = r"^[A-Za-z0-9._-]+(?::[A-Za-z0-9._-]+)*$"


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

    Two of these answer a different question from the rest, and mixing them was a bug.
    ``ON_TRACK`` through ``MISSED`` FORECAST an unfinished shot: will it make its
    deadline. ``DELIVERED`` and ``LATE`` RECORD what happened to a finished one. A landed
    shot used to fall back to ``ON_TRACK`` because it carried no risk any more, which put
    a green ON TRACK pill directly above the words "delivered 22h 11m late" on the board.
    Both statements were true and the pair read as a contradiction, which costs more than
    either one being wrong: a supervisor who catches the board contradicting itself stops
    believing the pill everywhere else.

    - ``DELIVERED``: finished, and not known to have landed late. The calmest state, so
      that any real failure combined with it always wins.
    - ``LATE``: finished, and known to have landed after its deadline. Worse than
      ``AT_RISK``, where the bad outcome is still only forecast, and deliberately BELOW
      ``CRITICAL``. A shot can be finished and broken at once, and when it is, the
      rejected frame is the actionable news while the lateness is history. Ordering
      ``LATE`` above ``CRITICAL`` would let a late landing mask a failed render, which is
      the one thing the second-opinion seam exists to prevent.

    Declaration order is severity order, least to most severe, and :mod:`guardian` reads
    it directly as ``tuple(Risk)``. Inserting a member here changes how every verdict
    combines, so the position is part of the definition rather than a formatting choice.
    """

    DELIVERED = "DELIVERED"
    ON_TRACK = "ON_TRACK"
    WATCH = "WATCH"
    AT_RISK = "AT_RISK"
    LATE = "LATE"
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

    id: str = Field(
        min_length=1,
        pattern=_ID_PATTERN,
        description=(
            "The composite render identity, not the bare shot label: "
            "'project:sequence:shot:render_job', as built by Shot.make_id. Telemetry keys "
            "every render series by those four fields (dailies_telemetry.schema.RenderEvent), "
            "so keying the board by shot label alone would merge two jobs rendering the same "
            "shot into one row of mixed numbers. Path-safe characters only (A-Za-z0-9._-), "
            "joined by ':', because the id is addressed as a URL path segment."
        ),
    )
    frames_total: int = Field(ge=0, description="Frames in the shot's range")
    frames_done: int = Field(default=0, ge=0, description="Frames finished so far")
    risk: Risk = Field(
        default=Risk.ON_TRACK,
        description="Standing against the deadline. Optimistic until told otherwise.",
    )
    eta_epoch: int | None = Field(
        default=None,
        description=(
            "Absolute epoch second this shot is expected to finish, or None when no "
            "honest estimate exists yet. A shot that has rendered no frames has no "
            "observed rate to project, which is the ordinary state at the top of a "
            "render rather than an error."
        ),
    )
    deadline_epoch: int | None = Field(
        default=None,
        description=(
            "Absolute epoch second this shot is due, or None when nothing is promised. "
            "Declared by the render itself and carried through telemetry, so the board "
            "and the investigator read the same due date from the same place."
        ),
    )
    slack_seconds: int | None = Field(
        default=None,
        description=(
            "Room between the ETA and the deadline. Negative is a real answer and the "
            "most important one, so it is never clamped. None means there is no deadline "
            "or no estimate to measure against one; that is deliberately not zero, "
            "because zero means 'exactly on the wire', which is a claim rather than an "
            "absence."
        ),
    )
    confidence: str = Field(
        default="unknown",
        description=(
            "How steady the frame costs behind eta_epoch were: high, medium, low, or "
            "unknown when there is no estimate at all. This rates the ETA, not the "
            "investigator's diagnosis; the two share a word and measure different things."
        ),
    )
    visual: dict[str, Any] | None = Field(
        default=None,
        description=(
            "What Gemini saw when it looked at this shot's most recent frame, or None if "
            "nobody has looked. Held beside the diagnosis rather than inside it, because "
            "they are two independent sources and the interesting case is when they "
            "disagree: a picture that looks fine beside a log saying an asset was missing "
            "is a real finding, and folding one into the other would hide it."
        ),
    )
    answered_at: int | None = Field(
        default=None,
        description=(
            "Unix seconds when the answer beside this shot was produced, or null when "
            "nobody has asked. A diagnosis is a claim about a render at a moment, and the "
            "board cannot say how much to trust one without saying how old it is."
        ),
    )
    answer_stale: bool = Field(
        default=False,
        description=(
            "True when the stored answer came from a different agent than the one running "
            "now, so the board can show it without asserting the current agent stands "
            "behind it. See dailies_api.provenance."
        ),
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

    #: What :meth:`make_id` joins identity components with. Not one of the characters a
    #: component may contain, so an id built here can never be mistaken for a bare label.
    ID_SEPARATOR: ClassVar[str] = ":"

    #: The identity fields, in the order they appear in an id. The same four that
    #: telemetry keys a render series by; keeping the order fixed is what makes an id
    #: sortable into project/sequence order on the board.
    ID_FIELDS: ClassVar[tuple[str, ...]] = ("project", "sequence", "shot", "render_job")

    @classmethod
    def make_id(cls, project: str, sequence: str, shot: str, render_job: str) -> str:
        """Build the composite id for one shot of one render job.

        The bare shot label is not unique: a shot is routinely re-rendered while the
        previous job is still running, and two jobs writing to one store key would leave
        the board showing a single row of interleaved frame counts with no way to tell
        which job it belonged to. Joining the four fields telemetry already keys by makes
        the collision impossible instead of merely unlikely.

        Raises:
            ValueError: if a component is empty or carries a character an id cannot, the
                separator included. Refusing here keeps a malformed id from reaching the
                store, where it would only surface as a 404 on a shot the list endpoint
                is plainly showing.
        """
        parts = (project, sequence, shot, render_job)
        bad = [
            f"{name}={value!r}"
            for name, value in zip(cls.ID_FIELDS, parts, strict=True)
            if not _ID_COMPONENT.fullmatch(value)
        ]
        if bad:
            raise ValueError(
                "Shot id components must be non-empty and spelled with A-Za-z0-9._- only; "
                f"got {', '.join(bad)}"
            )
        return cls.ID_SEPARATOR.join(parts)


class ShotStore:
    """The current standing of every shot being watched, keyed by :attr:`Shot.id`.

    The key is the composite render identity, not the bare shot label, so re-rendering a
    shot while the previous job is still running gives the board two rows rather than one
    row of two jobs' numbers.

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

    def __bool__(self) -> bool:
        """Always ``True``: a store exists whether or not it holds shots yet.

        Without this, ``__len__`` alone would make an empty store falsy, and the
        ordinary ``store or ShotStore()`` idiom would silently discard a caller's
        real-but-empty store and substitute a different one. That is a live risk
        here because a store is legitimately empty at startup, before the first
        render is registered. ``ShotStore`` is a service object, not a value
        container, so its truthiness is its existence rather than its contents.
        Callers who want emptiness should ask ``len(store) == 0``.
        """
        return True
