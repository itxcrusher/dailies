"""Look at the frame. Say what is there. Do not be told what to find.

This is the check telemetry structurally cannot perform. A missing texture is currently
inferred from a log line saying an asset could not be opened, which is evidence about the
*cause*. Whether the delivered picture is actually wrong is a different question, and for
a whole class of failures - a wrong camera, broken geometry, a black frame, a material
that silently resolved to grey - there is no log line at all.

**The prompt does not carry the telemetry's conclusion, and that is the entire design.**
A model told "this shot had a missing asset" and shown an image will agree, confidently,
about a frame that is perfectly fine. The check would then be confirming the metrics
rather than corroborating them, and two sources that cannot disagree are one source
wearing two hats. So this runs blind: the image and the shot label, nothing else.

What that buys is the only genuinely interesting result the system can produce - two
independent verdicts that can be compared. A log saying ``asset_missing`` beside a
picture that looks fine is a real finding, and the investigator is already instructed to
report a disagreement between sources rather than quietly pick the more alarming one.
"""

from __future__ import annotations

import json
import re
from typing import Any

__all__ = [
    "VISUAL_INSTRUCTION",
    "VISUAL_SCHEMA",
    "VisualCheckFailed",
    "build_visual_prompt",
    "parse_verdict",
]


class VisualCheckFailed(RuntimeError):
    """The visual check did not produce a verdict that can be shown to anyone."""


#: The answer shape. ``observation`` is required for the same reason the investigator's
#: ``evidence`` is: a model asked for a judgement will produce one whether or not it
#: looked, and a verdict with nothing behind it is exactly the confident-unverifiable
#: output this project exists to argue against. Making it say what it SAW is what lets a
#: human open the same frame and disagree.
VISUAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["verdict", "observation", "confidence"],
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["looks_correct", "suspect", "broken"],
            "description": (
                "looks_correct: nothing visibly wrong. suspect: something looks off and "
                "a human should check. broken: the frame is plainly unusable."
            ),
        },
        "observation": {
            "type": "string",
            "description": (
                "What is actually visible in the frame, in one or two sentences. This is "
                "the evidence for the verdict and must describe the image rather than "
                "restate the verdict."
            ),
        },
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    },
}

#: What the model is told about renderers, and deliberately nothing about this shot.
#:
#: The first version carried no domain knowledge at all, on the reasoning that a model
#: primed with a failure mode finds that failure mode. Driven against two real frames from
#: the same scene, one with a texture deliberately missing, it failed: the grey cube and
#: the magenta cube both came back ``looks_correct`` at high confidence. It described the
#: magenta accurately and judged it fine, which is the correct answer to the question it
#: was asked. A purple cube is not intrinsically wrong; "wrong colour" is only wrong
#: relative to an expectation, and it had been given none.
#:
#: Adding renderer knowledge fixed it. The same two frames then came back
#: ``looks_correct`` and ``suspect``, with the plain grey frame NOT flagged, which was the
#: real risk of priming.
#:
#: The line this walks is worth stating precisely, because it is easy to cross by
#: accident. Teaching the model that magenta signals a failed texture load in Blender
#: teaches it to READ THE INSTRUMENT, exactly as the investigator is told that Loki keeps
#: ``shot`` in structured metadata. Telling it what this shot's logs said would be
#: something else: the check would confirm the telemetry rather than corroborate it, and
#: two sources that cannot disagree are one source wearing two hats.
VISUAL_INSTRUCTION = """\
You are looking at a single rendered frame from a 3D animation shot.

Describe what you can actually see, then judge whether the image looks like a correct
render or like something went wrong in producing it.

Renderer-specific knowledge to apply. This is how the tools behave in general, not a
claim about this frame:

- A surface rendered as flat, saturated MAGENTA or PURPLE with no texture detail is the
  conventional signal, in Blender and most DCC tools, that an image texture failed to
  load. It is rarely an intentional art-direction choice for an entire object.
- A surface with no detail at all where a material was expected reads the same way.
- Uniform grey or white surfaces are ordinary renderer defaults and are NOT by themselves
  suspicious. Plenty of correct frames are plain.

Judge the frame on what you can see. Do not assume anything is wrong.

Rules:

- Report what is visible. Your observation must describe the image itself, not restate
  your verdict and not speculate about causes you cannot see.
- Say "looks_correct" when nothing is visibly wrong. A simple or plain image is not by
  itself a defect; plenty of correct frames are plain.
- Say "suspect" when something looks off in a way a person should check: a surface with
  no texture or detail where you would expect some, unexpected flat colour, obvious
  banding or noise, geometry that looks incomplete.
- Say "broken" only when the frame is plainly unusable: entirely black, entirely blank,
  or corrupted.
- If you are unsure, say so with confidence rather than by hedging the verdict. "low"
  is a real answer and a more useful one than a confident guess.

Answer with a single JSON object and nothing else, matching this schema:

{schema}
"""


def build_visual_prompt(shot: str) -> str:
    """What the model is asked about one frame.

    Carries the shot label so the answer can be attributed, and nothing else. There is
    deliberately no mention of the render's telemetry, its exit code, its logs, or the
    failure this project happens to be looking for.
    """
    return (
        f"This is a frame from shot {shot}.\n\n"
        "Describe what you can see in it, then judge whether it looks like a correct "
        "render. Answer with the JSON object described in your instructions and nothing "
        "else."
    )


def _strip_fence(text: str) -> str:
    """Unwrap a ```json fence. Models produce them constantly and it is not an error."""
    fenced = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
    return fenced.group(1) if fenced else text


def parse_verdict(answer: str) -> dict[str, Any]:
    """Turn the model's text into a verdict, or refuse it.

    Refuses rather than repairs. A verdict shown on the board carries the same weight as
    one a person wrote, so an answer that did not meet the contract must not be quietly
    reshaped into one that looks like it did.

    Raises:
        VisualCheckFailed: with what the model actually said, because that is the only
            thing that makes the failure diagnosable.
    """
    text = (answer or "").strip()
    if not text:
        raise VisualCheckFailed("The visual check returned nothing at all.")

    try:
        parsed = json.loads(_strip_fence(text))
    except json.JSONDecodeError as exc:
        raise VisualCheckFailed(
            f"The visual check did not answer with JSON. It said: {text[:400]}"
        ) from exc

    if not isinstance(parsed, dict):
        raise VisualCheckFailed(f"The visual check answered with a {type(parsed).__name__}.")

    verdict = parsed.get("verdict")
    allowed = VISUAL_SCHEMA["properties"]["verdict"]["enum"]
    if verdict not in allowed:
        raise VisualCheckFailed(
            f"The visual check answered {verdict!r}, which is not one of {allowed}."
        )

    observation = (parsed.get("observation") or "").strip()
    if not observation:
        # The whole point. A verdict nobody can check is the thing this project argues
        # against, and storing one would put it on the board looking like a real finding.
        raise VisualCheckFailed(
            f"The visual check returned {verdict!r} with no observation behind it, so "
            "there is nothing a person could open the frame and disagree with."
        )

    confidence = parsed.get("confidence")
    if confidence not in VISUAL_SCHEMA["properties"]["confidence"]["enum"]:
        confidence = "low"

    return {"verdict": verdict, "observation": observation, "confidence": confidence}
