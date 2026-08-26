"""Emit ``RenderEvent``s as OTLP metrics.

This is the only place that turns the event model into instruments, so it is where the
label sets get chosen. Each instrument takes the narrowest set the schema defines for
it rather than one flat label list:

* frame duration varies per worker, so it carries ``JOB_WORKER_LABELS``: every job axis
  plus ``worker``, and deliberately no ``frame``. A frame number is unique per
  observation, so labelling by it gives one histogram (sixteen bucket counters) per
  sample, growing without bound as frame numbers advance. Per-frame duration is still
  available where per-frame detail belongs: the ``RenderEvent`` stream itself;
* memory is a property of the worker, so ``WORKER_LABELS`` keeps one series per worker
  instead of one per worker-frame pair;
* the failure counter is job-level (its name says so) and carries ``FAILURE_LABELS`` -
  ``JOB_LABELS`` plus ``reason`` - so "how many frames has this job lost" is a single
  series read, and "to OOM or to engine crashes" is answerable at all. ``reason`` is
  bounded by ``FAILURE_KINDS``, three values.

Cardinality is the reason. A 200-frame shot on 20 workers is 4,000 series per metric
if everything takes the widest set, and the agents query these through the Grafana MCP
server on a live deadline. No instrument here takes ``FRAME_LABELS``; the same shot
costs 20 duration series and one failure series per reason instead.

Histogram buckets are chosen for render frames, not HTTP requests. The SDK default
tops out at 10s with most of its resolution below 1s, which would collapse a farm
whose typical frame is 2-10 minutes into a single bucket and make every p50/p95 read
"somewhere between 100 and 250 seconds". Boundaries are baked into the stored series,
so they are cheap now and a dashboard migration later.

Samples are stamped by the SDK at ``record`` time, so ``RenderEvent.timestamp`` is not
preserved: replaying a captured Blender log lands every sample at replay time, not at
the time the log says the frame rendered. Live parsing has negligible skew; a seed or
backfill path does not, and must not be read as historically accurate.

The class holds no state beyond its instruments and does no I/O: where the samples go
is the ``MeterProvider``'s business, which is what lets the tests hand it an in-memory
reader and the deployment hand it an OTLP exporter with nothing else changing.
"""

from typing import Final

from opentelemetry.metrics import MeterProvider

from .schema import METRICS, EventKind, Metric, RenderEvent

__all__ = ["FAILURE_KINDS", "FRAME_DURATION_BUCKETS_SECONDS", "RenderTelemetry"]

#: Histogram boundaries for frame duration, in seconds. Render-scale, not request-scale:
#: seconds for a preview frame through an hour for a heavy one, with the resolution
#: concentrated in the 1-20 minute band where production frames actually land.
FRAME_DURATION_BUCKETS_SECONDS: Final[tuple[float, ...]] = (
    1.0,
    5.0,
    15.0,
    30.0,
    60.0,
    120.0,
    300.0,
    600.0,
    1200.0,
    3600.0,
)

#: Event kinds that cost the job a frame. ``ASSET_MISSING`` is deliberately absent: it
#: is reported before the frame is attempted and is followed by its own
#: ``FRAME_FAILED``, so counting it here would report double the real loss and inflate
#: every delivery-risk estimate built on top of the counter.
FAILURE_KINDS: Final[frozenset[EventKind]] = frozenset(
    {EventKind.FRAME_FAILED, EventKind.OOM, EventKind.ENGINE_CRASH}
)


class RenderTelemetry:
    """Records render events onto OTLP instruments."""

    def __init__(self, meter_provider: MeterProvider) -> None:
        meter = meter_provider.get_meter("dailies.render")
        self._duration = meter.create_histogram(
            METRICS[Metric.FRAME_DURATION],
            unit="s",
            description="Wall-clock time to render one frame",
            explicit_bucket_boundaries_advisory=FRAME_DURATION_BUCKETS_SECONDS,
        )
        # "Current", not "peak": the parser reads Blender's `Mem:` field and ignores the
        # `(Peak ...)` figure beside it. This string is what Grafana shows next to the
        # metric, and someone sizing a worker off a number labelled peak that is not the
        # peak will under-provision it.
        self._memory = meter.create_gauge(
            METRICS[Metric.WORKER_MEMORY],
            unit="By",
            description="Current worker memory in use, sampled per frame",
        )
        self._failed = meter.create_counter(
            METRICS[Metric.FRAMES_FAILED],
            description="Frames that failed to render",
        )

    def record(self, event: RenderEvent) -> None:
        """Fan one event out to every instrument it carries a reading for.

        Not an if/elif chain: a single event can carry both a duration and a memory
        reading, and a failure line often carries the memory reading that explains it.
        """
        # The `is not None` duplicates a schema invariant (`_REQUIRED_BY_KIND` makes
        # duration_seconds mandatory on FRAME_COMPLETE). Kept deliberately: the model is
        # validated on build but not frozen, so this is defence against a field cleared
        # after construction, which would otherwise reach the SDK as a `None`.
        if event.kind is EventKind.FRAME_COMPLETE and event.duration_seconds is not None:
            self._duration.record(event.duration_seconds, event.job_worker_labels())

        # `is not None`, never truthiness: zero is a real reading. Blender reports
        # `Mem:0.00M` while synchronizing, and dropping it would leave a gap in the
        # series exactly where a memory investigation starts.
        if event.memory_bytes is not None:
            self._memory.set(event.memory_bytes, event.worker_labels())

        if event.kind in FAILURE_KINDS:
            self._failed.add(1, event.failure_labels())
