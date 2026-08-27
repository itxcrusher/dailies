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
from dailies_telemetry.schema import UNKNOWN, EventKind, RenderEvent

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


FRAME_ZERO_SAMPLE = [
    "Fra:12 Mem:120.00M (Peak 200.00M) | Time:00:01.00 | Rendering 1 / 16 samples",
    "Fra:0 Mem:130.00M (Peak 200.00M) | Time:00:01.00 | Rendering 1 / 16 samples",
    "Error: engine not found 'CYCLES'",
]


def test_frame_zero_is_carried_forward_like_any_other_frame():
    """Regression: the wrapper tested the frame for truthiness, so 0 read as "unset".

    Frame 0 is legal (``frame: int | None = Field(ge=0)``, and Blender renders
    ``--frame-start 0`` for hold and reference frames). The truthiness test sent a
    genuine ``Fra:0`` line down the else branch, overwrote its correct frame with the
    stale hint, and then misattributed every following unnumbered warning and crash to
    that same stale frame - the one behaviour this wrapper exists to get right.
    """
    frames = [e.frame for e in render_from_stream(iter(FRAME_ZERO_SAMPLE), shot="SH010")]
    assert frames == [12, 0, 0]


def test_a_failure_before_the_first_frame_line_is_not_reported_as_frame_zero():
    """Scene-load failures print before any ``Fra:`` line and name no frame.

    Emitting them as frame 0 would make them indistinguishable from a genuine frame-0
    failure, which is the plausible-sentinel trap the parser's own rules forbid.
    """
    stream = ["Warning: Unable to open file '/assets/jacket_diffuse.exr'"]
    (event,) = render_from_stream(iter(stream), shot="SH010")
    assert event.kind is EventKind.ASSET_MISSING
    assert event.frame is None
    assert event.frame_labels()["frame"] == UNKNOWN


def test_the_wrapper_does_not_mutate_the_parsed_event():
    """The old else branch assigned through the model, bypassing validation entirely.

    ``RenderEvent`` has no ``validate_assignment``, so a post-construction write of
    ``frame`` was unchecked; nothing the wrapper does may depend on that.
    """
    events = list(render_from_stream(iter(SAMPLE), shot="SH010"))
    for event in events:
        assert RenderEvent.model_validate(event.model_dump()) == event


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

