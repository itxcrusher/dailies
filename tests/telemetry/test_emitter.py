"""Tests for the OTLP emitter.

An ``InMemoryMetricReader`` stands in for the OTLP exporter, so these tests assert on
the same object the collector would receive without touching the network. What is
being pinned down is the mapping from a ``RenderEvent`` to instruments: which metric
fires for which event kind, and which label set each one carries. The names and the
label sets are the contract the Grafana dashboards and the agents' PromQL are written
against, so a rename that slips through here breaks a query somewhere with no import
to catch it.

``RenderEvent.demo()`` supplies the identity labels the model deliberately refuses to
default.
"""

from dailies_telemetry.emitter import RenderTelemetry
from dailies_telemetry.schema import (
    FRAME_LABELS,
    JOB_LABELS,
    METRICS,
    WORKER_LABELS,
    EventKind,
    Metric,
    RenderEvent,
)
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader


def collect(reader: InMemoryMetricReader) -> dict:
    """Flatten one reader collection into ``{metric name: metric}``.

    ``get_metrics_data`` returns ``None``, not an empty envelope, when no instrument
    has been touched, so an "emits nothing" assertion has to survive that.
    """
    data = reader.get_metrics_data()
    if data is None:
        return {}
    return {
        metric.name: metric
        for resource_metric in data.resource_metrics
        for scope_metric in resource_metric.scope_metrics
        for metric in scope_metric.metrics
    }


def make() -> tuple[RenderTelemetry, InMemoryMetricReader]:
    reader = InMemoryMetricReader()
    return RenderTelemetry(meter_provider=MeterProvider(metric_readers=[reader])), reader


def test_frame_complete_records_duration():
    tel, reader = make()
    tel.record(
        RenderEvent.demo(kind=EventKind.FRAME_COMPLETE, shot="SH010", frame=3, duration_seconds=8.0)
    )

    metrics = collect(reader)
    assert METRICS[Metric.FRAME_DURATION] in metrics
    point = metrics[METRICS[Metric.FRAME_DURATION]].data.data_points[0]
    assert point.sum == 8.0
    assert point.count == 1
    assert set(point.attributes) == set(FRAME_LABELS)
    assert point.attributes["shot"] == "SH010"
    assert point.attributes["frame"] == "3"


def test_oom_increments_frames_failed():
    tel, reader = make()
    tel.record(RenderEvent.demo(kind=EventKind.OOM, message="CUDA error: out of memory"))

    metrics = collect(reader)
    assert METRICS[Metric.FRAMES_FAILED] in metrics
    point = metrics[METRICS[Metric.FRAMES_FAILED]].data.data_points[0]
    assert point.value == 1
    # Job-level counter: no `frame`, no `worker`, or every failing frame opens its own
    # series and the "how far behind is this job" query has to aggregate them back.
    assert set(point.attributes) == set(JOB_LABELS)


def test_every_failure_kind_increments_frames_failed():
    """FRAME_FAILED, OOM and ENGINE_CRASH are all a lost frame; ASSET_MISSING is not.

    A missing asset is reported before the frame is attempted, so counting it here
    would double-count the FRAME_FAILED that follows it.
    """
    for kind in (EventKind.FRAME_FAILED, EventKind.OOM, EventKind.ENGINE_CRASH):
        tel, reader = make()
        tel.record(RenderEvent.demo(kind=kind, message="boom"))
        assert collect(reader)[METRICS[Metric.FRAMES_FAILED]].data.data_points[0].value == 1

    tel, reader = make()
    tel.record(RenderEvent.demo(kind=EventKind.ASSET_MISSING, message="missing tex.exr"))
    assert METRICS[Metric.FRAMES_FAILED] not in collect(reader)


def test_memory_is_recorded_on_any_kind_that_reports_it():
    tel, reader = make()
    tel.record(RenderEvent.demo(kind=EventKind.FRAME_START, memory_bytes=512_000_000))

    metrics = collect(reader)
    point = metrics[METRICS[Metric.WORKER_MEMORY]].data.data_points[0]
    assert point.value == 512_000_000
    assert set(point.attributes) == set(WORKER_LABELS)


def test_zero_memory_is_a_reading_not_a_missing_value():
    """Blender reports ``Mem:0.00M`` while synchronizing. Zero must still be emitted."""
    tel, reader = make()
    tel.record(RenderEvent.demo(kind=EventKind.FRAME_START, memory_bytes=0))

    assert collect(reader)[METRICS[Metric.WORKER_MEMORY]].data.data_points[0].value == 0


def test_event_without_payload_emits_nothing():
    tel, reader = make()
    tel.record(RenderEvent.demo(kind=EventKind.FRAME_START))

    assert collect(reader) == {}


def test_kind_deserialised_from_a_string_still_routes():
    """The routing uses enum identity, and events arrive over HTTP as JSON.

    If pydantic ever handed back a bare ``str`` for ``kind`` instead of coercing it to
    the member, every branch in ``record`` would go quiet and the dashboards would show
    a healthy, empty render farm. Cheap to pin, expensive to discover live.
    """
    tel, reader = make()
    tel.record(RenderEvent.demo(kind="frame_complete", duration_seconds=2.0))
    tel.record(RenderEvent.demo(kind="oom", message="out of memory"))

    metrics = collect(reader)
    assert metrics[METRICS[Metric.FRAME_DURATION]].data.data_points[0].sum == 2.0
    assert metrics[METRICS[Metric.FRAMES_FAILED]].data.data_points[0].value == 1
