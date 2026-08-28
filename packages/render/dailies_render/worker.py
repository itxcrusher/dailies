"""Run one Blender render and push what it prints to Grafana as OTLP metrics.

This is the render container's entrypoint. It is the only place in the project that
starts a process and talks to the network at the same time, so the two seams that make
it testable are kept explicit:

* :func:`build_command` turns a :class:`RenderRequest` into an argv list and does no
  I/O, so the CLI contract can be asserted without a Blender on the machine;
* :func:`run_command` takes the argv rather than building it, so a test can drive the
  whole stdout -> event -> instrument path with a process it controls.

**Identity is stamped here, not in the parser.** ``render_from_stream`` passes only the
shot through, because a line of Blender stdout cannot say which project, sequence, job
or worker produced it, and the schema deliberately refuses to guess (everything it is
not told lands in an obviously-fake ``unknown`` series). The container *does* know, from
its environment, so this module re-validates each event with those labels filled in
before it reaches the emitter. Without that, every metric this worker pushes would land
in ``project="unknown"`` and the investigator's PromQL would have nothing to filter on.

**Why the exit code matters.** Blender's return code is this process's return code. A
Cloud Run Job execution is marked failed by a non-zero exit, and the render job runs with
``max_retries = 0`` precisely so an induced failure stays failed and stays observable.
Swallowing the code here would report a successful execution for a render that produced
no frames.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import IO, Final

from dailies_telemetry.emitter import RenderTelemetry
from dailies_telemetry.schema import RenderEvent
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource

from dailies_render.blender import render_from_stream

__all__ = [
    "SERVICE_NAME",
    "RenderRequest",
    "build_command",
    "build_meter_provider",
    "main",
    "record_stream",
    "request_from_env",
    "run",
    "run_command",
]

#: What this worker calls itself to the collector. Grafana shows it as ``service_name``
#: on every series, which is how a render's metrics are told apart from the API's.
SERVICE_NAME: Final = "dailies-render"


@dataclass(frozen=True)
class RenderRequest:
    """One render: what to render, which frames, and whose series it belongs to.

    Frozen because it is read by both the command builder and the label stamper, and a
    request that changed between those two would put frames in one series and failures
    in another.
    """

    shot: str
    #: Frames are inclusive on both ends, which is what Blender's ``--frame-start`` /
    #: ``--frame-end`` mean. A single-frame render sets both to the same number.
    frame_start: int = 1
    frame_end: int = 1
    #: Blender's output pattern. ``####`` is Blender's frame-number placeholder; the
    #: parser reads the frame back out of the saved path, so the separator before the
    #: digits is load-bearing (``_FRAME_IN_PATH`` requires one).
    output: str = "/tmp/dailies/frame_####"
    blend_file: str | None = None
    #: A Blender-side Python script that builds the scene, for when there is no .blend
    #: to open. The image ships one so the container can render without an asset mount.
    scene_script: str | None = None
    engine: str = "CYCLES"
    project: str = "dailies"
    sequence: str = "SEQ01"
    render_job: str = "local"
    worker: str = "worker-0"
    scene: str = "Scene"
    priority: str = "normal"
    blender: str = "blender"

    @property
    def identity(self) -> dict[str, str]:
        """The label values the parser could not know, ready to stamp onto an event."""
        return {
            "project": self.project,
            "sequence": self.sequence,
            "render_job": self.render_job,
            "worker": self.worker,
            # `renderer` is the metric label; Blender calls the same thing the engine.
            # Lowercased because the label is compared against in PromQL, and CYCLES vs
            # cycles would be two series.
            "renderer": self.engine.lower(),
            "scene": self.scene,
            "priority": self.priority,
        }


def build_command(request: RenderRequest) -> list[str]:
    """The Blender argv for this request.

    Order is not cosmetic. Blender processes arguments in sequence: the parsing options
    and the .blend file have to precede ``--python`` (the script edits the scene that was
    opened) and the render options have to precede ``--render-anim`` (which renders
    whatever the preceding options configured).

    Raises:
        ValueError: if neither a .blend file nor a scene script is given. Blender would
            happily render its factory startup scene instead, and a container quietly
            rendering the default cube when its asset failed to mount is exactly the kind
            of green-but-wrong run this project exists to catch.
    """
    if request.blend_file is None and request.scene_script is None:
        raise ValueError(
            "A render needs either blend_file or scene_script; with neither, Blender "
            "renders its factory startup scene and the run looks successful."
        )
    if request.frame_end < request.frame_start:
        raise ValueError(
            f"frame_end ({request.frame_end}) is before frame_start ({request.frame_start})"
        )

    # "-noaudio" carries ONE dash. Blender mixes single-dash long options in with its
    # GNU-style ones, and it does not reject an unknown "--noaudio": it treats it as a
    # .blend path and fails with `Cannot read file "/app/--noaudio"`, which reads like a
    # missing asset rather than a bad flag. Verified against `blender --help` in the
    # image (Blender 4.5.9 LTS).
    argv = [request.blender, "--background", "--factory-startup", "-noaudio"]
    if request.blend_file is not None:
        argv.append(request.blend_file)
    if request.scene_script is not None:
        argv += ["--python", request.scene_script]
    argv += [
        "--render-output",
        request.output,
        "--engine",
        request.engine,
        "--frame-start",
        str(request.frame_start),
        "--frame-end",
        str(request.frame_end),
        "--render-anim",
    ]
    return argv


def request_from_env(env: Mapping[str, str] | None = None) -> RenderRequest:
    """Build a request from the container's environment.

    Every field has a default that renders *something*, because a Cloud Run Job execution
    with no overrides has to be a working smoke test rather than a crash. The identity
    fields are the exception worth reading twice: they default to a demo identity, so a
    real farm must set them or its series will be merged with the demo's.
    """
    env = os.environ if env is None else env

    def _int(name: str, default: int) -> int:
        raw = env.get(name)
        if raw is None or not raw.strip():
            return default
        try:
            return int(raw)
        except ValueError as exc:
            raise ValueError(f"{name} must be an integer, got {raw!r}") from exc

    return RenderRequest(
        shot=env.get("DAILIES_SHOT", "SH010"),
        frame_start=_int("DAILIES_FRAME_START", 1),
        frame_end=_int("DAILIES_FRAME_END", 1),
        output=env.get("DAILIES_OUTPUT", "/tmp/dailies/frame_####"),
        blend_file=env.get("DAILIES_BLEND_FILE") or None,
        scene_script=env.get("DAILIES_SCENE_SCRIPT") or None,
        engine=env.get("DAILIES_ENGINE", "CYCLES"),
        project=env.get("DAILIES_PROJECT", "dailies"),
        sequence=env.get("DAILIES_SEQUENCE", "SEQ01"),
        render_job=env.get("DAILIES_RENDER_JOB", "local"),
        # Cloud Run sets CLOUD_RUN_EXECUTION on a job execution, so the worker label is
        # the execution name by default: two executions of the same job are two workers,
        # which is what the label means.
        worker=env.get("DAILIES_WORKER") or env.get("CLOUD_RUN_EXECUTION") or "worker-0",
        scene=env.get("DAILIES_SCENE", "Scene"),
        priority=env.get("DAILIES_PRIORITY", "normal"),
        blender=env.get("DAILIES_BLENDER", "blender"),
    )


def build_meter_provider(resource_attributes: Mapping[str, str] | None = None) -> MeterProvider:
    """A provider that exports to the OTLP endpoint named in the environment.

    Configuration is left to the SDK's own ``OTEL_EXPORTER_OTLP_*`` variables rather than
    passed here, so the endpoint and the credential are set by the deployment (Terraform
    injects the first, the entrypoint assembles the second from Secret Manager) and never
    appear in this file.

    The export interval is short because a render job is short-lived and the interesting
    frames are the early ones; :func:`main` force-flushes on the way out regardless, which
    is what actually guarantees the last frame's sample leaves the process.
    """
    attributes = {"service.name": SERVICE_NAME, **(resource_attributes or {})}
    reader = PeriodicExportingMetricReader(OTLPMetricExporter(), export_interval_millis=15_000)
    return MeterProvider(metric_readers=[reader], resource=Resource.create(attributes))


def _stamp(event: RenderEvent, request: RenderRequest) -> RenderEvent:
    """Return ``event`` with the identity the parser could not know filled in.

    Re-validated rather than mutated: ``RenderEvent`` enforces a payload-per-kind rule in
    a model validator, and a copy that skipped it could carry a shape the emitter is
    written against but the schema forbids.
    """
    return RenderEvent.model_validate({**event.model_dump(), **request.identity})


def _tee(lines: Iterable[str], sink: IO[str] | None) -> Iterator[str]:
    """Yield each line after writing it out, so the raw render log is not swallowed.

    The container's stdout is what Cloud Logging captures, and a render that failed with
    no log is undiagnosable no matter how good the metrics are. Flushed per line because
    a job that is killed (an OOM is a kill) never gets to flush a buffer.
    """
    for line in lines:
        if sink is not None:
            sink.write(line if line.endswith("\n") else line + "\n")
            sink.flush()
        yield line


def record_stream(
    lines: Iterable[str],
    request: RenderRequest,
    telemetry: RenderTelemetry,
    *,
    echo: IO[str] | None = None,
) -> int:
    """Turn a Blender stdout stream into recorded metrics. Returns the event count."""
    recorded = 0
    for event in render_from_stream(_tee(lines, echo), shot=request.shot):
        telemetry.record(_stamp(event, request))
        recorded += 1
    return recorded


def run_command(
    argv: list[str],
    request: RenderRequest,
    telemetry: RenderTelemetry,
    *,
    echo: IO[str] | None = None,
) -> int:
    """Run ``argv``, record its output as it arrives, and return its exit code.

    ``stderr`` is folded into ``stdout`` because Blender splits a single render across
    both (progress on one, warnings and errors on the other) and the parser needs them
    interleaved in the order they were printed to attribute a failure to the frame that
    was in flight.

    Text mode with the default newline handling is load-bearing: Blender terminates its
    progress lines with a carriage return, and universal-newline translation is what
    turns that stream into the lines the parser expects instead of one enormous line.
    """
    process = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    if process.stdout is None:  # pragma: no cover - PIPE always yields a handle
        raise RuntimeError("subprocess produced no stdout pipe")
    with process.stdout as stream:
        record_stream(stream, request, telemetry, echo=echo)
    return process.wait()


def run(request: RenderRequest, telemetry: RenderTelemetry, *, echo: IO[str] | None = None) -> int:
    """Render ``request`` and record it. Returns Blender's exit code."""
    return run_command(build_command(request), request, telemetry, echo=echo)


def main(env: Mapping[str, str] | None = None) -> int:
    """Entrypoint: render what the environment describes, then flush the metrics.

    The flush is in a ``finally`` on purpose. A failed render is the run whose telemetry
    matters most, and a periodic reader that has not ticked yet loses every sample when
    the process exits.
    """
    request = request_from_env(env)
    provider = build_meter_provider()
    telemetry = RenderTelemetry(provider)
    try:
        return run(request, telemetry, echo=sys.stdout)
    finally:
        provider.force_flush()
        provider.shutdown()


if __name__ == "__main__":  # pragma: no cover - exercised by the container, not pytest
    sys.exit(main())
