"""The board API: the read surface over shot state.

Read-only on purpose. Everything that *changes* a shot - telemetry landing, the
investigator answering, the Guardian escalating - goes through the store directly on the
server side. Exposing a write route here would give the board a second way to set risk
that bypasses the agent that is supposed to decide it, and the first time the two
disagreed there would be no way to tell which one was right.

``create_app`` takes the store rather than reaching for a module-level one so the app has
no global state to leak between tests, and so a process can run two boards over two stores
without them seeing each other's shots. That is why there is no module-level ``app`` to
point a server at: a server takes the factory instead, and reaches the store it built
through ``app.state.shots``::

    uvicorn dailies_api.main:create_app --factory --port 8080
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from dailies_api.state import Shot, ShotStore

__all__ = ["Health", "ShotList", "create_app"]


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


def create_app(store: ShotStore | None = None) -> FastAPI:
    """Build the board API over ``store``.

    Args:
        store: The shot state to serve. Omit it for a fresh empty store, which is what a
            standalone run wants; pass one to share state with the render and agent side
            of the process.
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
