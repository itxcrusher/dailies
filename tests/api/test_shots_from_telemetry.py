"""The board populates itself from telemetry.

Before this, GET /api/shots served an in-memory store that no code path ever wrote to,
so the deployed board read "No shots are being watched yet" while Grafana held four
finished renders. These pin the wiring: shots come from telemetry when the deployment
can reach it, the in-memory store still works when it cannot, and a diagnosis written
onto a derived shot is not thrown away by the next refresh.
"""

from dailies_api.main import create_app
from dailies_api.state import Shot, ShotStore
from fastapi.testclient import TestClient


class FakeSource:
    def __init__(self, shots: list[Shot]) -> None:
        self._shots = shots
        self.calls = 0

    async def list_shots(self) -> list[Shot]:
        self.calls += 1
        return list(self._shots)


def shot(shot_id: str, total: int = 48, done: int = 12) -> Shot:
    return Shot(id=shot_id, frames_total=total, frames_done=done)


def test_the_board_serves_shots_discovered_in_telemetry():
    source = FakeSource([shot("dailies:SEQ01:SH030:job-7")])
    client = TestClient(create_app(shot_source=source))

    body = client.get("/api/shots").json()

    assert [s["id"] for s in body["shots"]] == ["dailies:SEQ01:SH030:job-7"]
    assert body["shots"][0]["frames_total"] == 48
    assert source.calls == 1


def test_a_shot_only_in_telemetry_is_addressable_by_id():
    """The detail and diagnose routes must see what the list route showed.

    A board that lists a shot and then 404s on it is worse than an empty one.
    """
    client = TestClient(create_app(shot_source=FakeSource([shot("dailies:SEQ01:SH030:job-7")])))

    assert client.get("/api/shots/dailies:SEQ01:SH030:job-7").status_code == 200


def test_without_a_source_the_in_memory_store_still_serves():
    store = ShotStore()
    store.upsert(shot("dailies:SEQ01:SH010:local"))
    client = TestClient(create_app(store))

    body = client.get("/api/shots").json()

    assert [s["id"] for s in body["shots"]] == ["dailies:SEQ01:SH010:local"]


def test_a_stored_diagnosis_survives_a_refresh_from_telemetry():
    """Telemetry knows the frame counts; it does not know what the agent concluded.

    A refresh that overwrote the row wholesale would erase the diagnosis the moment the
    board polled again, which is the one thing a supervisor is looking at.
    """
    source = FakeSource([shot("dailies:SEQ01:SH030:job-7", total=48, done=20)])
    app = create_app(shot_source=source)
    client = TestClient(app)

    client.get("/api/shots")
    stored = app.state.shots.get("dailies:SEQ01:SH030:job-7")
    app.state.shots.upsert(stored.model_copy(update={"diagnosis": {"cause": "missing texture"}}))

    body = client.get("/api/shots").json()

    row = body["shots"][0]
    assert row["diagnosis"] == {"cause": "missing texture"}, "the diagnosis must survive"
    assert row["frames_done"] == 20, "and the fresh frame counts must still land"


def test_a_telemetry_failure_does_not_blank_the_board():
    """Grafana being briefly unreachable must not look like 'no renders exist'."""

    class Broken:
        async def list_shots(self):
            raise RuntimeError("grafana said no")

    store = ShotStore()
    store.upsert(shot("dailies:SEQ01:SH010:local"))
    client = TestClient(create_app(store, shot_source=Broken()))

    response = client.get("/api/shots")

    assert response.status_code == 200
    assert [s["id"] for s in response.json()["shots"]] == ["dailies:SEQ01:SH010:local"]
