"""Tests for the board API and the shot state it serves.

The board is the only thing a supervisor actually looks at, so what is tested here is the
contract the board and the Guardian both code against, not FastAPI itself:

- the **risk vocabulary**. Exactly five members, spelled exactly this way. A renamed or
  extra member is a silent break in two consumers at once, and neither would fail loudly.
- the **404 path**. An unknown shot must be a 404 with a detail that names what was asked
  for. A board that renders "undefined" because the API answered 200 with null is the
  failure this pins.
- the **injected store**. ``create_app`` takes the store so a test never touches global
  state and two apps in one process cannot see each other's shots.

No network and no model: the store is in-memory and the routes are driven through
``TestClient``.
"""

from dailies_api.main import create_app
from dailies_api.state import Risk, Shot, ShotStore
from fastapi.testclient import TestClient


def _client(store: ShotStore | None = None) -> TestClient:
    return TestClient(create_app(store))


def test_shots_endpoint_returns_shot_list():
    client = TestClient(create_app())
    r = client.get("/api/shots")
    assert r.status_code == 200
    assert isinstance(r.json()["shots"], list)


def test_shot_detail_404s_for_unknown_shot():
    client = TestClient(create_app())
    assert client.get("/api/shots/NOPE").status_code == 404


def test_risk_has_exactly_the_five_members_the_board_depends_on():
    assert {member.value for member in Risk} == {
        "ON_TRACK",
        "WATCH",
        "AT_RISK",
        "CRITICAL",
        "MISSED",
    }


def test_shot_defaults_to_on_track_with_no_diagnosis():
    shot = Shot(id="SH010", frames_total=120)
    assert shot.risk is Risk.ON_TRACK
    assert shot.diagnosis is None
    assert shot.frames_done == 0


def test_healthz_reports_ok():
    assert _client().get("/healthz").json() == {"ok": True}


def test_shots_endpoint_serves_the_injected_store():
    store = ShotStore()
    store.upsert(Shot(id="SH040", frames_total=240, frames_done=96, risk=Risk.AT_RISK))
    body = _client(store).get("/api/shots").json()
    assert [shot["id"] for shot in body["shots"]] == ["SH040"]
    assert body["shots"][0]["risk"] == "AT_RISK"


def test_shot_detail_returns_the_stored_shot():
    store = ShotStore()
    store.upsert(
        Shot(
            id="SH040",
            frames_total=240,
            frames_done=96,
            risk=Risk.CRITICAL,
            diagnosis={"cause": "worker OOM"},
        )
    )
    body = _client(store).get("/api/shots/SH040").json()
    assert body == {
        "id": "SH040",
        "frames_total": 240,
        "frames_done": 96,
        "risk": "CRITICAL",
        "diagnosis": {"cause": "worker OOM"},
    }


def test_404_detail_names_the_shot_that_was_asked_for():
    detail = _client().get("/api/shots/NOPE").json()["detail"]
    assert "NOPE" in detail


def test_two_apps_do_not_share_state():
    store = ShotStore()
    store.upsert(Shot(id="SH010", frames_total=10))
    assert _client(store).get("/api/shots/SH010").status_code == 200
    assert _client().get("/api/shots/SH010").status_code == 404


def test_upsert_replaces_a_shot_in_place_and_keeps_order():
    store = ShotStore()
    store.upsert(Shot(id="SH010", frames_total=10))
    store.upsert(Shot(id="SH020", frames_total=20))
    store.upsert(Shot(id="SH010", frames_total=10, frames_done=7))

    assert [shot.id for shot in store.all()] == ["SH010", "SH020"]
    assert store.get("SH010").frames_done == 7


def test_get_returns_none_for_an_unknown_shot():
    assert ShotStore().get("NOPE") is None
