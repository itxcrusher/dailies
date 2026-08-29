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

from dailies_telemetry.emitter import FRAME_DURATION_BUCKETS_SECONDS, RenderTelemetry
from dailies_telemetry.schema import (
    FAILURE_LABELS,
    JOB_LABELS,
    JOB_WORKER_LABELS,
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
    # Job axes plus `worker`, never `frame`: a frame number is unique per observation,
    # so labelling by it gives one histogram (sixteen bucket counters) per sample and
    # the series count climbs for as long as the shot renders.
    assert set(point.attributes) == set(JOB_WORKER_LABELS)
    assert point.attributes["shot"] == "SH010"
    assert "frame" not in point.attributes


def test_duration_series_do_not_multiply_with_the_frame_number():
    """The cardinality guarantee, asserted rather than described.

    Fifty frames off one worker must stay ONE series carrying fifty samples. This is
    the regression that matters: re-adding `frame` here is a one-word change that no
    other assertion in the file would catch, and label sets are the hardest part of a
    metric to change once dashboards are written against them.
    """
    tel, reader = make()
    for frame in range(50):
        tel.record(
            RenderEvent.demo(kind=EventKind.FRAME_COMPLETE, frame=frame, duration_seconds=120.0)
        )

    points = collect(reader)[METRICS[Metric.FRAME_DURATION]].data.data_points
    assert len(points) == 1
    assert points[0].count == 50


def test_duration_buckets_resolve_render_scale_frame_times():
    """The SDK default tops out at 10s; a render farm's frames are minutes long.

    Without explicit boundaries 2, 3 and 4 minute frames all land in one bucket and
    every p50/p95 the deadline-risk agent computes answers "somewhere between 100 and
    250 seconds", which is not an answer.
    """
    tel, reader = make()
    for seconds in (120.0, 180.0, 240.0):
        tel.record(RenderEvent.demo(kind=EventKind.FRAME_COMPLETE, duration_seconds=seconds))

    point = collect(reader)[METRICS[Metric.FRAME_DURATION]].data.data_points[0]
    assert tuple(point.explicit_bounds) == FRAME_DURATION_BUCKETS_SECONDS
    # 120 -> (60, 120], 180 -> (120, 300], 240 -> (120, 300]: three frames, two buckets.
    occupied = {i for i, count in enumerate(point.bucket_counts) if count}
    assert len(occupied) == 2


def test_oom_increments_frames_failed():
    tel, reader = make()
    tel.record(RenderEvent.demo(kind=EventKind.OOM, message="CUDA error: out of memory"))

    metrics = collect(reader)
    assert METRICS[Metric.FRAMES_FAILED] in metrics
    point = metrics[METRICS[Metric.FRAMES_FAILED]].data.data_points[0]
    assert point.value == 1
    # Job-level counter: no `frame`, no `worker`, or every failing frame opens its own
    # series and the "how far behind is this job" query has to aggregate them back.
    # Plus `reason`, which is the whole point of a failure counter.
    assert set(point.attributes) == set(FAILURE_LABELS)
    assert point.attributes["reason"] == "oom"


def test_every_failure_kind_increments_frames_failed():
    """FRAME_FAILED, OOM and ENGINE_CRASH are all a lost frame; ASSET_MISSING is not.

    A missing asset is reported before the frame is attempted, so counting it here
    would double-count the FRAME_FAILED that follows it.
    """
    for kind in (EventKind.FRAME_FAILED, EventKind.OOM, EventKind.ENGINE_CRASH):
        tel, reader = make()
        tel.record(RenderEvent.demo(kind=kind, message="boom"))
        point = collect(reader)[METRICS[Metric.FRAMES_FAILED]].data.data_points[0]
        assert point.value == 1
        assert point.attributes["reason"] == kind.value

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


def test_each_failure_reason_gets_its_own_series():
    """The diagnosis question is "OOM or engine crash", and it is a single query.

    All three kinds on one job must land in three distinguishable series, not one
    counter that says only "you lost three frames".
    """
    tel, reader = make()
    for kind in (EventKind.FRAME_FAILED, EventKind.OOM, EventKind.OOM, EventKind.ENGINE_CRASH):
        tel.record(RenderEvent.demo(kind=kind, message="boom"))

    points = collect(reader)[METRICS[Metric.FRAMES_FAILED]].data.data_points
    by_reason = {point.attributes["reason"]: point.value for point in points}
    assert by_reason == {"frame_failed": 1, "oom": 2, "engine_crash": 1}


def test_oom_carrying_a_memory_reading_hits_both_instruments():
    """The exact case the fan-out is not an if/elif chain for.

    ``parser.py`` builds precisely this event: an OOM whose line carried the `Mem:`
    reading that explains it. An if/elif regression would drop the memory sample and
    leave a memory investigation with a gap at the only moment that mattered.
    """
    tel, reader = make()
    tel.record(
        RenderEvent.demo(
            kind=EventKind.OOM,
            message="CUDA error: out of memory in cuMemAlloc",
            memory_bytes=8_000_000_000,
        )
    )

    metrics = collect(reader)
    assert metrics[METRICS[Metric.WORKER_MEMORY]].data.data_points[0].value == 8_000_000_000
    failure = metrics[METRICS[Metric.FRAMES_FAILED]].data.data_points[0]
    assert failure.value == 1
    assert failure.attributes["reason"] == "oom"


def test_memory_gauge_overwrites_rather_than_accumulates():
    """Last-value semantics, pinned.

    A gauge substituted for an UpDownCounter would leave this suite green while every
    memory series silently became a running total of every reading ever taken, and a
    worker sized off that number would be provisioned against a fiction.
    """
    tel, reader = make()
    tel.record(RenderEvent.demo(kind=EventKind.FRAME_START, memory_bytes=100))
    tel.record(RenderEvent.demo(kind=EventKind.FRAME_START, memory_bytes=200))

    points = collect(reader)[METRICS[Metric.WORKER_MEMORY]].data.data_points
    assert len(points) == 1
    assert points[0].value == 200


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


# --- job-level progress -------------------------------------------------------------
#
# The board reconstructs a shot's standing from Prometheus rather than from a store the
# API keeps in memory, so "how far along is this shot" has to exist as telemetry. Before
# these, METRICS declared render_job_frames_expected and render_job_frames_completed_total
# and nothing ever created the instruments: the names resolved in Python and the series
# did not exist in Grafana, which is the worst of both, because a PromQL query written
# against them returns an empty result rather than an error.


def test_frame_complete_increments_frames_completed():
    tel, reader = make()
    tel.record(
        RenderEvent.demo(kind=EventKind.FRAME_COMPLETE, shot="SH010", frame=1, duration_seconds=4.0)
    )
    tel.record(
        RenderEvent.demo(kind=EventKind.FRAME_COMPLETE, shot="SH010", frame=2, duration_seconds=5.0)
    )

    metrics = collect(reader)
    assert METRICS[Metric.FRAMES_COMPLETED] in metrics
    points = metrics[METRICS[Metric.FRAMES_COMPLETED]].data.data_points
    assert len(points) == 1, "both frames belong to one job series"
    assert points[0].value == 2


def test_frames_completed_is_job_scoped_not_per_frame():
    tel, reader = make()
    for frame in (1, 2, 3):
        tel.record(
            RenderEvent.demo(
                kind=EventKind.FRAME_COMPLETE, shot="SH010", frame=frame, duration_seconds=1.0
            )
        )

    point = collect(reader)[METRICS[Metric.FRAMES_COMPLETED]].data.data_points[0]
    # Same reasoning as the duration histogram: a frame number is unique per observation,
    # so labelling by it turns a counter into one series per frame and the progress query
    # has nothing to sum.
    assert set(point.attributes) == set(JOB_LABELS)
    assert "frame" not in point.attributes
    assert "worker" not in point.attributes


def test_a_failed_frame_is_not_a_completed_one():
    tel, reader = make()
    tel.record(RenderEvent.demo(kind=EventKind.FRAME_FAILED, shot="SH010", frame=1, message="boom"))

    metrics = collect(reader)
    assert METRICS[Metric.FRAMES_COMPLETED] not in metrics, (
        "a failure must not advance progress; the delivery estimate is built on this"
    )


def test_declaring_a_job_publishes_how_many_frames_it_holds():
    tel, reader = make()
    event = RenderEvent.demo(kind=EventKind.FRAME_START, shot="SH010", frame=1)

    tel.declare_job(frames_expected=48, labels=event.job_labels())

    metrics = collect(reader)
    assert METRICS[Metric.FRAMES_EXPECTED] in metrics
    point = metrics[METRICS[Metric.FRAMES_EXPECTED]].data.data_points[0]
    assert point.value == 48
    assert set(point.attributes) == set(JOB_LABELS)


def test_redeclaring_a_job_overwrites_rather_than_accumulates():
    tel, reader = make()
    labels = RenderEvent.demo(kind=EventKind.FRAME_START, shot="SH010", frame=1).job_labels()

    tel.declare_job(frames_expected=48, labels=labels)
    tel.declare_job(frames_expected=50, labels=labels)

    # A gauge, not a counter: a shot re-scoped mid-render holds 50 frames, not 98.
    point = collect(reader)[METRICS[Metric.FRAMES_EXPECTED]].data.data_points[0]
    assert point.value == 50
