"""Finding the frame to look at, and handing it to Gemini.

Both are seams onto systems this test suite must not touch, so both are injected. What
is tested here is the logic around them: which frame gets picked when a shot rendered
forty, and what happens when there are none.
"""

import pytest
from dailies_api.frames import latest_frame, newest_of


def test_the_newest_frame_wins():
    """A shot renders many frames and the newest is the one worth looking at.

    Not the first: on a partial render the early frames are the ones that succeeded, and
    the interesting question is what the farm was producing when it stopped.
    """
    names = [
        "SH100/frame_0001.png",
        "SH100/frame_0017.png",
        "SH100/frame_0009.png",
    ]
    assert newest_of(names) == "SH100/frame_0017.png"


def test_frames_are_ordered_numerically_not_lexically():
    """frame_0009 vs frame_0010 sorts correctly by string here, but 9 vs 10 does not.

    Blender zero-pads, so lexical order happens to work today. It stops working the
    moment a pipeline writes frame_9.png, and a board that silently shows frame 9 of a
    forty-frame render because it sorted as the newest is very hard to notice.
    """
    assert newest_of(["SH1/frame_9.png", "SH1/frame_10.png"]) == "SH1/frame_10.png"


def test_no_frames_yields_none_rather_than_raising():
    """A shot can legitimately have no frames yet, at the top of a render."""
    assert newest_of([]) is None


def test_a_name_with_no_number_does_not_break_the_ordering():
    assert newest_of(["SH1/preview.png", "SH1/frame_0003.png"]) == "SH1/frame_0003.png"


@pytest.mark.asyncio
async def test_latest_frame_reads_the_newest_object_for_the_shot():
    reads: dict[str, str] = {}

    async def fake_list(prefix: str) -> list[str]:
        reads["prefix"] = prefix
        return ["SH201/frame_0001.png", "SH201/frame_0002.png"]

    async def fake_read(name: str) -> bytes:
        reads["read"] = name
        return b"png-bytes"

    found = await latest_frame("SH201", list_objects=fake_list, read_object=fake_read)

    assert found is not None
    assert found.path == "SH201/frame_0002.png"
    assert found.data == b"png-bytes"
    assert reads["prefix"] == "SH201/"


@pytest.mark.asyncio
async def test_latest_frame_is_none_when_the_shot_has_rendered_nothing():
    async def empty(prefix: str) -> list[str]:
        return []

    async def unused(name: str) -> bytes:  # pragma: no cover - must not be called
        raise AssertionError("should not read when there is nothing to read")

    assert await latest_frame("SH999", list_objects=empty, read_object=unused) is None


# --- what the bucket actually contains -----------------------------------------------


def test_a_directory_placeholder_is_not_a_frame():
    """gcsfuse writes zero-byte directory markers, and they sort first by accident.

    The bucket really contains this:

        SH200/
        SH200/frame_0001.png

    and the marker's trailing number is 200, from the shot name, while the frame's is 1,
    from the zero-padded frame number. So the marker won, the API downloaded zero bytes,
    and Gemini answered 400 "Provided image is not valid" on every shot. The failure was
    swallowed by design, so the board simply showed no visual verdict and nothing said why.
    """
    names = ["SH200/", "SH200/frame_0001.png"]
    assert newest_of(names) == "SH200/frame_0001.png"


def test_only_image_files_are_considered():
    """A sidecar or a log dropped in the directory is not something to show a model."""
    names = [
        "SH100/frame_0002.png",
        "SH100/render.log",
        "SH100/frame_0003.json",
    ]
    assert newest_of(names) == "SH100/frame_0002.png"


def test_a_directory_with_no_frames_yields_nothing():
    assert newest_of(["SH999/"]) is None


def test_the_shot_name_does_not_outrank_the_frame_number():
    """The general form of the bug: digits in the path must not beat digits in the name."""
    names = ["SEQ99/SH200/frame_0007.png", "SEQ99/SH200/frame_0012.png"]
    assert newest_of(names) == "SEQ99/SH200/frame_0012.png"
