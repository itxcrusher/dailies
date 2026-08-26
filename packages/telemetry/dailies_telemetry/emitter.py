"""Emit ``RenderEvent``s as OTLP metrics.

This is the only place that turns the event model into instruments, so it is where the
label sets get chosen. Each instrument takes the narrowest set the schema defines for
it rather than one flat label list:

* frame duration varies per frame and per worker, so it carries ``FRAME_LABELS``;
* memory is a property of the worker, so ``WORKER_LABELS`` keeps one series per worker
  instead of one per worker-frame pair;
* the failure counter is job-level (its name says so) and carries ``JOB_LABELS``, so
  "how many frames has this job lost" is a single series read, not a sum over every
  frame that ever failed.

Cardinality is the reason. A 200-frame shot on 20 workers is 4,000 series per metric
if everything takes the widest set, and the agents query these through the Grafana MCP
server on a live deadline.

The class holds no state beyond its instruments and does no I/O: where the samples go
is the ``MeterProvider``'s business, which is what lets the tests hand it an in-memory
reader and the deployment hand it an OTLP exporter with nothing else changing.
"""

from typing import Final

from opentelemetry.metrics import MeterProvider

from .schema import METRICS, EventKind, Metric, RenderEvent

__all__ = ["FAILURE_KINDS", "RenderTelemetry"]

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
        if event.kind is EventKind.FRAME_COMPLETE and event.duration_seconds is not None:
            self._duration.record(event.duration_seconds, event.frame_labels())

        # `is not None`, never truthiness: zero is a real reading. Blender reports
        # `Mem:0.00M` while synchronizing, and dropping it would leave a gap in the
        # series exactly where a memory investigation starts.
        if event.memory_bytes is not None:
            self._memory.set(event.memory_bytes, event.worker_labels())

        if event.kind in FAILURE_KINDS:
            self._failed.add(1, event.job_labels())
