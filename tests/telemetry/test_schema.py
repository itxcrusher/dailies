import re
from datetime import UTC, datetime

import pytest
from dailies_telemetry.schema import (
    FRAME_LABELS,
    JOB_LABELS,
    LABELS,
    METRICS,
    WORKER_LABELS,
    EventKind,
    Metric,
    RenderEvent,
)
from pydantic import ValidationError

METRIC_NAME_RE = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
LABEL_NAME_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")

ALL_LABEL_SETS = {
    "FRAME_LABELS": FRAME_LABELS,
    "JOB_LABELS": JOB_LABELS,
    "WORKER_LABELS": WORKER_LABELS,
}


def valid_event(**overrides):
    """A fully-identified event; overrides let a test poke one field."""
    base = {
        "kind": EventKind.FRAME_COMPLETE,
        "project": "atlas",
        "sequence": "SEQ01",
        "shot": "SH010",
        "render_job": "job-1",
        "worker": "worker-3",
        "frame": 1,
        "duration_seconds": 12.5,
        "memory_bytes": 1024,
    }
    base.update(overrides)
    return RenderEvent(**base)


# --- metric and label names ---------------------------------------------------


def test_metric_names_are_prometheus_safe():
    for name in METRICS.values():
        assert name.startswith("render_")
        assert METRIC_NAME_RE.fullmatch(name), f"{name} is not a legal metric name"


@pytest.mark.parametrize("bad", ["render_frame-duration", "render_frame..d", "render/frame", "render frame"])
def test_metric_name_grammar_rejects_illegal_names(bad):
    """The old test passed all of these; the grammar check must not."""
    assert not METRIC_NAME_RE.fullmatch(bad)


def test_metric_names_are_unique():
    assert len(set(METRICS.values())) == len(METRICS)


def test_metric_keys_cover_the_metric_enum():
    assert set(METRICS) == set(Metric)
    # StrEnum keys stay reachable by their plain string value.
    assert METRICS["frame_duration"] == METRICS[Metric.FRAME_DURATION]


def test_counter_names_carry_total_and_gauges_do_not():
    assert METRICS[Metric.FRAMES_COMPLETED].endswith("_total")
    assert METRICS[Metric.FRAMES_FAILED].endswith("_total")
    assert METRICS[Metric.RETRY].endswith("_total")
    for gauge in (
        Metric.FRAMES_EXPECTED,
        Metric.FRAME_DURATION,
        Metric.FRAME_PROGRESS,
        Metric.WORKER_MEMORY,
        Metric.QUEUE_WAIT,
        Metric.DEADLINE_SLACK,
    ):
        assert not METRICS[gauge].endswith("_total")


def test_label_names_are_prometheus_safe():
    for set_name, labels in ALL_LABEL_SETS.items():
        for label in labels:
            assert LABEL_NAME_RE.fullmatch(label), f"{label} in {set_name} is illegal"


def test_label_sets_are_immutable_tuples():
    for labels in ALL_LABEL_SETS.values():
        assert isinstance(labels, tuple)
    with pytest.raises(TypeError):
        METRICS[Metric.RETRY] = "nope"


# --- label sets vs the model --------------------------------------------------


def test_every_label_is_a_model_field():
    """LABELS and the model must not drift apart."""
    for set_name, labels in ALL_LABEL_SETS.items():
        unknown = set(labels) - set(RenderEvent.model_fields)
        assert not unknown, f"{set_name} names non-fields: {unknown}"


def test_labels_raises_on_a_name_that_is_not_a_field():
    """Regression: labels() used to silently drop unknown keys."""
    with pytest.raises(AttributeError):
        valid_event().labels(("project", "gpu_model"))


def test_job_labels_drop_frame_and_worker():
    """A job-level series must not be multiplied by frame or worker."""
    assert "frame" not in JOB_LABELS
    assert "worker" not in JOB_LABELS
    assert set(valid_event().job_labels()) == set(JOB_LABELS)


def test_worker_labels_are_worker_scoped():
    assert set(WORKER_LABELS) == {"worker", "render_job"}
    assert valid_event(worker="worker-3").worker_labels()["worker"] == "worker-3"


def test_frame_labels_are_the_default_set():
    e = valid_event()
    assert set(e.labels()) == set(LABELS) == set(FRAME_LABELS)
    assert e.labels() == e.frame_labels()


def test_payload_fields_are_never_labels():
    for labels in ALL_LABEL_SETS.values():
        payload = {"kind", "duration_seconds", "memory_bytes", "message", "asset_path", "timestamp"}
        assert not set(labels) & payload


# --- the model ----------------------------------------------------------------


def test_render_event_requires_shot_and_frame():
    e = valid_event()
    assert e.shot == "SH010"
    assert e.labels()["shot"] == "SH010"


@pytest.mark.parametrize("field", ["project", "sequence", "render_job", "worker"])
def test_identity_fields_are_required(field):
    """No demo default may stand in for who emitted the event."""
    payload = {
        "kind": EventKind.FRAME_START,
        "project": "atlas",
        "sequence": "SEQ01",
        "shot": "SH010",
        "render_job": "job-1",
        "worker": "worker-3",
        "frame": 1,
    }
    payload.pop(field)
    with pytest.raises(ValidationError):
        RenderEvent(**payload)


def test_demo_helper_supplies_the_demo_identity():
    e = RenderEvent.demo()
    assert e.worker == "worker-0"
    assert RenderEvent.demo(worker="worker-9").worker == "worker-9"


@pytest.mark.parametrize(
    "overrides",
    [
        {"frame": -5},
        {"duration_seconds": -12.5},
        {"memory_bytes": -1},
    ],
)
def test_negative_numeric_fields_are_rejected(overrides):
    with pytest.raises(ValidationError):
        valid_event(**overrides)


@pytest.mark.parametrize(
    "kind, missing",
    [
        (EventKind.FRAME_COMPLETE, "duration_seconds"),
        # NOT memory_bytes: an OOM line does not always carry a reading, and zero is
        # a legal one, so the kind requires the raw line instead.
        (EventKind.OOM, "message"),
        (EventKind.FRAME_FAILED, "message"),
        (EventKind.ASSET_MISSING, "message"),
        (EventKind.ENGINE_CRASH, "message"),
    ],
)
def test_kind_requires_its_payload_field(kind, missing):
    with pytest.raises(ValidationError):
        valid_event(kind=kind, **{missing: None})


def test_an_oom_without_a_memory_reading_is_valid():
    """The engine does not always report one, and zero is a real reading, so the
    schema must not force a producer to invent a measurement."""
    valid_event(kind=EventKind.OOM, memory_bytes=None, message="out of memory")


def test_kinds_with_their_payload_validate():
    valid_event(kind=EventKind.OOM, memory_bytes=1024, message="killed")
    valid_event(kind=EventKind.FRAME_FAILED, message="engine returned 1")
    valid_event(kind=EventKind.FRAME_START, duration_seconds=None, memory_bytes=None)


def test_timestamp_defaults_to_an_aware_utc_now():
    before = datetime.now(UTC)
    e = valid_event()
    assert e.timestamp.tzinfo is not None
    assert before <= e.timestamp <= datetime.now(UTC)
    assert "timestamp" not in LABELS
