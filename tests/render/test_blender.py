"""Tests for the Blender stream wrapper and the RenderBackend protocol.

The wrapper is driven with captured stdout rather than a real Blender so the suite
runs anywhere: on CI, on a laptop with no GPU, and in the container that ships the
adapter. Everything render-specific is already covered in ``tests/telemetry``; what
is under test here is the *streaming* behaviour on top of it (ordering, and the
frame number carrying forward onto lines that do not name one).
"""

from collections.abc import Iterable

import pytest
from dailies_render.backend import (
    BackendError,
    BackendUnavailable,
    JobNotFound,
    Priority,
    RenderBackend,
    TaskNotFound,
    UnsupportedOperation,
)
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
        self.priorities: list[tuple[str, Priority]] = []

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

    def change_priority(self, job_id: str, priority: Priority) -> None:
        self.priorities.append((job_id, priority))

    def get_output_frames(self, job_id: str) -> list[str]:
        # A URI, not a farm-local path: the contract is that a caller can resolve one
        # without knowing which backend produced it.
        return [f"file:///out/{job_id}_0001.png"]

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
        "get_output_frames",
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


# --- the error taxonomy -------------------------------------------------------


@pytest.mark.parametrize(
    "error", [JobNotFound, TaskNotFound, BackendUnavailable, UnsupportedOperation]
)
def test_every_declared_error_is_catchable_as_backend_error(error):
    """One `except BackendError` above the seam must be enough.

    If any of these sat outside the base, a caller would have to enumerate them and
    would silently miss the next one an adapter starts raising.
    """
    assert issubclass(error, BackendError)


def test_backend_error_is_an_exception_not_a_bare_class():
    assert issubclass(BackendError, Exception)


def test_the_taxonomy_distinguishes_missing_from_unreachable():
    """`JobNotFound` and `BackendUnavailable` must not be catchable as one another.

    They demand opposite recoveries: stop tracking the job, versus retry because
    nothing is actually known about it. Collapsing them is how a transient farm outage
    turns into a wrongly abandoned render.
    """
    assert not issubclass(JobNotFound, BackendUnavailable)
    assert not issubclass(BackendUnavailable, JobNotFound)


def test_a_partial_adapter_can_refuse_an_operation_without_leaking_its_vendor_error():
    """An adapter whose scheduler has no per-task retry has a declared way to say so."""

    class RetrylessBackend(FakeBackend):
        def retry_task(self, job_id: str, task_id: str) -> None:
            raise UnsupportedOperation("this scheduler cannot retry a single task")

    backend: RenderBackend = RetrylessBackend()
    with pytest.raises(BackendError):
        backend.retry_task("job-1", "task-1")


# --- the priority vocabulary --------------------------------------------------


def test_priority_is_a_shared_named_vocabulary():
    """The label a dashboard matches on and the command the agent sends must agree.

    `RenderEvent.priority` and `RenderBackend.change_priority` draw on the same enum,
    so "bump this job to high" means one thing on every farm.
    """
    assert [p.value for p in Priority] == ["low", "normal", "high", "urgent"]
    assert RenderEvent.demo().priority == Priority.NORMAL


def test_priority_values_are_strings_adapters_can_map():
    backend = FakeBackend()
    backend.change_priority("job-1", Priority.URGENT)
    assert backend.priorities == [("job-1", "urgent")]


def test_a_parsed_event_keeps_the_unknown_priority_sentinel():
    """`UNKNOWN` is deliberately outside the vocabulary: stdout never names a tier."""
    assert UNKNOWN not in {p.value for p in Priority}


# --- the output contract ------------------------------------------------------


def test_output_frames_are_resolvable_uris_not_farm_local_paths():
    """Two adapters returning `/out/x.png` and `gs://.../x.png` would both be "right".

    The consumer (the delivery board, the validation agent) cannot resolve a bare
    absolute path off the worker that wrote it, and no type error would catch the
    disagreement, so the contract is written down and tested.
    """
    (frame,) = FakeBackend().get_output_frames("job-1")
    assert "://" in frame


# Real Blender 4.5.9 output, captured from a render inside the render image on
# 2026-08-28. Blender prints the save across TWO lines. The single-line form that
# tests/telemetry/test_parser.py uses was invented when the plan was written and does
# not occur in practice, which is why 62 green parser tests still let
# render_frame_duration_seconds go silently missing from Grafana.
REAL_BLENDER_SAVE = [
    "Fra:1 Mem:16.55M (Peak 16.55M) | Time:00:00.27 | Remaining:00:00.01 | Mem:2.30M, Peak:2.30M | Scene, ViewLayer | Sample 8/8",
    "Fra:1 Mem:16.55M (Peak 16.55M) | Time:00:00.28 | Compositing | Tile 1/1",
    "Saved: '/tmp/dailies/frame_0001.png'",
    " Time: 00:00.76 (Saving: 00:00.48)",
]


def test_two_line_save_yields_frame_complete_with_duration():
    """Real Blender splits the save across two lines; both must close the frame.

    Regression for the defect that produced zero FRAME_COMPLETE events across 97 lines
    of genuine render output, leaving render_frame_duration_seconds absent from Grafana
    while FRAME_START kept flowing and the pipeline looked healthy.
    """
    events = list(render_from_stream(iter(REAL_BLENDER_SAVE), shot="SH010"))
    completes = [e for e in events if e.kind == EventKind.FRAME_COMPLETE]
    assert len(completes) == 1
    assert completes[0].frame == 1
    assert completes[0].duration_seconds == 0.76


def test_single_line_save_still_works():
    """The one-line form must keep working; some Blender builds emit it."""
    events = list(
        render_from_stream(
            iter(["Saved: '/out/SH010_0012.png'  Time: 00:04.55 (Saving: 00:00.03)"]),
            shot="SH010",
        )
    )
    completes = [e for e in events if e.kind == EventKind.FRAME_COMPLETE]
    assert len(completes) == 1
    assert completes[0].duration_seconds == 4.55


def test_saved_line_not_followed_by_time_does_not_hang_or_emit():
    """A dangling Saved: at end of stream must not emit a duration-less completion."""
    events = list(render_from_stream(iter(["Saved: '/tmp/x/frame_0003.png'"]), shot="SH010"))
    assert [e for e in events if e.kind == EventKind.FRAME_COMPLETE] == []
