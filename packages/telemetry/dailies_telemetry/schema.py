"""Render telemetry schema: metric names, label keys, and the render event model.

Metric names follow Prometheus conventions (lowercase, unit-suffixed) because they
are scraped into Grafana and queried by the agents through the Grafana MCP server.
"""

from enum import Enum

from pydantic import BaseModel

METRICS = {
    "frame_duration": "render_frame_duration_seconds",
    "frame_progress": "render_frame_progress_ratio",
    "frames_total": "render_job_frames_total",
    "frames_completed": "render_job_frames_completed",
    "frames_failed": "render_job_frames_failed",
    "worker_memory": "render_worker_memory_bytes",
    "retry_count": "render_retry_count",
    "queue_wait": "render_queue_wait_seconds",
    "deadline_slack": "render_deadline_slack_seconds",
}

LABELS = [
    "project",
    "sequence",
    "shot",
    "render_job",
    "frame",
    "worker",
    "renderer",
    "scene",
    "priority",
]


class EventKind(str, Enum):
    FRAME_START = "frame_start"
    FRAME_COMPLETE = "frame_complete"
    FRAME_FAILED = "frame_failed"
    ASSET_MISSING = "asset_missing"
    OOM = "oom"
    ENGINE_CRASH = "engine_crash"


class RenderEvent(BaseModel):
    kind: EventKind
    shot: str
    frame: int
    duration_seconds: float | None = None
    memory_bytes: int | None = None
    message: str | None = None
    project: str = "demo"
    sequence: str = "SEQ01"
    render_job: str = "job-1"
    worker: str = "worker-0"
    renderer: str = "cycles"
    scene: str = "Scene"
    priority: str = "normal"

    def labels(self) -> dict[str, str]:
        """Return the Prometheus label set for this event, values stringified."""
        return {k: str(getattr(self, k)) for k in LABELS if hasattr(self, k)}
