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

#: Backwards-compatible alias for the widest label set.
LABELS: Final[tuple[str, ...]] = FRAME_LABELS


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
        EventKind.OOM: ("memory_bytes",),
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


class RenderEvent(BaseModel):
    kind: EventKind
    # Identity. Required: these decide which series the sample lands in.
    project: str
    sequence: str
    shot: str
    render_job: str
    worker: str
    frame: int = Field(ge=0)
    # Descriptive labels. Defaults here are safe; they do not identify the emitter.
    renderer: str = "cycles"
    scene: str = "Scene"
    priority: str = "normal"
    # Payload. Not labels.
    duration_seconds: float | None = Field(default=None, ge=0)
    memory_bytes: int | None = Field(default=None, ge=0)
    message: str | None = None
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
        return {name: str(getattr(self, name)) for name in names}

    def frame_labels(self) -> dict[str, str]:
        return self.labels(FRAME_LABELS)

    def job_labels(self) -> dict[str, str]:
        return self.labels(JOB_LABELS)

    def worker_labels(self) -> dict[str, str]:
        return self.labels(WORKER_LABELS)
