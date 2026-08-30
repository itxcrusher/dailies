"""The Risk column has to mean something.

Tasks 13 to 15 built the production graph, the forecaster and the guardian, and Task 16
never wired them in. So `Shot.risk` kept its model default and every row on the deployed
board read ON TRACK, including a shot whose asset was missing and a shot with no frames
done at all. A column that says the same thing about every row is worse than no column:
it asserts a verdict nothing computed, in a product whose entire argument is that a
green signal must be earned.

These pin the wiring. The arithmetic itself is already tested in tests/graph and
tests/api/test_guardian.py; what is tested here is that the board actually asks.
"""

import time

from dailies_api.main import create_app
from dailies_api.state import Risk, Shot
from fastapi.testclient import TestClient

HOUR = 3600


def shot(shot_id="dailies:SEQ01:SH010:job-1", total=10, done=2, **kw):
    return Shot(id=shot_id, frames_total=total, frames_done=done, **kw)


class Source:
    """A shot source that also carries what telemetry knows about pace and due date."""

    def __init__(self, shots, telemetry=None):
        self._shots = shots
        self.telemetry = telemetry or {}

    async def list_shots(self):
        return list(self._shots)


def test_a_shot_with_room_to_spare_is_on_track():
    now = int(time.time())
    src = Source(
        [shot(done=9, total=10)],
        # one frame left at ~10s, due in an hour
        {"dailies:SEQ01:SH010:job-1": {"deadline_epoch": now + HOUR, "durations": [10.0] * 9}},
    )
    body = TestClient(create_app(shot_source=src)).get("/api/shots").json()

    row = body["shots"][0]
    assert row["risk"] == Risk.ON_TRACK.value
    assert row["deadline_epoch"] == now + HOUR
    assert row["eta_epoch"] is not None
    assert row["slack_seconds"] > 0


def test_a_shot_that_cannot_finish_in_time_is_not_on_track():
    now = int(time.time())
    src = Source(
        [shot(done=1, total=100)],
        # 99 frames left at 600s each, due in ten minutes: hopeless
        {"dailies:SEQ01:SH010:job-1": {"deadline_epoch": now + 600, "durations": [600.0] * 1}},
    )
    body = TestClient(create_app(shot_source=src)).get("/api/shots").json()

    row = body["shots"][0]
    assert row["risk"] != Risk.ON_TRACK.value, "a shot that misses its date must say so"
    assert row["slack_seconds"] < 0


def test_a_shot_with_no_deadline_reports_no_slack_rather_than_zero():
    """Zero slack means 'exactly on the wire', which is a claim. Absent means absent."""
    src = Source(
        [shot()], {"dailies:SEQ01:SH010:job-1": {"deadline_epoch": None, "durations": [5.0]}}
    )
    row = TestClient(create_app(shot_source=src)).get("/api/shots").json()["shots"][0]

    assert row["deadline_epoch"] is None
    assert row["slack_seconds"] is None


def test_the_eta_carries_its_own_confidence():
    now = int(time.time())
    src = Source(
        [shot(done=4, total=10)],
        {
            "dailies:SEQ01:SH010:job-1": {
                "deadline_epoch": now + HOUR,
                "durations": [10.0, 10.5, 9.8, 10.1],
            }
        },
    )
    row = TestClient(create_app(shot_source=src)).get("/api/shots").json()["shots"][0]

    assert row["confidence"] in {"high", "medium", "low", "unknown"}


def test_a_shot_telemetry_knows_nothing_about_still_renders():
    """No pace and no date is the ordinary state at the top of a render, not an error."""
    src = Source([shot()], {})
    row = TestClient(create_app(shot_source=src)).get("/api/shots").json()["shots"][0]

    assert row["eta_epoch"] is None
    assert row["slack_seconds"] is None
    assert row["confidence"] == "unknown"


def test_a_stored_diagnosis_still_survives_the_refresh():
    """The risk wiring must not undo the merge that keeps an agent's answer on the row."""
    now = int(time.time())
    src = Source(
        [shot()], {"dailies:SEQ01:SH010:job-1": {"deadline_epoch": now + HOUR, "durations": [5.0]}}
    )
    app = create_app(shot_source=src)
    client = TestClient(app)

    client.get("/api/shots")
    held = app.state.shots.get("dailies:SEQ01:SH010:job-1")
    app.state.shots.upsert(held.model_copy(update={"diagnosis": {"cause": "missing texture"}}))

    row = client.get("/api/shots").json()["shots"][0]
    assert row["diagnosis"] == {"cause": "missing texture"}


def test_a_source_without_telemetry_detail_does_not_break_the_board():
    """Any source satisfying the old protocol must keep working."""

    class Old:
        async def list_shots(self):
            return [shot()]

    body = TestClient(create_app(shot_source=Old())).get("/api/shots").json()
    assert len(body["shots"]) == 1
    assert body["shots"][0]["risk"] in {r.value for r in Risk}
