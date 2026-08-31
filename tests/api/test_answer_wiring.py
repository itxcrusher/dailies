"""Stored answers come back after a restart.

Cloud Run replaces the instance on every deploy, so without this a judge opening the
board sees a farm nobody has ever asked about, and pressing Diagnose spends a Vertex
call rediscovering what the system concluded an hour ago.
"""

from typing import ClassVar

from dailies_api.main import create_app
from dailies_api.state import Shot, ShotStore
from fastapi.testclient import TestClient

SHOT = "dailies:SEQ01:SH201:job-1"
DIAGNOSIS = {
    "shot": "SH201",
    "problem_found": True,
    "cause": "a required asset was missing",
    "evidence": [{"query": "q", "finding": "f"}],
    "confidence": "high",
}
VISUAL = {"verdict": "suspect", "observation": "a flat magenta cube", "confidence": "high"}


class Source:
    """Reconstructs the shot from telemetry, exactly as the real one does after a restart."""

    telemetry: ClassVar[dict] = {}

    async def list_shots(self):
        return [Shot(id=SHOT, frames_total=3, frames_done=3)]


class Recording:
    """An answer store backed by a dict, standing in for the bucket."""

    def __init__(self, initial=None):
        self.saved = dict(initial or {})

    async def save(self, shot_id, *, diagnosis, visual):
        self.saved[shot_id] = {"diagnosis": diagnosis, "visual": visual}

    async def load(self, shot_id):
        return self.saved.get(shot_id)


async def diagnoser(shot_id: str) -> dict:
    return DIAGNOSIS


async def looker(shot_id: str) -> dict:
    return VISUAL


def test_a_diagnosis_is_written_when_it_is_produced():
    answers = Recording()
    app = create_app(
        ShotStore(), diagnose=diagnoser, inspect=looker, shot_source=Source(), answers=answers
    )

    TestClient(app).post(f"/api/shots/{SHOT}/diagnose")

    assert answers.saved[SHOT]["diagnosis"]["cause"] == DIAGNOSIS["cause"]
    assert answers.saved[SHOT]["visual"]["verdict"] == "suspect"


def test_a_stored_answer_comes_back_on_a_cold_board():
    """The whole point: a fresh process, an empty store, and the answers are still there."""
    answers = Recording({SHOT: {"diagnosis": DIAGNOSIS, "visual": VISUAL}})
    app = create_app(ShotStore(), shot_source=Source(), answers=answers)

    row = TestClient(app).get("/api/shots").json()["shots"][0]

    assert row["diagnosis"]["cause"] == DIAGNOSIS["cause"]
    assert row["visual"]["verdict"] == "suspect"


def test_a_fresh_answer_is_not_overwritten_by_a_stored_one():
    """What is in memory is newer than what is in the bucket, and must win."""
    answers = Recording({SHOT: {"diagnosis": {"cause": "an older answer"}, "visual": None}})
    store = ShotStore()
    store.upsert(Shot(id=SHOT, frames_total=3, frames_done=3, diagnosis={"cause": "the new one"}))
    app = create_app(store, shot_source=Source(), answers=answers)

    row = TestClient(app).get("/api/shots").json()["shots"][0]

    assert row["diagnosis"]["cause"] == "the new one"


def test_a_board_with_no_answer_store_still_serves():
    """A local run with no bucket is legitimate and must not be a failure."""
    app = create_app(ShotStore(), shot_source=Source(), answers=None)

    assert TestClient(app).get("/api/shots").status_code == 200


def test_a_refresh_does_not_forget_when_an_answer_was_produced():
    """The board rebuilds every shot from telemetry on each page load, and telemetry knows
    nothing about answers. The merge that follows keeps the fields telemetry cannot speak
    to, and `answered_at` and `answer_stale` are two of them.

    Missing them, a freshly diagnosed shot lost its timestamp the instant the board polled
    again, so the report header showed no age at all. Every API test still passed: the
    diagnose route's own response carried the stamp correctly, and only the list route,
    which is what the page actually reads, dropped it. Found by pressing the button on the
    real board and watching "answered just now" fail to appear.
    """
    answers = Recording()
    client = TestClient(
        create_app(
            ShotStore(), diagnose=diagnoser, inspect=looker, shot_source=Source(), answers=answers
        )
    )

    posted = client.post(f"/api/shots/{SHOT}/diagnose").json()
    assert posted["answered_at"] is not None, "the route itself must stamp the answer"

    row = client.get("/api/shots").json()["shots"][0]

    assert row["answered_at"] == posted["answered_at"], "the refresh dropped the timestamp"
    assert row["answer_stale"] is False, "an answer this process just produced is not stale"
