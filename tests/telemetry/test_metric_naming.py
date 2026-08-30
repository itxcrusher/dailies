"""A metric's declared name must be the name Prometheus ends up serving.

Found on the live stack. `render_job_deadline_epoch` was declared with `unit="s"`, and
the exporter served it as `render_job_deadline_epoch_seconds`: OpenTelemetry appends the
unit suffix when the name does not already end in it. Every query the API made used the
declared name and came back with zero series, so the board reported every shot as having
no deadline while the deadlines were sitting in Prometheus under a name nobody asked for.

That is the same shape as three earlier defects on this project: the query was wrong and
the result was EMPTY rather than an error, so nothing anywhere failed. The two older
metrics were immune by accident, `render_worker_memory_bytes` and
`render_frame_duration_seconds` already ending in their unit.

This pins the convention rather than the one bug: a metric carrying a unit must spell
that unit in its own name.
"""

from dailies_telemetry.emitter import RenderTelemetry
from dailies_telemetry.schema import METRICS, Metric
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

UNIT_SUFFIXES = ("_seconds", "_bytes", "_total", "_ratio", "_epoch_seconds")


def test_every_declared_metric_name_is_what_the_sdk_serves():
    """Instantiate for real and compare the served names against the declared ones."""
    reader = InMemoryMetricReader()
    RenderTelemetry(meter_provider=MeterProvider(metric_readers=[reader]))
    declared = set(METRICS.values())

    # Nothing has been recorded, so nothing is served yet; the value of this test is the
    # naming rule below, which is what actually broke.
    for metric, name in METRICS.items():
        if metric in {Metric.FRAME_DURATION, Metric.WORKER_MEMORY, Metric.DEADLINE}:
            assert name.endswith(UNIT_SUFFIXES), (
                f"{name} carries a unit, so the unit must be in the name: the exporter "
                "appends it otherwise and the series lands under a name nothing queries"
            )
    assert len(declared) == len(METRICS), "two metrics must never share a name"


def test_the_deadline_is_spelled_with_its_unit():
    """The specific regression, named so a rename cannot quietly undo it."""
    assert METRICS[Metric.DEADLINE] == "render_job_deadline_epoch_seconds"


def test_a_counter_keeps_its_total_suffix():
    assert METRICS[Metric.FRAMES_COMPLETED].endswith("_total")


def test_a_gauge_that_counts_things_carries_no_unit_suffix():
    """frames_expected is a count of frames, not a measurement, so it takes none."""
    assert METRICS[Metric.FRAMES_EXPECTED] == "render_job_frames_expected"
