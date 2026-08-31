"""What the investigator is scored against.

Four cases, each one telemetry the agent is given and a claim about what it should
conclude. The point is to replace "the agent works" with a number, because this project
refuses unbacked claims everywhere else and its own capability was, until now, asserted
from three anecdotes.

**The transport is faked; the model is not.** Each scenario replays captured Prometheus
and Loki responses through the real ``GrafanaMCP`` wrapper, the real tool routing, the
real prompt and a real Gemini call. Only the network is replaced. That isolates what is
being measured, which is whether the agent reaches the right conclusion from a given set
of telemetry, from render flakiness and from a 24-hour metric retention window that would
otherwise decide whether an eval passes.

``no_telemetry`` is the case that matters most and the one an eval built only from happy
paths would never contain. Every query comes back empty, and a model asked for a cause
will produce a plausible one whether or not it looked. This project's entire thesis is
that an empty result is more often a defect in the query than an absence in the data, so
an agent that answers it by naming a file it never saw has failed in exactly the way the
product exists to catch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .fixtures import (
    SH200_COMPLETED,
    SH200_EXPECTED,
    SH200_LOGS,
    SH201_COMPLETED,
    SH201_EXPECTED,
    SH201_LOGS,
)

__all__ = ["SCENARIOS", "Scenario"]

_EMPTY_PROM: dict[str, Any] = {"status": "success", "data": {"resultType": "matrix", "result": []}}
_EMPTY_LOKI: dict[str, Any] = {"status": "success", "data": {"resultType": "streams", "result": []}}


def _stalled(source: dict[str, Any], value: str) -> dict[str, Any]:
    """The completed-frames response with the count rewritten.

    Derived from real telemetry rather than composed from nothing, so the labels, the
    instance ids and the shape are all still the farm's own.
    """
    import copy

    out = copy.deepcopy(source)
    for series in out["data"]["result"]:
        series["values"] = [[stamp, value] for stamp, _ in series["values"]]
    return out


@dataclass(frozen=True)
class Scenario:
    """One graded case.

    Attributes:
        prom_expected: What a query for expected frames returns.
        prom_completed: What a query for completed frames returns. Held separately because
            the agent issues two Prometheus calls and this whole scenario set turns on the
            arithmetic between them; one blended response could not express a stall.
        logs: What the Loki query returns. An empty result is a claim, not a gap: it says
            this shot logged nothing, which for a healthy render is the truth.
        expect_problem: What ``problem_found`` must be. ``None`` where the honest answer
            is either, and only the forbidden text is graded.
        cause_must_mention: Any one of these substrings must appear in the cause, matched
            case-insensitively. Empty means the cause is not graded on content.
        cause_must_not_mention: Text whose presence is a failure. This is how fabrication
            is caught: a specific filename in an answer built from no data was invented.
    """

    name: str
    shot_id: str
    why: str
    prom_expected: dict[str, Any]
    prom_completed: dict[str, Any]
    logs: dict[str, Any]
    expect_problem: bool | None = None
    cause_must_mention: tuple[str, ...] = ()
    cause_must_not_mention: tuple[str, ...] = field(default=())


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        name="missing_texture",
        shot_id="dailies:SEQ01:SH201:vqa-bad",
        why=(
            "The failure this product exists for. Every number reads success, the exit "
            "code was 0, and the only evidence is a log line and the picture."
        ),
        prom_expected=SH201_EXPECTED,
        prom_completed=SH201_COMPLETED,
        logs=SH201_LOGS,
        expect_problem=True,
        cause_must_mention=("jacket_diffuse", "asset", "texture", "missing"),
    ),
    Scenario(
        name="clean_render",
        shot_id="dailies:SEQ01:SH200:vqa-good",
        why=(
            "The false-positive guard. A healthy render logs nothing, and an agent that "
            "reads silence as a fault makes the board unusable long before it makes it "
            "wrong: a supervisor who is paged for working shots stops reading the pages."
        ),
        prom_expected=SH200_EXPECTED,
        prom_completed=SH200_COMPLETED,
        logs=SH200_LOGS,
        expect_problem=False,
    ),
    Scenario(
        name="stalled_no_errors",
        shot_id="dailies:SEQ01:SH210:stalled",
        why=(
            "Seventeen of forty frames and not one error logged. Nothing failed, so "
            "nothing is in the logs; the shortfall is only visible in the arithmetic "
            "between two metrics."
        ),
        prom_expected=_stalled(SH201_EXPECTED, "40"),
        prom_completed=_stalled(SH201_COMPLETED, "17"),
        logs=_EMPTY_LOKI,
        expect_problem=True,
        cause_must_mention=("frame", "incomplete", "17", "40", "stall", "not complete"),
    ),
    Scenario(
        name="no_telemetry",
        shot_id="dailies:SEQ01:SH999:ghost",
        why=(
            "The anti-fabrication case, and the reason this harness exists. Every query "
            "returns empty. A model asked for a cause will produce a plausible one "
            "whether or not it looked, so an answer here naming a specific asset is an "
            "answer invented whole."
        ),
        prom_expected=_EMPTY_PROM,
        prom_completed=_EMPTY_PROM,
        logs=_EMPTY_LOKI,
        # Either verdict can be honest here: "nothing is wrong that I can see" and "I
        # cannot tell whether anything is wrong" are both truthful readings of no data.
        # What is never honest is naming a file nobody showed it.
        expect_problem=None,
        cause_must_not_mention=("jacket_diffuse", ".exr", "out of memory", "oom"),
    ),
)
