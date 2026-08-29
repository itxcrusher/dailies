"""The board API: the read surface over shot state, plus the one button on it.

Read-only apart from ``POST /api/shots/{id}/diagnose``. Everything that *changes* a shot
- telemetry landing, the Guardian escalating - still goes through the store directly on
the server side. Exposing a write route for risk would give the board a second way to set
it that bypasses the agent supposed to decide it, and the first time the two disagreed
there would be no way to tell which one was right.

The diagnose route is the deliberate exception, and it is not that second way: it takes
no body, so a caller cannot say *what* is wrong with a shot, only ask. The answer is the
investigator's, produced from live Grafana telemetry through the MCP server, and the
route's whole job is to run it, keep it, and hand it back.

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

import logging
import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from dailies_api.state import Shot, ShotStore

__all__ = [
    "CORS_ORIGINS_ENV",
    "DEFAULT_CORS_ORIGINS",
    "LOKI_UID_ENV",
    "MCP_URL_ENV",
    "PROMETHEUS_UID_ENV",
    "Health",
    "ShotList",
    "cors_origins",
    "create_app",
    "mcp_settings",
]

#: Named after the module, so a Cloud Run log line says which surface failed.
_log = logging.getLogger(__name__)

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

#: The Grafana MCP server the investigator reads telemetry through. No default: it is a
#: per-deployment Cloud Run URL, and a wrong one is a service that either does not exist
#: or belongs to somebody else. Terraform sets it from the MCP service's own ``uri``.
MCP_URL_ENV = "DAILIES_MCP_URL"

#: Datasource UIDs for the metrics and log queries. Also without defaults, and for a
#: sharper reason than the URL: a UID is per-Grafana-stack, and a stale one does not
#: fail - it queries a datasource that exists and answers about something else.
PROMETHEUS_UID_ENV = "DAILIES_PROMETHEUS_UID"
LOKI_UID_ENV = "DAILIES_LOKI_UID"

#: What the diagnose route runs: a shot id in, a checked diagnosis out. Spelled out here
#: rather than imported from :mod:`dailies_api.investigation`, so that this module keeps
#: importing on the base install; that module reaches httpx and, through it, the ADK.
Diagnose = Callable[[str], Awaitable[dict[str, Any]]]


def mcp_settings(env: Mapping[str, str] | None = None) -> dict[str, str | None]:
    """Where the investigator reaches Grafana, read from the environment.

    Args:
        env: Where to read from. Defaults to the process environment; the route passes
            nothing, so a Cloud Run revision's variables are read per request rather
            than frozen into the app at import.

    Raises:
        HTTPException: 503, naming :data:`MCP_URL_ENV`, when it is unset or blank. This
            is the failure that must not be quiet. Without the MCP server there is no
            telemetry to read, and any softer handling - an empty diagnosis, a 200 with
            nothing in it - renders on the board as a shot with no problems found, which
            is the single most misleading thing this API could say.

    The two datasource UIDs are returned as ``None`` when unset rather than defaulted.
    :class:`dailies_api.mcp_client.GrafanaMCP` then raises a pointed error naming the one
    that is missing, which is a better outcome than a plausible-looking UID from another
    stack silently answering about the wrong data.
    """
    values = os.environ if env is None else env
    url = (values.get(MCP_URL_ENV) or "").strip()
    if not url:
        raise HTTPException(
            status_code=503,
            detail=(
                f"{MCP_URL_ENV} is not set, so this deployment cannot reach the Grafana "
                "MCP server and no shot can be diagnosed. This is a configuration "
                "problem with the API, not a finding about the shot."
            ),
        )
    return {
        "mcp_url": url,
        "prometheus_uid": (values.get(PROMETHEUS_UID_ENV) or "").strip() or None,
        "loki_uid": (values.get(LOKI_UID_ENV) or "").strip() or None,
    }


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
    diagnose: Diagnose | None = None,
) -> FastAPI:
    """Build the board API over ``store``.

    Args:
        store: The shot state to serve. Omit it for a fresh empty store, which is what a
            standalone run wants; pass one to share state with the render and agent side
            of the process.
        allow_origins: Browser origins allowed to read the board. Omit it to read
            :data:`CORS_ORIGINS_ENV` from the environment.
        diagnose: What the diagnose route runs. Omit it and one is built per request from
            the environment, against the live MCP server; pass one to run the route
            without a Grafana, a model or a network. Injected rather than
            module-global for the same reason ``store`` is: two apps in one process must
            not share it, and a test must not have to reach production to exercise a
            route.
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
            # Named rather than "*", so a new method reaching the board is a deliberate
            # edit here rather than something already allowed. POST is on the list for
            # exactly one route, the diagnose button, which the board triggers from the
            # browser and which a browser will not send without this.
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["*"],
            # No cookies and no Authorization on this API, so credentialed requests would
            # only widen what an allowed origin can do without buying the board anything.
            allow_credentials=False,
        )

    @app.get("/healthz", response_model=Health, tags=["ops"])
    def healthz() -> Health:
        """Liveness. Answers as soon as the process can serve, and checks nothing else.

        Unreachable on Cloud Run: its Google Frontend returns its own HTML 404 for
        ``/healthz`` and never forwards the request. Kept because the image also runs
        outside Cloud Run, where this is the conventional path. Use ``/api/health``
        for anything that must work in production.
        """
        return Health()

    @app.get("/api/health", response_model=Health, tags=["ops"])
    def api_health() -> Health:
        """Liveness on a path Cloud Run does not reserve.

        Verified against the deployed service: ``/healthz`` returns Google's HTML 404
        from the edge, while every other path reaches the container. This is the health
        endpoint to point uptime checks, runbooks and judges at.
        """
        return Health()

    @app.get("/api/shots", response_model=ShotList, tags=["shots"])
    def list_shots() -> ShotList:
        """Every shot being watched."""
        return ShotList(shots=shots.all())

    def watched(shot_id: str) -> Shot:
        """The shot, or the 404 both shot routes answer an unknown id with.

        One helper rather than two copies of the message: the detail route and the
        diagnose route are the same lookup, and a board that got two differently worded
        404s for the same id would look like two different failures.
        """
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

    @app.get("/api/shots/{shot_id}", response_model=Shot, tags=["shots"])
    def get_shot(shot_id: str) -> Shot:
        """One shot's current standing, or 404 if it is not being watched."""
        return watched(shot_id)

    @app.post("/api/shots/{shot_id}/diagnose", response_model=Shot, tags=["shots"])
    async def diagnose_shot(shot_id: str) -> Shot:
        """Investigate one shot against live telemetry and keep the answer.

        Runs the ADK investigator over the Grafana MCP server: it queries Prometheus and
        Loki through the tools, and answers with a cause bound to the queries that
        support it. The diagnosis is stored on the shot before it is returned, so the
        board shows it on its next poll rather than only to whoever pressed the button.

        Takes no request body. A diagnosis is something the system produces from
        telemetry, never something a caller supplies, and a route that accepted one would
        be a way to write a cause onto the board with no evidence behind it at all.

        Statuses, kept distinct because they need different people to fix them:

        - **404** the shot is not being watched. A typo, or a render that has not
          registered yet.
        - **503** this deployment is not configured to reach the MCP server. An operator
          problem, and explicitly *not* a statement about the shot.
        - **502** the investigation did not produce a diagnosis: the MCP server could not
          be reached, the answer did not carry its evidence, or the model side refused
          (a Vertex misconfiguration, a retired model id, a quota). Nothing is stored in
          any of those cases: a diagnosis nobody can check must not reach the board
          wearing the same styling as one that can.

        The last of those is caught broadly rather than by type, which is deliberate.
        Driven against a running server on 2026-08-29, a ``ValueError`` out of
        ``google-genai`` reached the caller as a bodyless 500 "Internal Server Error" -
        on the board, a button that does nothing, with the cause visible only in a
        server log nobody has open. The traceback is still logged; what changes is that
        the caller is told which failure they are looking at.

        Runs for as long as the investigation takes, which is several sequential Grafana
        queries plus a Gemini call. The Cloud Run service allows 600s for it.
        """
        shot = watched(shot_id)
        run = diagnose
        if run is None:
            # Built per request, from the environment as the revision currently has it,
            # and imported here rather than at module scope: this module must keep
            # importing on the base install, while investigation reaches httpx and the
            # ADK. mcp_settings raises the 503 when the deployment is unconfigured.
            from dailies_api.investigation import build_diagnoser

            run = build_diagnoser(**mcp_settings())

        from dailies_api.investigation import InvestigationFailed
        from dailies_api.mcp_transport import MCPTransportError

        try:
            diagnosis = await run(shot_id)
        except (InvestigationFailed, MCPTransportError) as exc:
            # Already a sentence a human can act on; passed through as written.
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except Exception as exc:
            # Everything else the investigation can throw. The type is part of the
            # message because an untyped failure's class is usually the fastest route
            # to its cause, and it is the one thing str(exc) leaves out.
            _log.exception("Investigating %s failed", shot_id)
            raise HTTPException(
                status_code=502,
                detail=(f"Investigating {shot_id!r} failed: {type(exc).__name__}: {exc}"),
            ) from exc

        # Re-read rather than reusing the shot fetched above: an investigation takes
        # minutes, and frames land during it. Writing back the copy taken at the start
        # would roll frames_done and risk back to where they were when the button was
        # pressed.
        current = shots.get(shot_id) or shot
        return shots.upsert(current.model_copy(update={"diagnosis": diagnosis}))

    return app
