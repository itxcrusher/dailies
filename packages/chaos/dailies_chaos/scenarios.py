"""The faults this farm can be made to have on purpose.

A demo that clicks a shot which was already broken proves the board can display a stored
answer. Breaking a render in front of someone and watching the agent find it proves the
thing the project claims. These are the levers for doing that.

**Three, not the six the SPEC listed, and the difference is deliberate.** Every scenario
here is inducible with the render worker exactly as it is today, through environment the
scene already reads. Task crash, output-write failure and priority inversion would each
need new code in the worker to simulate, and a chaos suite whose scenarios are themselves
mocks proves less than three real ones. What is here actually happens to the render.

Each scenario names what a supervisor would see and what the agent should conclude, so a
run can be judged rather than merely watched.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["SCENARIOS", "Scenario", "find"]


@dataclass(frozen=True)
class Scenario:
    """One deliberate fault.

    Attributes:
        env: Environment applied to the render execution. Every key is one the worker or
            the scene already reads; nothing here is a flag invented for the demo.
        proves: What this scenario demonstrates that the others do not.
        expect: What the agent should conclude, so a run has a pass and a fail rather than
            a shrug.
    """

    name: str
    summary: str
    env: dict[str, str]
    proves: str
    expect: str
    frames: int = 4
    aliases: tuple[str, ...] = field(default=())


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        name="missing-texture",
        summary="A texture fails to resolve. Blender substitutes a flat colour and exits 0.",
        env={"DAILIES_MISSING_TEXTURE": "1"},
        proves=(
            "The failure nothing else catches. Every number reads success: the exit code "
            "is 0, the frame count is complete, the durations are normal. The only "
            "evidence is a warning in the logs and the picture itself."
        ),
        expect=(
            "problem_found true, the cause naming jacket_diffuse.exr, and the frame "
            "verdict 'suspect' from a visual check that was told nothing about the logs."
        ),
    ),
    Scenario(
        name="slow-frame",
        summary="A pathologically expensive frame, at sixteen times the usual sample count.",
        env={"DAILIES_SAMPLES": "1024"},
        proves=(
            "Delivery risk without any failure at all. Nothing errors and nothing is "
            "missing; the shot is simply not going to land in time, which is a different "
            "question from whether it is broken and is answered by different arithmetic."
        ),
        expect=(
            "No defect found, and a delivery verdict that moves off ON_TRACK as the "
            "forecast crosses the deadline."
        ),
        frames=2,
    ),
    Scenario(
        name="worker-oom",
        summary="A frame far too large for the worker's memory, at 4K with no denoising.",
        env={"DAILIES_RESOLUTION_X": "3840", "DAILIES_RESOLUTION_Y": "2160"},
        proves=(
            "The failure a render farm already catches, kept here as the contrast. This "
            "one is loud: the task dies, the frame count falls short, and infrastructure "
            "notices on its own. It is the baseline the silent failures are measured "
            "against, not a feature of this project."
        ),
        expect=(
            "Frames completed short of expected, and a cause naming memory rather than an asset."
        ),
        frames=1,
        aliases=("oom",),
    ),
)


def find(name: str) -> Scenario | None:
    """The scenario called ``name``, by its name or an alias."""
    wanted = name.strip().lower()
    for scenario in SCENARIOS:
        if wanted == scenario.name or wanted in scenario.aliases:
            return scenario
    return None
