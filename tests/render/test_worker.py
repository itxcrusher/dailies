"""Tests for the render container's entrypoint.

Two things are worth pinning here and neither needs a Blender installed.

The **argv** is a contract with a CLI whose argument order changes what it does: the
scene script has to run after the file is opened and before the render starts, or it
edits nothing. Asserting on the list catches a reordering that no unit of Python would.

The **stdout path** is driven with a real subprocess (this interpreter, printing captured
Blender lines) rather than a list of strings, because the thing most likely to break in
the container is not the parsing - that is covered in ``tests/telemetry`` - but the
plumbing around it: text mode, the pipe, the exit code, and the identity labels that the
parser cannot know and the worker has to stamp on.
"""

import sys

import pytest
from dailies_render.worker import (
    RenderRequest,
    build_command,
    record_stream,
    request_from_env,
    run_command,
)
from dailies_telemetry.emitter import RenderTelemetry
from dailies_telemetry.schema import METRICS, UNKNOWN, Metric, RenderEvent
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

SAMPLE = [
    "Blender 4.5.9",
    "Fra:1 Mem:120.00M (Peak 200.00M) | Time:00:01.00 | Rendering 1 / 16 samples",
    "Saved: '/out/frame_0001.png'  Time: 00:02.50 (Saving: 00:00.02)",
]


def make() -> tuple[RenderTelemetry, InMemoryMetricReader]:
    reader = InMemoryMetricReader()
    return RenderTelemetry(meter_provider=MeterProvider(metric_readers=[reader])), reader


def collect(reader: InMemoryMetricReader) -> dict:
    data = reader.get_metrics_data()
    if data is None:
        return {}
    return {
        metric.name: metric
        for resource_metric in data.resource_metrics
        for scope_metric in resource_metric.scope_metrics
        for metric in scope_metric.metrics
    }


def test_build_command_puts_the_scene_script_before_the_render_options():
    argv = build_command(
        RenderRequest(shot="SH010", scene_script="/app/scenes/demo_scene.py", frame_end=4)
    )

    # -noaudio, one dash: Blender takes "--noaudio" for a .blend path and dies naming a
    # file nobody asked for. This assertion is the only thing standing between that
    # typo and a Cloud Run execution that fails looking like a missing asset.
    assert argv[:4] == ["blender", "--background", "--factory-startup", "-noaudio"]
    # The script edits the scene the render then uses, so it must be parsed first.
    assert argv.index("--python") < argv.index("--render-anim")
    assert argv[argv.index("--frame-start") + 1] == "1"
    assert argv[argv.index("--frame-end") + 1] == "4"
    # --render-anim renders whatever the preceding options configured, so it goes last.
    assert argv[-1] == "--render-anim"


def test_build_command_opens_the_blend_file_before_running_the_script():
    argv = build_command(
        RenderRequest(shot="SH010", blend_file="/assets/sh010.blend", scene_script="/app/s.py")
    )

    assert argv.index("/assets/sh010.blend") < argv.index("--python")


def test_build_command_refuses_a_render_with_no_scene():
    """Blender would render its factory startup cube and exit 0. That is the trap."""
    with pytest.raises(ValueError, match="blend_file or scene_script"):
        build_command(RenderRequest(shot="SH010"))


def test_build_command_refuses_a_backwards_frame_range():
    with pytest.raises(ValueError, match="before frame_start"):
        build_command(
            RenderRequest(shot="SH010", scene_script="/app/s.py", frame_start=8, frame_end=4)
        )


def test_request_from_env_defaults_the_worker_to_the_cloud_run_execution():
    """Two executions of one job are two workers; the label has to say which."""
    request = request_from_env({"CLOUD_RUN_EXECUTION": "dailies-render-abc12"})

    assert request.worker == "dailies-render-abc12"


def test_request_from_env_prefers_an_explicit_worker():
    request = request_from_env(
        {"CLOUD_RUN_EXECUTION": "dailies-render-abc12", "DAILIES_WORKER": "farm-node-7"}
    )

    assert request.worker == "farm-node-7"


def test_request_from_env_rejects_a_non_numeric_frame():
    with pytest.raises(ValueError, match="DAILIES_FRAME_END"):
        request_from_env({"DAILIES_FRAME_END": "last"})


def test_request_from_env_treats_an_empty_blend_file_as_unset():
    """Cloud Run injects an empty string for a variable set to "", not an absent key."""
    assert request_from_env({"DAILIES_BLEND_FILE": ""}).blend_file is None


def test_recorded_events_carry_the_identity_the_parser_could_not_know():
    telemetry, reader = make()
    request = RenderRequest(
        shot="SH040", project="atlas", sequence="SEQ07", render_job="job-9", worker="node-3"
    )

    assert record_stream(iter(SAMPLE), request, telemetry) == 2

    point = collect(reader)[METRICS[Metric.FRAME_DURATION]].data.data_points[0]
    labels = dict(point.attributes)
    assert labels["project"] == "atlas"
    assert labels["sequence"] == "SEQ07"
    assert labels["render_job"] == "job-9"
    assert labels["worker"] == "node-3"
    assert labels["shot"] == "SH040"
    # The whole point of stamping: nothing the worker knows may reach Grafana as unknown.
    assert UNKNOWN not in labels.values()


def test_stamping_does_not_break_the_payload_contract():
    """The stamped copy is re-validated, so a kind still requires its payload field."""
    telemetry, reader = make()
    request = RenderRequest(shot="SH010")

    record_stream(iter(["Error: engine failed to initialise"]), request, telemetry)

    metrics = collect(reader)
    assert METRICS[Metric.FRAMES_FAILED] in metrics
    reasons = {
        dict(point.attributes)["reason"]
        for point in metrics[METRICS[Metric.FRAMES_FAILED]].data.data_points
    }
    assert reasons == {"engine_crash"}


def test_run_command_records_a_real_subprocess_stdout_and_returns_its_code():
    telemetry, reader = make()
    printer = "import sys\n" + "".join(f"print({line!r})\n" for line in SAMPLE)

    code = run_command([sys.executable, "-c", printer], RenderRequest(shot="SH010"), telemetry)

    assert code == 0
    assert METRICS[Metric.FRAME_DURATION] in collect(reader)


def test_run_command_propagates_a_failing_exit_code():
    """Cloud Run marks the execution failed on this code; swallowing it hides the OOM."""
    telemetry, _ = make()

    code = run_command(
        [sys.executable, "-c", "raise SystemExit(1)"], RenderRequest(shot="SH010"), telemetry
    )

    assert code == 1


def test_run_command_echoes_the_raw_log(tmp_path):
    """A render with no log is undiagnosable however good the metrics are."""
    telemetry, _ = make()
    log = tmp_path / "render.log"
    printer = "".join(f"print({line!r})\n" for line in SAMPLE)

    with log.open("w") as sink:
        run_command(
            [sys.executable, "-c", printer], RenderRequest(shot="SH010"), telemetry, echo=sink
        )

    assert log.read_text().splitlines() == SAMPLE


def test_identity_covers_every_label_a_stamped_event_needs():
    """Guards against a label being added to the schema and forgotten here."""
    request = RenderRequest(shot="SH010")
    stamped = RenderEvent.model_validate(
        {**RenderEvent.demo(shot="SH010").model_dump(), **request.identity}
    )

    assert UNKNOWN not in stamped.frame_labels().values()


# --- job declaration ----------------------------------------------------------------


def test_record_stream_declares_the_frame_range_before_recording():
    """The frame count is in the request, not in Blender's output.

    Blender never announces how many frames it is about to render, so nothing in the
    stream can be parsed into it. If the worker does not state it, the board has a
    completed count and nothing to divide by.
    """
    declared: list[tuple[int, dict[str, str]]] = []

    class Spy:
        def declare_job(self, *, frames_expected, labels, deadline_epoch=None):
            declared.append((frames_expected, dict(labels)))

        def record(self, event):
            pass

    request = RenderRequest(shot="SH010", frame_start=10, frame_end=57, render_job="job-7")
    record_stream([], request, Spy())

    assert declared, "the job must be declared even before any line arrives"
    frames, labels = declared[0]
    # Inclusive on both ends, matching Blender's --frame-start / --frame-end.
    assert frames == 48
    assert labels["shot"] == "SH010"
    assert labels["render_job"] == "job-7"
    assert "frame" not in labels
    assert "worker" not in labels


def test_a_single_frame_render_declares_one_frame():
    declared: list[int] = []

    class Spy:
        def declare_job(self, *, frames_expected, labels, deadline_epoch=None):
            declared.append(frames_expected)

        def record(self, event):
            pass

    record_stream([], RenderRequest(shot="SH010", frame_start=7, frame_end=7), Spy())
    assert declared == [1]


def test_a_render_can_carry_a_deadline():
    """The due date comes from the request, like the frame range, not from the output."""
    declared: list[dict] = []

    class Spy:
        def declare_job(self, *, frames_expected, labels, deadline_epoch=None):
            declared.append({"frames": frames_expected, "deadline": deadline_epoch})

        def record(self, event):
            pass

    request = RenderRequest(shot="SH010", frame_start=1, frame_end=4, deadline_epoch=1788100000)
    record_stream([], request, Spy())

    assert declared[0]["deadline"] == 1788100000


def test_a_render_without_a_deadline_declares_none():
    declared: list[dict] = []

    class Spy:
        def declare_job(self, *, frames_expected, labels, deadline_epoch=None):
            declared.append({"deadline": deadline_epoch})

        def record(self, event):
            pass

    record_stream([], RenderRequest(shot="SH010"), Spy())
    assert declared[0]["deadline"] is None
