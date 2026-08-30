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
