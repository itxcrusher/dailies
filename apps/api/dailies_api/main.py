"""The board API: the read surface over shot state.

Read-only on purpose. Everything that *changes* a shot - telemetry landing, the
investigator answering, the Guardian escalating - goes through the store directly on the
server side. Exposing a write route here would give the board a second way to set risk
that bypasses the agent that is supposed to decide it, and the first time the two
disagreed there would be no way to tell which one was right.

The board is served from its own origin (``apps/web`` is a separate Next.js app), so this
module also owns the CORS allow-list. It is read from the environment rather than written
here: see :func:`cors_origins`.

``create_app`` takes the store rather than reaching for a module-level one so the app has
no global state to leak between tests, and so a process can run two boards over two stores
without them seeing each other's shots. That is why there is no module-level ``app`` to
point a server at: a server takes the factory instead, and reaches the store it built
through ``app.state.shots``::

    uvicorn dailies_api.main:create_app --factory --port 8080
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from dailies_api.state import Shot, ShotStore

__all__ = [
    "CORS_ORIGINS_ENV",
    "DEFAULT_CORS_ORIGINS",
    "Health",
    "ShotList",
    "cors_origins",
    "create_app",
]

#: Where the browser origins allowed to read the board come from. Comma-separated, and
#: read from the environment rather than baked in because the board's origin differs per
#: deployment and a hardcoded production hostname is the kind of thing that gets copied
#: into a fork and quietly grants it access.
CORS_ORIGINS_ENV = "DAILIES_CORS_ORIGINS"

#: What a standalone run allows when the variable is unset: a Next.js dev server on this
#: machine, which is the only origin `apps/web` has before it is deployed anywhere. Both
#: spellings, because a browser sends the one that was typed and the two are different
#: origins to CORS even though they are the same server.
DEFAULT_CORS_ORIGINS: tuple[str, ...] = ("http://localhost:3000", "http://127.0.0.1:3000")


def cors_origins(env: Mapping[str, str] | None = None) -> list[str]:
    """The browser origins allowed to read the board.

    Args:
        env: Where to read ``DAILIES_CORS_ORIGINS`` from. Defaults to the process
            environment; a caller passes one to test the parsing without mutating it.

    An unset variable means "a local board", which is what a standalone run wants. An
    empty one means "no cross-origin reader", which is what a deployment that serves the
    board from the same origin wants, and is deliberately reachable: the alternative
    would be no way to switch CORS off short of an unset variable that means the opposite.
    """
    raw = (os.environ if env is None else env).get(CORS_ORIGINS_ENV)
    if raw is None:
        return list(DEFAULT_CORS_ORIGINS)
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


class ShotList(BaseModel):
    """The board's payload.

    An object wrapping the list, not a bare JSON array: a top-level array is the shape
    that cannot grow. The board will want a server timestamp and a deadline alongside the
    shots, and adding either to an array response is a breaking change while adding a key
    to an object is not.
    """

    shots: list[Shot] = Field(description="Every shot being watched, in submission order")


class Health(BaseModel):
    """The liveness answer. Deliberately says nothing about readiness."""

    ok: bool = True


def create_app(
    store: ShotStore | None = None,
    *,
    allow_origins: Sequence[str] | None = None,
) -> FastAPI:
    """Build the board API over ``store``.

    Args:
        store: The shot state to serve. Omit it for a fresh empty store, which is what a
            standalone run wants; pass one to share state with the render and agent side
            of the process.
        allow_origins: Browser origins allowed to read the board. Omit it to read
            :data:`CORS_ORIGINS_ENV` from the environment.
    """
    # `is None`, not `or`: ShotStore defines __len__, so an empty store passed in by a
    # caller is falsy and `store or ShotStore()` would quietly swap it for a different
    # one. The bug would only show up once something upserted into the caller's store and
    # the board kept answering with an empty list.
    shots = ShotStore() if store is None else store

    app = FastAPI(
        title="Dailies",
        version="0.1.0",
        summary="Delivery risk and diagnoses for shots on a render deadline",
    )
    app.state.shots = shots

    # The board at apps/web/ is a Next.js app on its own origin, so every read it makes is
    # cross-origin and a browser drops the response without one of these headers. Worth the
    # middleware because a CORS failure is the least diagnosable error class there is: the
    # request reaches the server, the server answers 200, and the only trace is a console
    # message in a browser nobody has open. An allow-list, never a wildcard, so the board's
    # origin is a deployment decision rather than an open door.
    origins = cors_origins() if allow_origins is None else list(allow_origins)
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            # Read-only surface: GET is every route there is, and OPTIONS is what the
            # middleware answers a preflight with. Naming them beats "*" so adding a write
            # route later is a deliberate edit here rather than something already allowed.
            allow_methods=["GET", "OPTIONS"],
            allow_headers=["*"],
            # No cookies and no Authorization on this API, so credentialed requests would
            # only widen what an allowed origin can do without buying the board anything.
            allow_credentials=False,
        )

    @app.get("/healthz", response_model=Health, tags=["ops"])
    def healthz() -> Health:
        """Liveness. Answers as soon as the process can serve, and checks nothing else."""
        return Health()

    @app.get("/api/shots", response_model=ShotList, tags=["shots"])
    def list_shots() -> ShotList:
        """Every shot being watched."""
        return ShotList(shots=shots.all())

    @app.get("/api/shots/{shot_id}", response_model=Shot, tags=["shots"])
    def get_shot(shot_id: str) -> Shot:
        """One shot's current standing, or 404 if it is not being watched."""
        shot = shots.get(shot_id)
        if shot is None:
            # Name the id and say how many shots exist. "Not found" alone leaves a caller
            # unable to tell a typo'd id from a board that is watching nothing yet, and
            # those two have completely different fixes. The ids themselves are left out:
            # a full render is hundreds of shots and that is an error body nobody reads.
            raise HTTPException(
                status_code=404,
                detail=(
                    f"No shot {shot_id!r} is being watched. "
                    f"The board currently holds {len(shots)} shot(s); "
                    "GET /api/shots lists them."
                ),
            )
        return shot

    return app
