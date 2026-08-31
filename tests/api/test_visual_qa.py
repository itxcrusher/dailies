"""Looking at the picture, with no idea what the telemetry said.

This is the check telemetry structurally cannot do. A missing texture is currently
inferred from a log line saying an asset could not be opened, which is evidence about
the CAUSE. Whether the delivered frame is actually wrong is a different question, and
for a whole class of failures - a wrong camera, broken geometry, a black frame, a
material that resolved to grey - there is no log line at all.

**The prompt must not carry the telemetry's conclusion, and that is the whole design.**
A model told "this shot had a missing asset" and shown a picture will agree, every time,
about a frame that is perfectly fine. Then the check has confirmed the metrics rather
than corroborated them, and two sources that cannot disagree are one source. So Visual QA
is deliberately blind: it gets the image and the shot label, and nothing about what
Prometheus or Loki reported.

What that buys is the only interesting result in the system: two independent verdicts
that can be compared. If the log says asset_missing and the picture looks fine, that
disagreement is itself a finding, and the investigator is already instructed to report a
disagreement rather than pick a side.
"""

import pytest
from dailies_api.visual_qa import (
    VISUAL_SCHEMA,
    VisualCheckFailed,
    build_visual_prompt,
    parse_verdict,
)


def test_the_prompt_never_carries_the_telemetry_verdict():
    """The regression that would quietly destroy the feature's value."""
    prompt = build_visual_prompt("SH050")

    lowered = prompt.lower()
    for leak in ("asset_missing", "jacket_diffuse", "prometheus", "loki", "log line"):
        assert leak not in lowered, f"the visual check must not be told {leak!r}"


def test_the_prompt_asks_for_what_is_visible_not_what_is_wrong():
    """'What is wrong with this frame' presumes something is, and gets an answer."""
    prompt = build_visual_prompt("SH050").lower()
    assert "sh050" in prompt
    assert "describe" in prompt or "what you can see" in prompt


def test_a_clean_verdict_parses():
    verdict = parse_verdict(
        '{"verdict":"looks_correct","observation":"A grey cube on a plain '
        'background, evenly lit, no obvious artefacts.","confidence":"high"}'
    )
    assert verdict["verdict"] == "looks_correct"
    assert verdict["confidence"] == "high"


def test_a_broken_verdict_parses():
    verdict = parse_verdict(
        '{"verdict":"suspect","observation":"The subject is a flat untextured grey '
        'with no surface detail, which usually means a material failed to load.",'
        '"confidence":"medium"}'
    )
    assert verdict["verdict"] == "suspect"
    assert "untextured" in verdict["observation"]


def test_a_verdict_without_an_observation_is_refused():
    """A verdict with nothing behind it is the unverifiable output this project rejects.

    Same rule as the investigator's evidence: a model asked for a judgement will give
    one whether or not it looked. Requiring it to say what it SAW is what makes the
    judgement checkable by a human opening the same frame.
    """
    with pytest.raises(VisualCheckFailed):
        parse_verdict('{"verdict":"looks_correct","confidence":"high"}')


def test_a_verdict_outside_the_vocabulary_is_refused():
    with pytest.raises(VisualCheckFailed):
        parse_verdict('{"verdict":"probably fine","observation":"x","confidence":"high"}')


def test_prose_instead_of_json_is_refused_with_what_was_said():
    with pytest.raises(VisualCheckFailed) as caught:
        parse_verdict("The frame looks a bit grey to me.")
    assert "a bit grey" in str(caught.value)


def test_json_wrapped_in_a_code_fence_is_accepted():
    """Models fence JSON constantly, and refusing that is refusing a correct answer."""
    verdict = parse_verdict(
        '```json\n{"verdict":"broken","observation":"The frame is entirely black.",'
        '"confidence":"high"}\n```'
    )
    assert verdict["verdict"] == "broken"


def test_the_schema_requires_an_observation():
    assert "observation" in VISUAL_SCHEMA["required"]
    assert set(VISUAL_SCHEMA["properties"]["verdict"]["enum"]) == {
        "looks_correct",
        "suspect",
        "broken",
    }


# --- renderer domain knowledge -------------------------------------------------------
#
# Driven against two real frames on 2026-08-30, both rendered from the same scene, one
# with a texture deliberately missing. The blind check as first written FAILED:
#
#   clean  (grey cube)     -> looks_correct, high
#   broken (magenta cube)  -> looks_correct, high
#
# It described the magenta accurately and judged it fine, which on reflection is the
# correct answer to the question it was asked. A purple cube is not intrinsically wrong;
# "wrong colour" is only wrong relative to an expectation, and it had none.
#
# Adding RENDERER knowledge - not this shot's telemetry - fixed it:
#
#   clean  -> looks_correct, high   ("no textures or complex details")
#   broken -> suspect,       high   ("solid, saturated magenta/purple")
#
# The distinction is the whole point. Telling it that magenta means a failed texture load
# in Blender teaches it to read the instrument, the same way the investigator is told that
# Loki keeps `shot` in structured metadata. It is not told what the answer is, and it
# still knows nothing about what this shot's metrics or logs reported, so it remains a
# source that can disagree with them.


def test_the_instruction_carries_renderer_knowledge():
    from dailies_api.visual_qa import VISUAL_INSTRUCTION

    lowered = VISUAL_INSTRUCTION.lower()
    assert "magenta" in lowered, "the missing-texture signature has to be taught"
    assert "blender" in lowered or "dcc" in lowered


def test_the_instruction_guards_against_flagging_a_plain_frame():
    """The risk of priming: a model hunting for defects finds them in a plain render.

    Our own demo scene is a grey cube on a dark background, so without this guard the
    clean frame is the false positive waiting to happen.
    """
    from dailies_api.visual_qa import VISUAL_INSTRUCTION

    # Whitespace-normalised, because the prompt is hard-wrapped for readability and a
    # phrase that happens to straddle a line break is a formatting detail, not a change
    # in what the model is told. The first version of this test asserted the raw
    # substring and failed on exactly that.
    lowered = " ".join(VISUAL_INSTRUCTION.lower().split())
    assert "grey" in lowered or "gray" in lowered
    assert "not by themselves suspicious" in lowered or "not by itself a defect" in lowered


def test_renderer_knowledge_is_not_telemetry_knowledge():
    """The line that must not be crossed, restated as a test.

    Teaching how the renderer signals failure is domain knowledge. Telling it what this
    shot's logs said would make it confirm the metrics rather than corroborate them, and
    two sources that cannot disagree are one source.
    """
    from dailies_api.visual_qa import VISUAL_INSTRUCTION, build_visual_prompt

    whole = (VISUAL_INSTRUCTION + build_visual_prompt("SH201")).lower()
    for leak in ("asset_missing", "jacket_diffuse", "prometheus", "loki", "exit code"):
        assert leak not in whole, f"the visual check must not be told {leak!r}"


# --- the runner ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_verdict_comes_back_from_the_frame_bytes():
    from dailies_api.visual_qa import check_frame

    seen: dict[str, object] = {}

    async def fake_model(*, image: bytes, mime_type: str, instruction: str, prompt: str):
        seen.update(image=image, mime_type=mime_type, prompt=prompt)
        return '{"verdict":"suspect","observation":"a flat magenta cube","confidence":"high"}'

    verdict = await check_frame(b"\x89PNG-bytes", shot="SH201", model=fake_model)

    assert verdict["verdict"] == "suspect"
    assert verdict["observation"] == "a flat magenta cube"
    assert seen["image"] == b"\x89PNG-bytes"
    assert seen["mime_type"] == "image/png"


@pytest.mark.asyncio
async def test_the_runner_never_leaks_this_shots_telemetry_into_the_prompt():
    """The design constraint, enforced where it can actually be broken.

    An instruction test cannot catch a caller that appends 'the logs said asset_missing'
    to the prompt. This one watches what the model is actually handed.
    """
    from dailies_api.visual_qa import check_frame

    sent: dict[str, str] = {}

    async def fake_model(*, image, mime_type, instruction, prompt):
        sent.update(instruction=instruction, prompt=prompt)
        return '{"verdict":"looks_correct","observation":"a grey cube","confidence":"high"}'

    await check_frame(b"x", shot="SH201", model=fake_model)

    whole = (sent["instruction"] + sent["prompt"]).lower()
    for leak in ("asset_missing", "jacket_diffuse", "prometheus", "loki", "exit 0"):
        assert leak not in whole


@pytest.mark.asyncio
async def test_an_unusable_answer_is_refused_rather_than_shown():
    from dailies_api.visual_qa import VisualCheckFailed, check_frame

    async def prose(*, image, mime_type, instruction, prompt):
        return "The frame looks fine to me."

    with pytest.raises(VisualCheckFailed):
        await check_frame(b"x", shot="SH201", model=prose)


@pytest.mark.asyncio
async def test_a_verdict_without_an_observation_is_refused():
    """Same rule as the investigator's evidence: a judgement nobody can check is not one."""
    from dailies_api.visual_qa import VisualCheckFailed, check_frame

    async def bare(*, image, mime_type, instruction, prompt):
        return '{"verdict":"broken","confidence":"high"}'

    with pytest.raises(VisualCheckFailed):
        await check_frame(b"x", shot="SH201", model=bare)


def test_a_jpeg_frame_is_typed_as_a_jpeg():
    from dailies_api.visual_qa import mime_for

    assert mime_for("SH201/frame_0001.png") == "image/png"
    assert mime_for("SH201/frame_0001.jpg") == "image/jpeg"
    assert mime_for("SH201/frame_0001.exr") == "image/png", "unknown types fall back to png"
