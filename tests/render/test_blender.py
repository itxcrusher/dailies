"""Tests for the Blender stream wrapper and the RenderBackend protocol.

The wrapper is driven with captured stdout rather than a real Blender so the suite
runs anywhere: on CI, on a laptop with no GPU, and in the container that ships the
adapter. Everything render-specific is already covered in ``tests/telemetry``; what
is under test here is the *streaming* behaviour on top of it (ordering, and the
frame number carrying forward onto lines that do not name one).
"""

from collections.abc import Iterable

import pytest
from dailies_render.backend import RenderBackend
from dailies_render.blender import render_from_stream
from dailies_telemetry.schema import EventKind

SAMPLE = [
    "Blender 4.2.1",
    "Fra:1 Mem:120.00M (Peak 200.00M) | Time:00:01.00 | Rendering 1 / 16 samples",
    "Saved: '/out/SH010_0001.png'  Time: 00:02.50 (Saving: 00:00.02)",
    "Warning: Unable to open file '/assets/jacket_diffuse.exr'",
]


def test_render_from_stream_yields_events_in_order():
    events = list(render_from_stream(iter(SAMPLE), shot="SH010"))
    kinds = [e.kind for e in events]
    assert kinds == [EventKind.FRAME_START, EventKind.FRAME_COMPLETE, EventKind.ASSET_MISSING]


def test_frame_hint_carries_forward_to_unnumbered_events():
    events = list(render_from_stream(iter(SAMPLE), shot="SH010"))
    assert events[-1].frame == 1


def test_render_from_stream_is_lazy():
    """Consuming one event must not drain the whole stream.

    A render is a long-running process and the API streams its stdout; if the
    wrapper materialised the list, nothing downstream could react to a failure
    until the job ended, which is the entire point of watching it live.
    """
    stream = iter(SAMPLE)
    events = render_from_stream(stream, shot="SH010")
    first = next(events)
    assert first.kind is EventKind.FRAME_START
    # The generator stopped at the line it just yielded, so the tail is still unread.
    assert next(stream).startswith("Saved:")


def test_shot_reaches_every_event():
    events = list(render_from_stream(iter(SAMPLE), shot="SH010"))
    assert {e.shot for e in events} == {"SH010"}


def test_identity_labels_reach_every_event():
    """Identity passed to the wrapper must land on the events, not just the shot.

    Without this, every sample from every worker collapses into one ``unknown``
    series and per-worker questions ("is one worker dragging?") stop being
    answerable.
    """
    events = list(
        render_from_stream(
            iter(SAMPLE),
            shot="SH010",
            project="atlas",
            sequence="SEQ01",
            render_job="job-7",
            worker="worker-3",
        )
    )
    assert events
    for event in events:
        assert (event.project, event.sequence, event.render_job, event.worker) == (
            "atlas",
            "SEQ01",
            "job-7",
            "worker-3",
        )


class FakeBackend:
    """Minimal reference implementation of ``RenderBackend``.

    Deliberately structural: it does not inherit from ``RenderBackend``, so this is
    also the proof that a future Flamenco/OpenCue/Deadline adapter only has to match
    the shape. Future adapters should be read against this.
    """

    def __init__(self) -> None:
        self.retried: list[tuple[str, str]] = []
        self.cancelled: list[tuple[str, str]] = []
        self.priorities: list[tuple[str, str]] = []

    def list_jobs(self) -> list[dict]:
        return [{"id": "job-1", "shot": "SH010", "state": "running"}]

    def get_job(self, job_id: str) -> dict:
        return {"id": job_id, "shot": "SH010", "state": "running"}

    def get_tasks(self, job_id: str) -> list[dict]:
        return [{"id": "task-1", "job": job_id, "frame": 1, "state": "running"}]

    def retry_task(self, job_id: str, task_id: str) -> None:
        self.retried.append((job_id, task_id))

    def cancel_task(self, job_id: str, task_id: str) -> None:
        self.cancelled.append((job_id, task_id))

    def change_priority(self, job_id: str, priority: str) -> None:
        self.priorities.append((job_id, priority))

    def get_output(self, job_id: str) -> list[str]:
        return [f"/out/{job_id}_0001.png"]

    def get_logs(self, job_id: str) -> Iterable[str]:
        return iter(SAMPLE)


class PartialBackend:
    """An adapter that forgot half the protocol. Must not pass as a backend."""

    def list_jobs(self) -> list[dict]:
        return []

    def get_job(self, job_id: str) -> dict:
        return {}


def test_fake_backend_satisfies_the_protocol_structurally():
    assert isinstance(FakeBackend(), RenderBackend)


def test_incomplete_adapter_does_not_satisfy_the_protocol():
    assert not isinstance(PartialBackend(), RenderBackend)


def test_backend_declares_the_full_scheduler_surface():
    """The protocol is the contract every adapter is written against.

    Naming the members explicitly means dropping one from the protocol is a failing
    test rather than a silently narrower interface that adapters quietly stop
    implementing.
    """
    declared = {name for name in vars(RenderBackend) if not name.startswith("_")}
    assert declared == {
        "list_jobs",
        "get_job",
        "get_tasks",
        "retry_task",
        "cancel_task",
        "change_priority",
        "get_output",
        "get_logs",
    }


def test_backend_annotation_accepts_the_fake():
    """A function annotated with the protocol takes the fake, unmodified."""

    def frames_from(backend: RenderBackend, job_id: str, shot: str) -> list[int]:
        return [e.frame for e in render_from_stream(backend.get_logs(job_id), shot=shot)]

    assert frames_from(FakeBackend(), "job-1", "SH010") == [1, 1, 1]


def test_protocol_cannot_be_instantiated():
    with pytest.raises(TypeError):
        RenderBackend()  # type: ignore[abstract]
