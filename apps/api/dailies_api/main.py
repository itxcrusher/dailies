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

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, Protocol

from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from dailies_api.annotate import should_annotate
from dailies_api.delivery import rate
from dailies_api.provenance import agent_fingerprint
from dailies_api.retry import is_transient_server_error, run_with_retry
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


class ShotSource(Protocol):
    """Where the board's rows come from.

    A protocol rather than the concrete class so a test can serve a fixed list, and so
    the API keeps no compile-time dependency on Grafana: the board is a view over
    whatever can answer "which shots exist and how far along are they".
    """

    async def list_shots(self) -> list[Shot]: ...


def build_shot_source(env: Mapping[str, str] | None = None) -> ShotSource | None:
    """A source reading live telemetry, or ``None`` if this deployment cannot reach it.

    ``None`` rather than an exception, because a board with no Grafana is a legitimate
    local run and should still serve its in-memory store. That is the opposite of the
    diagnose route's handling, where a missing MCP URL is a hard 503: there, staying
    quiet would render as a shot with no problems found, which is a false statement about
    the render. Here it renders as a board with no shots, which is simply the truth about
    an unconfigured deployment.
    """
    values = os.environ if env is None else env
    url = (values.get(MCP_URL_ENV) or "").strip()
    if not url:
        return None

    from .mcp_client import GrafanaMCP
    from .mcp_transport import connect
    from .shot_source import GrafanaShotSource

    prometheus_uid = (values.get(PROMETHEUS_UID_ENV) or "").strip() or None
    loki_uid = (values.get(LOKI_UID_ENV) or "").strip() or None

    class _PerRequest:
        """Opens a fresh MCP session per refresh.

        Not a long-lived session: Cloud Run freezes an instance between requests, and a
        socket held across that comes back dead in a way that surfaces as a slow failure
        on the next page load rather than a clean reconnect.
        """

        def __init__(self) -> None:
            self.telemetry: dict[str, Any] = {}

        async def _once(self) -> list[Shot]:
            async with connect(url) as session:
                grafana = GrafanaMCP(session, prometheus_uid=prometheus_uid, loki_uid=loki_uid)
                inner = GrafanaShotSource(grafana)
                shots = await inner.list_shots()
                # Carried up because the inner source is built per call and discarded;
                # the route reads `source.telemetry` off the long-lived wrapper.
                self.telemetry = inner.telemetry
                return shots

        async def list_shots(self) -> list[Shot]:
            """One read of the farm, asked twice if Grafana was briefly unavailable.

            The MCP server resolves the datasource by uid on every tool call, and Grafana
            Cloud intermittently answers that endpoint with a 503. Measured in production
            on 2026-09-02: the board rendered "the telemetry source could not be read" on
            one load and served three shots on the next, with nothing changed between them.
            Across four weeks of judging that is a page some judges simply find broken.

            Retries only a transient server error, never a 404 or a 401: a missing
            datasource stays missing and an expired token does not un-expire, and asking
            again for either spends a reader's wait to reach the same answer. The whole
            session is reopened rather than the query alone, because the failure is in
            resolving the datasource the session is about to use.
            """
            return await run_with_retry(
                self._once,
                # Two, and short. Someone is watching a page load; a board that takes eight
                # seconds to admit a problem is its own kind of failure.
                attempts=2,
                delay=0.75,
                retry_on=is_transient_server_error,
            )

    return _PerRequest()


#: How long a stored diagnosis is served instead of running a new investigation.
#:
#: The diagnose route is bound to ``allUsers`` because judges must reach it, every press
#: costs a Vertex call plus several Grafana queries, and shot ids are enumerable from the
#: public list route. Without this, anyone who can read the board can spend the project's
#: credits in a loop.
#:
#: Five minutes is chosen against the render, not the wallet: frames land on the order of
#: seconds to minutes, so a diagnosis younger than this is describing substantially the
#: same state and re-running it buys a differently-worded answer to the same question. It
#: is also the better demo behaviour, since pressing twice should show the answer at once
#: rather than spending a minute recomputing it.
DIAGNOSIS_COOLDOWN_SECONDS = 300


class Inspect(Protocol):
    """Looks at a shot's most recent frame and returns a verdict, or None."""

    async def __call__(self, shot_id: str) -> dict[str, Any] | None: ...


#: Marks the handler this module installed, so a second call replaces rather than adds.
_HANDLER_TAG = "dailies-api"


def configure_logging(env: Mapping[str, str] | None = None) -> None:
    """Make the quiet paths visible.

    Nothing configured a level, so the root logger sat at WARNING and every ``_log.info``
    in this codebase was discarded. That is not cosmetic. Twice while debugging Visual QA
    in production the deciding question was whether it had found a frame at all, the
    answer is logged at INFO by :func:`~dailies_api.frames.latest_frame`, and it was not
    in Cloud Logging.

    It matters here more than in most services because the visual check is deliberately
    non-fatal: a shot with no frame and a shot whose check blew up look identical from
    outside, and the log is the only thing that tells them apart.

    The level is read from the environment so a noisy incident can be turned down without
    rebuilding an image, and an unreadable value falls back rather than taking the
    service down over a typo in a variable.
    """
    values = os.environ if env is None else env
    wanted = (values.get("DAILIES_LOG_LEVEL") or "INFO").strip().upper()
    level = getattr(logging, wanted, None)
    if not isinstance(level, int):
        level = logging.INFO

    root = logging.getLogger()
    # Replace our own handler rather than adding another. Cloud Run can import the app
    # factory more than once, and a second handler duplicates every line in the log.
    for existing in list(root.handlers):
        if getattr(existing, "name", None) == _HANDLER_TAG:
            root.removeHandler(existing)

    handler = logging.StreamHandler()
    handler.name = _HANDLER_TAG
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(level)


def build_timeline_annotator(env: Mapping[str, str] | None = None):
    """The Grafana timeline writer, or ``None`` when MCP is not configured.

    Same shape and same environment as :func:`build_shot_source`: without an MCP URL there
    is no Grafana to write to, and a deployment in that state should serve a board rather
    than fail. Returning ``None`` rather than a no-op stand-in keeps that visible at the
    call site instead of hiding a silently discarded write behind an object that looks
    like it works.
    """
    values = os.environ if env is None else env
    url = (values.get(MCP_URL_ENV) or "").strip()
    if not url:
        _log.info("No MCP URL configured; findings will not reach the Grafana timeline")
        return None

    from .annotate import build_annotator
    from .mcp_transport import connect

    return build_annotator(
        connect,
        url,
        prometheus_uid=(values.get(PROMETHEUS_UID_ENV) or "").strip() or None,
        loki_uid=(values.get(LOKI_UID_ENV) or "").strip() or None,
    )


def build_answer_store():
    """Where answers are kept across restarts, or None when there is no bucket.

    None rather than an in-memory stand-in, so a deployment without storage forgets
    visibly rather than appearing to remember and losing everything on the next deploy.
    """
    try:
        from dailies_api.answers import AnswerStore
        from dailies_api.frames import bucket_name, gcs_answer_io

        bucket = bucket_name()
        if not bucket:
            _log.info("No bucket configured; answers will not survive a restart")
            return None
        write_object, read_object = gcs_answer_io(bucket)
        return AnswerStore(
            write_object=write_object,
            read_object=read_object,
            produced_by=agent_fingerprint(),
        )
    except Exception:  # noqa: BLE001 - storage setup must never break the API
        _log.exception("Could not set up the answer store; continuing without it")
        return None


def build_inspector():
    """The visual check reading the real bucket, or None when there is no bucket.

    None rather than a stub, so a deployment without frames simply has no visual verdict
    instead of an empty one. An empty verdict on the board would read as "we looked and
    saw nothing wrong", which is a claim, and the opposite of the truth.
    """
    try:
        from dailies_api.frames import bucket_name, gcs_reader, latest_frame
        from dailies_api.investigation import shot_label
        from dailies_api.visual_qa import check_frame, gemini_vision

        bucket = bucket_name()
        if not bucket:
            _log.info("No frames bucket configured; the visual check is off")
            return None

        from dailies_api.agent import INVESTIGATOR_MODEL

        model = gemini_vision(INVESTIGATOR_MODEL)
        list_objects, read_object = gcs_reader(bucket)

        async def inspect(shot_id: str) -> dict[str, Any] | None:
            frame = await latest_frame(
                shot_label(shot_id), list_objects=list_objects, read_object=read_object
            )
            if frame is None:
                return None
            verdict = await check_frame(
                frame.data, shot=shot_label(shot_id), model=model, path=frame.path
            )
            # The frame it judged, so a person can open the same image and disagree.
            return {**verdict, "frame": frame.path}

        return inspect
    except Exception:  # noqa: BLE001 - observability must never break the API
        _log.exception("Could not set up the visual check; continuing without it")
        return None


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
    telemetry_readable: bool = Field(
        default=True,
        description=(
            "Whether the last refresh could read the telemetry source. False means the "
            "shots below are whatever was already held and may be stale, and that an "
            "empty list says nothing about whether the farm is idle. Defaults true, "
            "because anything that cannot answer the question must not raise an alarm "
            "it has no evidence for."
        ),
    )


class Health(BaseModel):
    """The liveness answer. Deliberately says nothing about readiness."""

    ok: bool = True


def _build_agent_telemetry():
    """The investigator's own metrics pipeline, or None when this deployment has no OTLP.

    Wrapped rather than inlined so a failure to set up observability can never take down
    the API. Instrumentation that breaks the thing it measures is worse than none: the
    board would stop answering because a metrics exporter could not reach Grafana, which
    is a spectacular way to fail at reliability engineering.
    """
    try:
        from dailies_api.agent_telemetry import AgentTelemetry
        from dailies_api.otlp import build_meter_provider

        provider = build_meter_provider()
        if provider is None:
            return None
        return AgentTelemetry(provider)
    except Exception:  # noqa: BLE001 - see the docstring; this must never fail the app
        _log.exception("Could not set up the agent's own telemetry; continuing without it")
        return None


def create_app(
    store: ShotStore | None = None,
    *,
    allow_origins: Sequence[str] | None = None,
    diagnose: Diagnose | None = None,
    inspect: Inspect | None = None,
    shot_source: ShotSource | None = None,
    answers: Any = None,
    annotate: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
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
        inspect: What looks at a shot's most recent frame, returning a visual verdict or
            None when there is no frame. Omit it and one is built from the environment
            when a frames bucket is configured. A separate seam from ``diagnose`` because
            they are independent sources and must fail independently.
        answers: Where diagnoses and visual verdicts are kept so they survive a restart.
            Omit it and one is built from the environment when a bucket is configured.
            None means the board forgets on every deploy, which a local run may well
            want and a judge opening the hosted URL certainly does not.
        shot_source: Where the board's rows come from. Omit it and one is built from the
            environment when the deployment can reach Grafana, so the board populates
            itself from telemetry; pass one to serve a fixed list without a network.
            Pass nothing and configure nothing, and the in-memory store is served as
            before, which is what a local run without a Grafana wants.
    """
    # `is None`, not `or`: ShotStore defines __len__, so an empty store passed in by a
    # caller is falsy and `store or ShotStore()` would quietly swap it for a different
    # one. The bug would only show up once something upserted into the caller's store and
    # the board kept answering with an empty list.
    configure_logging()
    shots = ShotStore() if store is None else store
    # `is None` again, not `or`: a caller-supplied source must never be swapped out, and
    # building the default here rather than per request means an unconfigured deployment
    # does not re-read the environment on every page load to reach the same conclusion.
    source = build_shot_source() if shot_source is None else shot_source
    look = build_inspector() if inspect is None else inspect
    kept = build_answer_store() if answers is None else answers
    mark = build_timeline_annotator() if annotate is None else annotate
    # Per-app, never module-global, for the same reason the store is: two apps in one
    # process must not share a cooldown clock or a lock table.
    diagnosed_at: dict[str, float] = {}
    in_flight: dict[str, asyncio.Lock] = {}

    # Built once per app, not per request: a PeriodicExportingMetricReader owns a thread
    # and a flush timer, and one per diagnose call would leak both. None when the
    # deployment has no OTLP endpoint, which a local run legitimately does not.
    agent_telemetry = _build_agent_telemetry()

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
            # A cross-origin response's custom headers are invisible to the page unless
            # they are named here. The board reads these to tell a supervisor whether
            # pressing Re-run actually recomputed anything, and without this the button
            # would go back to looking broken for exactly the reason they were added.
            expose_headers=["X-Dailies-Answer", "X-Dailies-Answer-Age"],
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

    async def refresh() -> bool:
        """Fold whatever telemetry knows about into the store.

        An upsert per shot rather than replacing the store, because the two sides know
        different things and neither is complete on its own. Telemetry is authoritative
        on which shots exist and how far along they are; it has no idea what the
        investigator concluded. Overwriting the row wholesale would erase the diagnosis
        the instant the board polled again, which is the one thing on the page a
        supervisor is actually reading.

        A telemetry failure is swallowed deliberately. Grafana being briefly unreachable
        must not present as "no renders exist": that is indistinguishable, on the page,
        from a farm that has not started, and it is the more alarming of the two. The
        board keeps serving what it already had and the cause goes to the log.
        """
        if source is None:
            # Nothing wired, so nothing failed. An unconfigured deployment genuinely has
            # no shots, and reporting that as a telemetry failure would cry wolf on every
            # local run.
            return True
        try:
            discovered = await source.list_shots()
        except Exception:  # noqa: BLE001 - deliberate; see the docstring above
            # Broad on purpose, and not re-raised. Refreshing is best-effort decoration
            # on a read that must always answer: a Grafana hiccup, an expired token, a
            # protocol change upstream, none of them are reasons to fail a page load and
            # all of them present identically to a supervisor. The cause goes to Cloud
            # Logging with a traceback; the board keeps serving what it already holds.
            _log.exception("Could not refresh shots from telemetry; serving what is held")
            # Said out loud rather than swallowed. The board rebuilds from telemetry on
            # every load, so a failure here empties it, and an empty board that blames the
            # farm for being idle is a lie told to the one person who could act.
            return False
        # What telemetry knows beyond the frame counts: the due date and the observed
        # frame costs, per shot. A source that does not carry it (a test fake, or a
        # future source over something other than Grafana) simply rates on progress.
        detail = getattr(source, "telemetry", None) or {}

        for found in discovered:
            rated = rate(found, detail.get(found.id))
            held = shots.get(found.id)

            # Nothing in memory for this shot, so look for what a previous instance
            # concluded. Only then: what is in memory is newer than what is in the
            # bucket, and reading over it would replay a stale answer.
            if kept is not None and (held is None or held.diagnosis is None):
                previous = await kept.load(found.id)
                if previous:
                    rated = rated.model_copy(
                        update={
                            "diagnosis": previous.get("diagnosis"),
                            "visual": previous.get("visual"),
                            "answered_at": previous.get("saved_at"),
                            # Shown, not hidden. Dropping an answer the running agent did
                            # not produce would empty the board after every deploy, and a
                            # board with nothing on it teaches a visitor less than one that
                            # says plainly how old what it holds is.
                            "answer_stale": not previous.get("current", False),
                        }
                    )
            # Keep the fields telemetry cannot speak to, take the ones it owns.
            #
            # The provenance pair belongs to the first group and was missed. Telemetry has
            # no idea when a shot was diagnosed or by which agent, so leaving them out of
            # this merge silently reset them on the next poll: a shot diagnosed a second
            # ago rendered with no age beside its report, on the board, while every API
            # test passed because the diagnose route's own response was correct.
            shots.upsert(
                rated
                if held is None
                else rated.model_copy(
                    update={
                        "diagnosis": held.diagnosis,
                        "visual": held.visual,
                        "answered_at": held.answered_at,
                        "answer_stale": held.answer_stale,
                    }
                )
            )

        return True

    @app.get("/api/shots", response_model=ShotList, tags=["shots"])
    async def list_shots() -> ShotList:
        """Every shot being watched, refreshed from telemetry first."""
        readable = await refresh()
        return ShotList(shots=shots.all(), telemetry_readable=readable)

    async def watched(shot_id: str) -> Shot:
        """The shot, or the 404 both shot routes answer an unknown id with.

        One helper rather than two copies of the message: the detail route and the
        diagnose route are the same lookup, and a board that got two differently worded
        404s for the same id would look like two different failures.
        """
        shot = shots.get(shot_id)
        if shot is None:
            # Refresh before answering 404. A board that lists a shot and then 404s on
            # it is worse than an empty one, and on a cold instance the list route may
            # not have run yet in this process.
            await refresh()
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
    async def get_shot(shot_id: str) -> Shot:
        """One shot's current standing, or 404 if it is not being watched."""
        return await watched(shot_id)

    @app.post("/api/shots/{shot_id}/diagnose", response_model=Shot, tags=["shots"])
    async def diagnose_shot(shot_id: str, response: Response) -> Shot:
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

        Every 502 is logged before it is raised, and the transport's variant is logged
        *instead of* being returned verbatim. This route is bound to ``allUsers``, and a
        transport error's message carries the private MCP endpoint URL and up to 500
        characters of the upstream response body; the operator needs those and an
        anonymous caller does not.

        The last of those is caught broadly rather than by type, which is deliberate.
        Driven against a running server on 2026-08-29, a ``ValueError`` out of
        ``google-genai`` reached the caller as a bodyless 500 "Internal Server Error" -
        on the board, a button that does nothing, with the cause visible only in a
        server log nobody has open. The traceback is still logged; what changes is that
        the caller is told which failure they are looking at.

        Runs for as long as the investigation takes, which is several sequential Grafana
        queries plus a Gemini call. The Cloud Run service allows 600s for it.
        """
        shot = await watched(shot_id)
        run = diagnose
        if run is None:
            # Built per request, from the environment as the revision currently has it,
            # and imported here rather than at module scope: this module must keep
            # importing on the base install, while investigation reaches httpx and the
            # ADK. mcp_settings raises the 503 when the deployment is unconfigured.
            from dailies_api.investigation import build_diagnoser

            run = build_diagnoser(**mcp_settings(), telemetry=agent_telemetry)

        from dailies_api.investigation import InvestigationFailed
        from dailies_api.mcp_transport import MCPTransportError

        # One lock per shot, so a slow investigation of SH030 never delays SH040. The
        # lock is what the cooldown alone cannot give: a cooldown keyed on a stored
        # answer does nothing until the first one lands, and an investigation takes
        # minutes, so ten clicks in that window would be ten concurrent Vertex calls.
        lock = in_flight.setdefault(shot_id, asyncio.Lock())
        async with lock:
            # Re-checked inside the lock, not only outside it. A request that queued
            # behind a running investigation arrives here with the answer already
            # stored, and re-running would defeat the guard it just waited on.
            held = shots.get(shot_id)
            # "Complete" means every check that COULD have answered did. A previous
            # attempt whose visual check failed left a diagnosis with no verdict beside
            # it, and serving that for five minutes meant the visual check never ran
            # again and the shot looked permanently broken. The cooldown exists to save a
            # supervisor a Vertex call on a question already answered; an answer missing
            # half of itself is not one.
            complete = (
                held is not None
                and held.diagnosis is not None
                and (look is None or held.visual is not None)
            )
            if complete:
                # Absence is ``None``, never a default of 0.0, and the difference is not
                # pedantry. A missing entry means this instance has never served an answer
                # for this shot, and 0.0 is not a point in the distant past: it is the
                # monotonic clock's origin, which is per sandbox. A fresh Cloud Run
                # instance measured 44 seconds, so every shot restored from the answer
                # store sat inside the cooldown for the first five minutes of the
                # instance's life, refusing to diagnose anything after a cold start.
                served_at = diagnosed_at.get(shot_id)
                age = None if served_at is None else time.monotonic() - served_at
                if age is not None and age < DIAGNOSIS_COOLDOWN_SECONDS:
                    _log.info(
                        "Serving the diagnosis of %s stored %.0fs ago; cooldown is %ds",
                        shot_id,
                        age,
                        DIAGNOSIS_COOLDOWN_SECONDS,
                    )
                    # Said out loud, because a Re-run that silently returns the previous
                    # answer looks like a broken button. A supervisor who cannot tell
                    # "unchanged" from "nothing happened" will press it again, which is
                    # the behaviour the cooldown exists to prevent.
                    response.headers["X-Dailies-Answer"] = "cached"
                    response.headers["X-Dailies-Answer-Age"] = str(int(age))
                    return held

            try:
                diagnosis = await run(shot_id)
            except MCPTransportError as exc:
                # Logged, and deliberately not passed through. The transport's own messages
                # carry the private MCP endpoint URL and up to 500 characters of whatever the
                # far side answered with, including Cloud Run's raw 401/403 page. That is an
                # operator's diagnostic, and this route is bound to allUsers, so it goes to
                # Cloud Logging and the response says only which side failed.
                _log.warning("Investigating %s could not reach the MCP server: %s", shot_id, exc)
                raise HTTPException(
                    status_code=502,
                    detail=(
                        f"Investigating {shot_id!r} could not reach the telemetry MCP "
                        "server. The service log has the cause."
                    ),
                ) from exc
            except InvestigationFailed as exc:
                # The investigator's own sentence, about the model's answer rather than the
                # transport, so it names no internal host and is passed through as written.
                # Logged as well as returned: without this the response body was the only
                # copy of the cause, and it went to a browser and nowhere else.
                _log.warning("Investigating %s produced no usable diagnosis: %s", shot_id, exc)
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
            # Independent of the investigation and after it, so a bucket permission
            # problem or a vision quota error cannot turn a successful diagnosis into a
            # 502. The supervisor still gets the answer that worked, and the board says
            # nothing rather than something reassuring about the frame.
            visual: dict[str, Any] | None = None
            if look is not None:
                try:
                    visual = await look(shot_id)
                except Exception:  # noqa: BLE001 - see above; corroboration, not a gate
                    _log.exception("The visual check failed for %s; keeping the diagnosis", shot_id)

            current = shots.get(shot_id) or shot
            stored = shots.upsert(
                current.model_copy(
                    update={
                        "diagnosis": diagnosis,
                        "visual": visual,
                        # Just produced, by definition by the agent running now.
                        "answered_at": int(time.time()),
                        "answer_stale": False,
                    }
                )
            )
            if kept is not None:
                # After the upsert, and best-effort inside AnswerStore. Persisting is a
                # convenience; failing to write must never cost the supervisor the
                # answer they are looking at.
                await kept.save(shot_id, diagnosis=diagnosis, visual=visual)
            # After the answer is safely stored, and best-effort like the storage itself.
            # Only a found problem: see annotate.should_annotate for why a clean shot must
            # not spend a mark. A supervisor asked what is wrong with this shot, and a
            # failed write to a dashboard must not cost them the answer they asked for.
            if mark is not None and should_annotate(diagnosis):
                try:
                    await mark(shot_id, diagnosis)
                except Exception:  # noqa: BLE001 - corroboration, never a gate
                    _log.warning(
                        "Could not annotate the Grafana timeline for %s", shot_id, exc_info=True
                    )
            # Stamped only on a COMPLETE success. A failed investigation must stay
            # retryable at once: a 502 that also started a five-minute cooldown would
            # leave a transient Grafana outage looking like a permanently broken button.
            # And a diagnosis whose visual check failed is not a success worth caching,
            # for the same reason - it would pin the half-answer in place until it aged
            # out, which is exactly what happened to SH201 in production.
            if look is None or visual is not None:
                diagnosed_at[shot_id] = time.monotonic()
            response.headers["X-Dailies-Answer"] = "fresh"
            return stored

    return app
