"""Render telemetry schema: metric names, label keys, and the render event model.

Metric names follow Prometheus/OpenMetrics conventions (lowercase, unit-suffixed,
`_total` reserved for monotonic counters) because they are scraped into Grafana and
queried by the agents through the Grafana MCP server. The OTLP exporter normalises
counter names by appending `_total`, so counters carry that suffix here too and the
constants match what Grafana actually stores.

Labels come in three granularities. A job-level gauge does not vary per frame, and
giving it a `frame` label would multiply its series count by the frame count, so the
label sets are kept separate rather than one flat list.
"""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from enum import Enum, StrEnum
from types import MappingProxyType
from typing import Final

from pydantic import BaseModel, Field, model_validator

#: Placeholder identity for a producer that did not supply one. Deliberately not a
#: plausible value: an ``unknown`` series in Grafana reads as a wiring bug, which is
#: what it is. It lives here rather than in the parser because every consumer
#: (exporter, dashboards, the diagnosis agent) has to test label values against it.
UNKNOWN: Final = "unknown"


class Metric(StrEnum):
    """Keys into ``METRICS``. An enum so call sites are statically checkable."""

    FRAME_DURATION = "frame_duration"
    FRAME_PROGRESS = "frame_progress"
    FRAMES_EXPECTED = "frames_expected"
    FRAMES_COMPLETED = "frames_completed"
    FRAMES_FAILED = "frames_failed"
    WORKER_MEMORY = "worker_memory"
    RETRY = "retry"
    QUEUE_WAIT = "queue_wait"
    DEADLINE_SLACK = "deadline_slack"


METRICS: Final[Mapping[Metric, str]] = MappingProxyType(
    {
        Metric.FRAME_DURATION: "render_frame_duration_seconds",
        Metric.FRAME_PROGRESS: "render_frame_progress_ratio",
        # A gauge: how many frames the job contains. `_total` is the counter suffix
        # and must not be used here.
        Metric.FRAMES_EXPECTED: "render_job_frames_expected",
        Metric.FRAMES_COMPLETED: "render_job_frames_completed_total",
        Metric.FRAMES_FAILED: "render_job_frames_failed_total",
        Metric.WORKER_MEMORY: "render_worker_memory_bytes",
        Metric.RETRY: "render_retry_total",
        Metric.QUEUE_WAIT: "render_queue_wait_seconds",
        Metric.DEADLINE_SLACK: "render_deadline_slack_seconds",
    }
)

#: Labels for per-frame metrics (frame duration, frame progress).
FRAME_LABELS: Final[tuple[str, ...]] = (
    "project",
    "sequence",
    "shot",
    "render_job",
    "frame",
    "worker",
    "renderer",
    "scene",
    "priority",
)

#: Labels for job-level metrics. No `frame`, no `worker`: a job-level series does not
#: vary along either axis.
JOB_LABELS: Final[tuple[str, ...]] = tuple(
    label for label in FRAME_LABELS if label not in {"frame", "worker"}
)

#: Labels for worker-level metrics (memory, retries).
WORKER_LABELS: Final[tuple[str, ...]] = ("render_job", "worker")

#: Labels for per-worker distributions (frame duration). Every job axis plus `worker`,
#: but never `frame`: a frame number is unique per observation, so including it gives
#: one series per sample and the series count grows without bound as the shot renders.
#: Dropping it turns "a 200-frame shot on 20 workers" from 4,000 series into 20, while
#: still answering both "is this shot on pace" and "is one worker dragging".
JOB_WORKER_LABELS: Final[tuple[str, ...]] = tuple(
    label for label in FRAME_LABELS if label != "frame"
)

#: Labels for the failure counter: the job axes plus WHY the frame was lost. Bounded
#: by ``FAILURE_KINDS`` (three values), and without it FRAME_FAILED, OOM and
#: ENGINE_CRASH collapse into one indistinguishable series, so "are we losing frames
#: to OOM or to engine crashes" becomes unanswerable. Adding a label to a live counter
#: re-partitions its series and breaks every rule written against it, so it belongs
#: here from the start rather than the first time someone needs the breakdown.
FAILURE_LABELS: Final[tuple[str, ...]] = (*JOB_LABELS, "reason")


class EventKind(str, Enum):
    FRAME_START = "frame_start"
    FRAME_COMPLETE = "frame_complete"
    FRAME_FAILED = "frame_failed"
    ASSET_MISSING = "asset_missing"
    OOM = "oom"
    ENGINE_CRASH = "engine_crash"


#: Which payload field each event kind is contractually required to carry.
_REQUIRED_BY_KIND: Final[Mapping[EventKind, tuple[str, ...]]] = MappingProxyType(
    {
        EventKind.FRAME_COMPLETE: ("duration_seconds",),
        # NOT memory_bytes: an OOM line does not always carry a reading, and zero is a
        # legal one (Blender reports `Mem:0.00M` while synchronizing), so requiring the
        # field would force a producer to invent a measurement at the exact moment
        # memory mattered most. `None` means "not reported" and the exporter skips it.
        EventKind.OOM: ("message",),
        EventKind.FRAME_FAILED: ("message",),
        EventKind.ASSET_MISSING: ("message",),
        EventKind.ENGINE_CRASH: ("message",),
    }
)

#: Values used only by the demo/seed path. They are deliberately NOT model defaults:
#: a worker that forgets to pass its own identity must fail, not merge its series
#: into another worker's.
_DEMO_IDENTITY: Final[Mapping[str, object]] = MappingProxyType(
    {
        "kind": EventKind.FRAME_START,
        "project": "demo",
        "sequence": "SEQ01",
        "shot": "SH010",
        "render_job": "job-1",
        "frame": 1,
        "worker": "worker-0",
    }
)


def _label_value(value: object) -> str:
    """Render one field as a label value, mapping "not known" onto ``UNKNOWN``.

    ``frame`` is the only label field that can be ``None`` (no ``Fra:`` line has been
    seen yet). ``str(None)`` would put a literal ``"None"`` into a Grafana series;
    ``UNKNOWN`` is the sentinel every consumer already tests against.
    """
    return UNKNOWN if value is None else str(value)


class RenderEvent(BaseModel):
    kind: EventKind
    # Identity. Required: these decide which series the sample lands in.
    project: str
    sequence: str
    shot: str
    render_job: str
    worker: str
    #: ``None`` means "no frame is known yet", not frame zero. Frame 0 is a legal
    #: frame (Blender renders ``--frame-start 0`` for hold and reference frames), so it
    #: cannot double as the sentinel: a failure printed before the first ``Fra:`` line
    #: (asset resolution at scene load) must not be reported as a genuine frame-0
    #: failure. Same rule as ``duration_seconds`` and ``memory_bytes`` below, and the
    #: same reason: a missing value must never be a plausible reading.
    frame: int | None = Field(ge=0)
    # Descriptive labels. Defaults here are safe; they do not identify the emitter.
    renderer: str = "cycles"
    scene: str = "Scene"
    priority: str = "normal"
    # Payload. Not labels.
    duration_seconds: float | None = Field(default=None, ge=0)
    memory_bytes: int | None = Field(default=None, ge=0)
    message: str | None = None
    #: The asset a failure names, when the line carried one. Structured so a consumer
    #: does not have to re-run the parser's regex over ``message`` to get it.
    asset_path: str | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def _check_payload_matches_kind(self) -> "RenderEvent":
        missing = [
            field
            for field in _REQUIRED_BY_KIND.get(self.kind, ())
            if getattr(self, field) is None
        ]
        if missing:
            raise ValueError(f"{self.kind.value} requires {', '.join(missing)}")
        return self

    @classmethod
    def demo(cls, **overrides: object) -> "RenderEvent":
        """Build an event with the demo identity, for seed scripts and fixtures."""
        return cls(**{**_DEMO_IDENTITY, **overrides})

    def labels(self, names: Sequence[str] = FRAME_LABELS) -> dict[str, str]:
        """Return a Prometheus label set for this event, values stringified.

        Raises ``AttributeError`` if a name is not a field: a label set and the model
        drifting apart is a bug, and it must surface here rather than as a mismatched
        registration far away in the exporter.
        """
        return {name: _label_value(getattr(self, name)) for name in names}

    def frame_labels(self) -> dict[str, str]:
        return self.labels(FRAME_LABELS)

    def job_labels(self) -> dict[str, str]:
        return self.labels(JOB_LABELS)

    def worker_labels(self) -> dict[str, str]:
        return self.labels(WORKER_LABELS)

    def job_worker_labels(self) -> dict[str, str]:
        return self.labels(JOB_WORKER_LABELS)

    def failure_labels(self) -> dict[str, str]:
        """Job labels plus the failure reason.

        ``reason`` is not a model field, so it is added here rather than through
        ``labels()``: the value is the event kind, and it is written as ``kind.value``
        so the attribute reads ``oom`` rather than ``EventKind.OOM``.
        """
        return {**self.job_labels(), "reason": self.kind.value}
