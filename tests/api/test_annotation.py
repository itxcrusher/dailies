"""A finding has to reach the Grafana timeline, not just the board.

The agent has had `create_annotation` in its toolset since the start, `mcp_client`
implements it, and the instructions mention that an annotation "puts your conclusion on
the Grafana timeline". Measured on 2026-08-31: **zero annotations in seven days, across
seventeen investigations.** The instruction is descriptive rather than directive, so a
model asked to diagnose answers the question it was asked and never calls the tool.

The project's own positioning claims Dailies does what the Grafana track's reference
agent does, and annotating the dashboard is one of those four things. It did not.

So the write is deterministic rather than another sentence in the prompt. This is the
same reasoning as the evidence schema, which REFUSES a diagnosis with no queries behind
it instead of asking the model nicely: if "every finding lands on the timeline" is a
property of the system, it cannot be a coin flip on whether the model felt like calling a
tool. The tool stays in the agent's kit for its own use mid-investigation; this is the
floor under it.
"""

from typing import Any

import pytest
from dailies_api.main import create_app
from dailies_api.state import Shot, ShotStore
from fastapi.testclient import TestClient

SHOT = "dailies:SEQ01:SH201:job-1"
FOUND = {
    "shot": "SH201",
    "problem_found": True,
    "cause": "A required asset was missing: /assets/jacket_diffuse.exr",
    "evidence": [{"query": "q", "finding": "Unable to open file"}],
    "confidence": "high",
}
CLEAN = {
    "shot": "SH200",
    "problem_found": False,
    "cause": "No render-domain failures were recorded for this shot.",
    "evidence": [{"query": "q", "finding": "no log entries"}],
    "confidence": "high",
}


class Recorder:
    """Stands in for the Grafana MCP write path."""

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail = fail

    async def __call__(self, shot_id: str, diagnosis: dict) -> None:
        if self.fail:
            raise RuntimeError("Grafana is unreachable")
        self.calls.append({"shot_id": shot_id, "diagnosis": diagnosis})


def client_for(diagnosis: dict, annotator: Recorder) -> TestClient:
    async def diagnose(shot_id: str) -> dict:
        return diagnosis

    store = ShotStore()
    store.upsert(Shot(id=SHOT, frames_total=1, frames_done=1))
    return TestClient(create_app(store, diagnose=diagnose, inspect=None, annotate=annotator))


def test_a_finding_reaches_the_timeline():
    annotator = Recorder()
    assert client_for(FOUND, annotator).post(f"/api/shots/{SHOT}/diagnose").status_code == 200
    assert len(annotator.calls) == 1
    assert annotator.calls[0]["shot_id"] == SHOT
    assert "jacket_diffuse" in annotator.calls[0]["diagnosis"]["cause"]


def test_a_healthy_shot_writes_nothing():
    """A timeline marked on every shot is a timeline nobody reads.

    The annotation means "something is wrong here". Writing one for a clean render makes
    the mark meaningless, and the board already carries the clean answer.
    """
    annotator = Recorder()
    assert client_for(CLEAN, annotator).post(f"/api/shots/{SHOT}/diagnose").status_code == 200
    assert annotator.calls == []


def test_a_diagnosis_with_no_verdict_writes_nothing():
    """Absence of `problem_found` is not a problem found. An older stored answer has no
    such key, and defaulting a missing verdict to true would mark the timeline on the
    strength of a field nobody set."""
    annotator = Recorder()
    without = {k: v for k, v in FOUND.items() if k != "problem_found"}
    assert client_for(without, annotator).post(f"/api/shots/{SHOT}/diagnose").status_code == 200
    assert annotator.calls == []


def test_a_failed_annotation_never_costs_the_diagnosis():
    """Best-effort, exactly like persistence.

    The supervisor asked what is wrong with a shot. Losing that answer because a write
    to a dashboard failed would trade the thing they wanted for a thing they did not.
    """
    annotator = Recorder(fail=True)
    response = client_for(FOUND, annotator).post(f"/api/shots/{SHOT}/diagnose")
    assert response.status_code == 200
    assert response.json()["diagnosis"]["cause"] == FOUND["cause"]


@pytest.mark.asyncio
async def test_the_annotation_time_is_milliseconds_not_seconds():
    """Grafana's unit is epoch MILLISECONDS, and seconds is a silent wrong answer.

    `mcp_client.create_annotation` documents this: passing seconds puts the annotation in
    1970 and the call still succeeds. Nothing errors, nothing is logged, and the mark is
    simply somewhere nobody will scroll to. That is this repo's recurring failure shape,
    so the unit is pinned by a test rather than by a comment.
    """
    import time

    from dailies_api.annotate import annotation_for

    payload = annotation_for(SHOT, FOUND, now_epoch=1_700_000_000.0)
    assert payload["time_ms"] == 1_700_000_000_000
    # Sanity against the real clock: a seconds value would land in 1970, decades adrift.
    live = annotation_for(SHOT, FOUND, now_epoch=time.time())
    assert live["time_ms"] > 1_600_000_000_000


def test_the_annotation_says_what_was_found_and_where():
    """Hover text is the whole value of a mark on a timeline. A vertical line saying
    "dailies" tells a viewer that something happened and nothing about what."""
    from dailies_api.annotate import annotation_for

    payload = annotation_for(SHOT, FOUND, now_epoch=1_700_000_000.0)
    assert "SH201" in payload["text"]
    assert "jacket_diffuse.exr" in payload["text"]
    assert "dailies" in payload["tags"]
    assert "SH201" in payload["tags"], "filterable by shot, or the timeline cannot be read per shot"
