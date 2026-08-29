"""The render log pipeline.

Metrics alone cannot express "the frame rendered successfully and the output is wrong".
That sentence is the entry's differentiator, and it lives in a log line: Blender exits 0
while printing a missing-texture warning. The investigator's diagnosis schema requires an
evidence citation for every claim, and for that class of failure the only available
evidence is the log.

A kernel OOM makes the same point from the other side: the process is SIGKILLed, so no
final metric flush happens, and whatever was already shipped line-by-line is all that
survives. That is why records are emitted as they are parsed rather than batched to the
end of the render.
"""

from dailies_telemetry.log_emitter import RenderLogEmitter
from dailies_telemetry.schema import EventKind, RenderEvent
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import InMemoryLogExporter, SimpleLogRecordProcessor


def _emitter() -> tuple[RenderLogEmitter, InMemoryLogExporter]:
    exporter = InMemoryLogExporter()
    provider = LoggerProvider()
    provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    return RenderLogEmitter(provider), exporter


def _event(kind: EventKind, **kw) -> RenderEvent:
    base = {
        "kind": kind,
        "shot": "SH030",
        "frame": 12,
        "project": "dailies",
        "sequence": "SEQ01",
        "render_job": "job-1",
        "worker": "worker-0",
    }
    base.update(kw)
    return RenderEvent(**base)


def test_an_asset_miss_is_emitted_as_a_log_record():
    """The beat the whole entry turns on: a warning that metrics cannot carry."""
    emitter, exporter = _emitter()
    emitter.record(
        _event(
            EventKind.ASSET_MISSING,
            message="Warning: Unable to open file '/assets/jacket_diffuse.exr'",
        )
    )
    emitter.flush()
    records = exporter.get_finished_logs()
    assert len(records) == 1
    body = records[0].log_record.body
    assert "jacket_diffuse.exr" in body


def test_the_record_carries_the_labels_an_investigator_filters_on():
    emitter, exporter = _emitter()
    emitter.record(_event(EventKind.ASSET_MISSING, message="Warning: missing 'x.exr'"))
    emitter.flush()
    attrs = exporter.get_finished_logs()[0].log_record.attributes
    for key in ("shot", "frame", "render_job", "worker", "event_kind"):
        assert key in attrs, f"{key} missing; the investigator cannot narrow to one shot"
    assert attrs["shot"] == "SH030"
    assert attrs["event_kind"] == "asset_missing"


def test_a_failure_is_more_severe_than_an_asset_miss():
    """Severity must let a query separate 'wrong output' from 'no output'."""
    from opentelemetry._logs import SeverityNumber

    emitter, exporter = _emitter()
    emitter.record(_event(EventKind.ASSET_MISSING, message="missing 'x.exr'"))
    emitter.record(_event(EventKind.OOM, message="out of memory"))
    emitter.flush()
    got = [r.log_record.severity_number for r in exporter.get_finished_logs()]
    assert got[0] is SeverityNumber.WARN
    assert got[1] is SeverityNumber.ERROR


def test_routine_progress_is_not_shipped_as_logs():
    """A 97-line render must not become 97 log lines; metrics already carry progress."""
    emitter, exporter = _emitter()
    for frame in range(1, 40):
        emitter.record(_event(EventKind.FRAME_START, frame=frame, memory_bytes=1024))
        emitter.record(_event(EventKind.FRAME_COMPLETE, frame=frame, duration_seconds=1.0))
    emitter.flush()
    assert list(exporter.get_finished_logs()) == []


def test_records_leave_as_they_are_parsed():
    """No batching: a SIGKILLed process never runs a final flush."""
    emitter, exporter = _emitter()
    emitter.record(_event(EventKind.OOM, message="out of memory"))
    # deliberately no flush() - a kernel kill would not reach one
    assert len(exporter.get_finished_logs()) == 1
