"""Blender CLI output parser.

This is where the render-domain knowledge lives, so it carries the heaviest coverage:
the classification ORDER (a CUDA out-of-memory line also contains a path and must not
be read as a missing asset), unit and time arithmetic, frame recovery from the output
path, and the identity a parsed line is stamped with.
"""

import pytest
from dailies_telemetry.parser import UNKNOWN, parse_line
from dailies_telemetry.schema import EventKind
from pydantic import BaseModel

# --- the four canonical lines -------------------------------------------------


def test_parses_frame_progress_line():
    line = "Fra:12 Mem:245.31M (Peak 512.00M) | Time:00:04.21 | Rendering 3 / 16 samples"
    e = parse_line(line, shot="SH010")
    assert e.kind == EventKind.FRAME_START
    assert e.frame == 12
    assert e.memory_bytes == int(245.31 * 1024 * 1024)


def test_parses_saved_frame_as_complete():
    e = parse_line("Saved: '/out/SH010_0012.png'  Time: 00:04.55 (Saving: 00:00.03)", shot="SH010")
    assert e.kind == EventKind.FRAME_COMPLETE
    assert e.frame == 12
    assert e.duration_seconds == 4.55


def test_detects_missing_texture_as_asset_missing():
    line = "Warning: Unable to open file '/assets/jacket_diffuse.exr'"
    e = parse_line(line, shot="SH030")
    assert e.kind == EventKind.ASSET_MISSING
    assert "jacket_diffuse.exr" in e.message


def test_returns_none_for_uninteresting_lines():
    assert parse_line("Blender 4.2.1", shot="SH010") is None


# --- classification order (the part that is easy to get wrong) ----------------


def test_cuda_oom_with_a_path_is_oom_not_asset_missing():
    """Locks the branch order: OOM is checked before the missing-asset pattern.

    A CUDA out-of-memory message names the source file it failed in, which the
    missing-asset regex would happily claim.
    """
    line = (
        "CUDA error: out of memory in cuMemAlloc, "
        "Unable to open file '/build/intern/cycles/device/cuda/device_impl.cpp'"
    )
    e = parse_line(line, shot="SH010")
    assert e.kind == EventKind.OOM


def test_crash_with_a_path_is_engine_crash_not_asset_missing():
    line = "Segmentation fault while reading, Cannot read file '/assets/set.blend'"
    e = parse_line(line, shot="SH010")
    assert e.kind == EventKind.ENGINE_CRASH


def test_oom_beats_engine_crash():
    """Both patterns match; the more specific cause wins."""
    line = "Error: engine 'CYCLES' failed: std::bad_alloc"
    assert parse_line(line, shot="SH010").kind == EventKind.OOM


def test_a_failure_line_is_not_read_as_frame_progress():
    """The Fra: prefix rides along on Cycles error lines; the failure must win."""
    line = "Fra:12 Mem:15000.00M (Peak 16000.00M) | Error: System is out of GPU memory"
    e = parse_line(line, shot="SH010")
    assert e.kind == EventKind.OOM


@pytest.mark.parametrize(
    "line, kind",
    [
        ("CUDA error: Out Of Memory in cuMemAlloc", EventKind.OOM),
        ("terminate called after throwing an instance of 'std::bad_alloc'", EventKind.OOM),
        ("Error: System is out of GPU memory", EventKind.OOM),
        ("Segmentation fault (core dumped)", EventKind.ENGINE_CRASH),
        ("EXCEPTION_ACCESS_VIOLATION at 0x00007ff8", EventKind.ENGINE_CRASH),
        ("Error: engine 'CYCLES' not found", EventKind.ENGINE_CRASH),
        ("Warning: Unable to open file '/assets/a.exr'", EventKind.ASSET_MISSING),
        ("Cannot read file '/assets/set.blend'", EventKind.ASSET_MISSING),
        ("Warning: missing '/assets/hair.abc'", EventKind.ASSET_MISSING),
    ],
)
def test_failure_lines_classify(line, kind):
    assert parse_line(line, shot="SH010").kind == kind


def test_failure_classification_is_case_insensitive():
    assert parse_line("UNABLE TO OPEN FILE '/a.exr'", shot="SH010").kind == EventKind.ASSET_MISSING


# --- frame progress -----------------------------------------------------------


@pytest.mark.parametrize(
    "mem, unit, multiplier",
    [("512.00", "K", 1024), ("245.31", "M", 1024**2), ("2.50", "G", 1024**3)],
)
def test_memory_units_are_converted_to_bytes(mem, unit, multiplier):
    line = f"Fra:7 Mem:{mem}{unit} (Peak 600.00M) | Time:00:01.00 | Path Tracing Tile 1/64"
    e = parse_line(line, shot="SH010")
    assert e.memory_bytes == int(float(mem) * multiplier)


def test_frame_progress_reads_the_current_not_the_peak_memory():
    line = "Fra:7 Mem:100.00M (Peak 900.00M) | Time:00:01.00 | Rendering 1 / 16 samples"
    assert parse_line(line, shot="SH010").memory_bytes == (100 * 1024**2)


def test_frame_progress_carries_no_duration():
    """The Time: on a progress line is elapsed-so-far, not the frame's duration."""
    line = "Fra:12 Mem:245.31M (Peak 512.00M) | Time:00:04.21 | Rendering 3 / 16 samples"
    assert parse_line(line, shot="SH010").duration_seconds is None


# --- frame complete -----------------------------------------------------------


def test_duration_includes_whole_minutes():
    e = parse_line("Saved: '/out/SH010_0125.exr'  Time: 02:04.55 (Saving: 00:00.03)", shot="SH010")
    assert e.duration_seconds == pytest.approx(124.55)
    assert e.frame == 125


def test_frame_is_recovered_from_a_windows_output_path():
    e = parse_line(r"Saved: 'C:\out\SH010_0042.png'  Time: 00:01.00", shot="SH010")
    assert e.frame == 42


def test_frame_falls_back_to_the_hint_when_the_path_has_no_frame_number():
    e = parse_line("Saved: '/out/beauty.png'  Time: 00:01.00", shot="SH010", frame_hint=99)
    assert e.kind == EventKind.FRAME_COMPLETE
    assert e.frame == 99


def test_a_path_with_spaces_still_parses():
    e = parse_line("Saved: '/out/my shot_0007.png'  Time: 00:03.00", shot="SH010")
    assert e.frame == 7
    assert e.duration_seconds == pytest.approx(3.0)


# --- the frame hint -----------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "Warning: Unable to open file '/assets/a.exr'",
        "Segmentation fault (core dumped)",
        "CUDA error: out of memory",
    ],
)
def test_failure_events_use_the_frame_hint(line):
    assert parse_line(line, shot="SH010", frame_hint=31).frame == 31


def test_a_frame_number_on_the_line_beats_the_hint():
    line = "Fra:12 Mem:15000.00M (Peak 16000.00M) | Error: System is out of GPU memory"
    e = parse_line(line, shot="SH010", frame_hint=99)
    assert e.frame == 12
    assert e.memory_bytes == (15000 * 1024**2)


def test_frame_hint_defaults_to_zero():
    assert parse_line("Segmentation fault", shot="SH010").frame == 0


# --- the produced event is always a valid RenderEvent -------------------------

ALL_INTERESTING_LINES = [
    "Fra:12 Mem:245.31M (Peak 512.00M) | Time:00:04.21 | Rendering 3 / 16 samples",
    "Saved: '/out/SH010_0012.png'  Time: 00:04.55 (Saving: 00:00.03)",
    "Warning: Unable to open file '/assets/jacket_diffuse.exr'",
    "CUDA error: out of memory in cuMemAlloc",
    "Segmentation fault (core dumped)",
]


@pytest.mark.parametrize("line", ALL_INTERESTING_LINES)
def test_every_event_satisfies_the_schema_contract(line):
    """RenderEvent enforces a payload field per kind; every branch must supply it."""
    e = parse_line(line, shot="SH010")
    assert isinstance(e, BaseModel)
    assert e.shot == "SH010"
    assert e.frame >= 0


@pytest.mark.parametrize("line", ALL_INTERESTING_LINES)
def test_every_event_can_produce_its_label_sets(line):
    e = parse_line(line, shot="SH010")
    assert e.frame_labels()["shot"] == "SH010"
    assert "frame" not in e.job_labels()


def test_oom_events_carry_memory_bytes():
    """The schema requires it, so the parser must never emit OOM without one."""
    e = parse_line("CUDA error: out of memory", shot="SH010")
    assert e.memory_bytes is not None


@pytest.mark.parametrize("line", ALL_INTERESTING_LINES[2:])
def test_failure_events_keep_the_raw_line_as_the_message(line):
    assert parse_line(line, shot="SH010").message == line.strip()


# --- identity -----------------------------------------------------------------


def test_identity_flows_through_to_the_event():
    e = parse_line(
        "Fra:12 Mem:245.31M (Peak 512.00M) | Time:00:04.21 | Rendering 3 / 16 samples",
        shot="SH010",
        project="atlas",
        sequence="SEQ01",
        render_job="job-7",
        worker="worker-3",
    )
    assert e.labels()["project"] == "atlas"
    assert e.labels()["worker"] == "worker-3"
    assert e.labels()["render_job"] == "job-7"


def test_unset_identity_lands_in_a_visibly_unknown_series():
    """The parser cannot know the job identity from a line of text.

    It must not borrow another worker's, so the placeholder is an obvious sentinel
    rather than a plausible-looking default.
    """
    e = parse_line("Segmentation fault", shot="SH010")
    assert e.worker == UNKNOWN == "unknown"
    assert e.project == e.sequence == e.render_job == UNKNOWN


# --- lines that are not events ------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "Blender 4.2.1",
        "",
        "   ",
        "Read blend: /scenes/SH010.blend",
        "Fra:12 Mem:245.31",  # no unit: nothing to convert to bytes
        "Saved: '/out/SH010_0012.png'",  # no Time: field
        "Time: 00:04.55",
        "Warning: the scene has no camera",
        "| Rendering 3 / 16 samples",
    ],
)
def test_uninteresting_lines_return_none(line):
    assert parse_line(line, shot="SH010") is None


def test_parse_line_does_not_mutate_its_input():
    line = "  Fra:12 Mem:245.31M (Peak 512.00M) | Time:00:04.21 | Rendering 3 / 16 samples  "
    before = line
    parse_line(line, shot="SH010")
    assert line == before
