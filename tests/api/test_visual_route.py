"""Pressing Diagnose runs both checks, and one failing does not lose the other.

The two are independent sources: the investigator reads telemetry, Visual QA looks at the
picture. One button runs both, because a supervisor asking "what is wrong with this shot"
wants both answers and should not have to know there are two systems.

What matters most here is that they fail independently. A bucket with no frames, or a
vision call that errors, must not cost the telemetry diagnosis that already succeeded.
"""

from typing import ClassVar

from dailies_api.main import create_app
from dailies_api.state import Shot, ShotStore
from fastapi.testclient import TestClient

DIAGNOSIS = {
    "shot": "SH201",
    "cause": "a required asset was missing",
    "evidence": [{"query": '{shot="SH201"}', "finding": "asset_missing"}],
    "confidence": "high",
}
VERDICT = {"verdict": "suspect", "observation": "a flat magenta cube", "confidence": "high"}


def store_with(shot_id="dailies:SEQ01:SH201:job-1") -> ShotStore:
    store = ShotStore()
    store.upsert(Shot(id=shot_id, frames_total=3, frames_done=3))
    return store


async def diagnoser(shot_id: str) -> dict:
    return DIAGNOSIS


def test_both_verdicts_land_on_the_shot():
    async def look(shot_id: str) -> dict:
        return VERDICT

    app = create_app(store_with(), diagnose=diagnoser, inspect=look)
    body = TestClient(app).post("/api/shots/dailies:SEQ01:SH201:job-1/diagnose").json()

    assert body["diagnosis"]["cause"] == DIAGNOSIS["cause"]
    assert body["visual"]["verdict"] == "suspect"
    assert body["visual"]["observation"] == "a flat magenta cube"


def test_a_shot_with_no_frames_still_gets_its_telemetry_diagnosis():
    """No frames is the ordinary state before a render has written anything."""

    async def nothing(shot_id: str) -> None:
        return None

    app = create_app(store_with(), diagnose=diagnoser, inspect=nothing)
    body = TestClient(app).post("/api/shots/dailies:SEQ01:SH201:job-1/diagnose").json()

    assert body["diagnosis"]["cause"] == DIAGNOSIS["cause"]
    assert body["visual"] is None


def test_a_failing_visual_check_does_not_lose_the_diagnosis():
    """The whole point of running them independently.

    A bucket permission problem or a vision quota error must not turn a successful
    investigation into a 502. The supervisor still gets the answer that worked.
    """

    async def broken(shot_id: str):
        raise RuntimeError("bucket said no")

    app = create_app(store_with(), diagnose=diagnoser, inspect=broken)
    response = TestClient(app).post("/api/shots/dailies:SEQ01:SH201:job-1/diagnose")

    assert response.status_code == 200
    assert response.json()["diagnosis"]["cause"] == DIAGNOSIS["cause"]
    assert response.json()["visual"] is None


def test_a_failing_investigation_is_still_a_502_even_if_the_frame_looked_fine():
    """Visual QA is corroboration, not a substitute. It cannot rescue a broken route."""

    async def exploding(shot_id: str) -> dict:
        raise RuntimeError("model said no")

    async def look(shot_id: str) -> dict:
        return VERDICT

    app = create_app(store_with(), diagnose=exploding, inspect=look)
    assert TestClient(app).post("/api/shots/dailies:SEQ01:SH201:job-1/diagnose").status_code == 502


def test_the_visual_verdict_survives_a_refresh_from_telemetry():
    """Telemetry owns frame counts and knows nothing about what a frame looked like."""

    class Source:
        telemetry: ClassVar[dict] = {}

        async def list_shots(self):
            return [Shot(id="dailies:SEQ01:SH201:job-1", frames_total=3, frames_done=3)]

    async def look(shot_id: str) -> dict:
        return VERDICT

    app = create_app(store_with(), diagnose=diagnoser, inspect=look, shot_source=Source())
    client = TestClient(app)

    client.post("/api/shots/dailies:SEQ01:SH201:job-1/diagnose")
    row = client.get("/api/shots").json()["shots"][0]

    assert row["visual"]["verdict"] == "suspect", "a refresh must not wipe what was seen"
    assert row["diagnosis"] is not None
