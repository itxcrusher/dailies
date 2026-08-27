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
- the **shot id**. It is the composite render identity and it has to survive a URL path
  segment, so an id the detail route could never address is rejected where it is built
  rather than 404ing later, and two jobs on one shot stay two rows.
- the **CORS allow-list**. The board is cross-origin, and a missing header there is a
  failure that only exists inside somebody's browser console.

No network and no model: the store is in-memory and the routes are driven through
``TestClient``.
"""

import pytest
from dailies_api.main import CORS_ORIGINS_ENV, cors_origins, create_app
from dailies_api.state import Risk, Shot, ShotStore
from fastapi.testclient import TestClient
from pydantic import ValidationError


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


def test_risk_members_are_ordered_least_to_most_severe():
    # The board sorts by declaration order, so the order is part of the contract and not
    # just how the file happens to read.
    assert [member.name for member in Risk] == [
        "ON_TRACK",
        "WATCH",
        "AT_RISK",
        "CRITICAL",
        "MISSED",
    ]


def test_risk_serialises_as_its_own_name():
    assert (
        Shot(id="SH010", frames_total=1, risk=Risk.MISSED).model_dump(mode="json")["risk"]
        == "MISSED"
    )


def test_store_len_counts_distinct_shots():
    store = ShotStore()
    store.upsert(Shot(id="SH010", frames_total=10))
    store.upsert(Shot(id="SH010", frames_total=10, frames_done=3))
    store.upsert(Shot(id="SH020", frames_total=20))
    assert len(store) == 2


def test_404_detail_reports_how_many_shots_are_watched():
    # Distinguishes a typo'd id from a board watching nothing, which is the whole point
    # of the count being in the message.
    store = ShotStore()
    store.upsert(Shot(id="SH010", frames_total=10))
    assert "1 shot(s)" in _client(store).get("/api/shots/NOPE").json()["detail"]


def test_the_store_owns_its_data_and_callers_cannot_mutate_it_in_place():
    store = ShotStore()
    submitted = Shot(id="SH010", frames_total=10, diagnosis={"cause": "unknown"})
    store.upsert(submitted)

    submitted.frames_done = 999
    submitted.diagnosis["cause"] = "tampered"
    store.get("SH010").frames_done = 42
    store.all()[0].frames_done = 43

    held = store.get("SH010")
    assert held.frames_done == 0
    assert held.diagnosis == {"cause": "unknown"}


def test_create_app_publishes_its_store_on_app_state():
    # The only handle on the store when the app is built by a server factory rather than
    # by a caller who already has one.
    store = ShotStore()
    assert create_app(store).state.shots is store
    assert isinstance(create_app().state.shots, ShotStore)


def test_shot_rejects_an_empty_id():
    with pytest.raises(ValidationError):
        Shot(id="", frames_total=10)


def test_shot_rejects_negative_frame_counts():
    with pytest.raises(ValidationError):
        Shot(id="SH010", frames_total=-1)
    with pytest.raises(ValidationError):
        Shot(id="SH010", frames_total=10, frames_done=-1)


# --- shot id: addressable, and unique per render job ---------------------------------


@pytest.mark.parametrize("bad_id", ["proj/SH040", "SH 040", "SH040?x=1", "SH040#a", "../etc"])
def test_shot_rejects_an_id_the_detail_route_could_not_address(bad_id):
    # The bug this pins: such an id used to construct fine, be listed by GET /api/shots,
    # and then 404 on GET /api/shots/<id> with a detail telling the caller to check the
    # list it was plainly in.
    with pytest.raises(ValidationError):
        Shot(id=bad_id, frames_total=10)


def test_a_composite_id_is_addressable_on_the_detail_route():
    store = ShotStore()
    shot_id = Shot.make_id("bluebird", "SEQ01", "SH040", "job-1")
    store.upsert(Shot(id=shot_id, frames_total=240))

    listed = _client(store).get("/api/shots").json()["shots"][0]["id"]
    assert listed == shot_id
    # Every id the list endpoint hands out must round-trip through the detail route.
    assert _client(store).get(f"/api/shots/{listed}").status_code == 200


def test_make_id_joins_the_four_fields_telemetry_keys_a_render_by():
    assert Shot.make_id("bluebird", "SEQ01", "SH040", "job-1") == "bluebird:SEQ01:SH040:job-1"
    assert Shot.ID_FIELDS == ("project", "sequence", "shot", "render_job")


@pytest.mark.parametrize(
    "parts",
    [
        ("", "SEQ01", "SH040", "job-1"),
        ("blue/bird", "SEQ01", "SH040", "job-1"),
        ("bluebird", "SEQ01", "SH040", "job:1"),
        ("bluebird", "SEQ 01", "SH040", "job-1"),
    ],
)
def test_make_id_rejects_a_component_that_would_corrupt_the_id(parts):
    with pytest.raises(ValueError):
        Shot.make_id(*parts)


def test_two_jobs_rendering_the_same_shot_are_two_rows():
    # Keyed by shot label alone these would land on one store key and the board would show
    # one row of interleaved frame counts.
    store = ShotStore()
    first = Shot.make_id("bluebird", "SEQ01", "SH040", "job-1")
    retry = Shot.make_id("bluebird", "SEQ01", "SH040", "job-2")
    store.upsert(Shot(id=first, frames_total=240, frames_done=96, risk=Risk.AT_RISK))
    store.upsert(Shot(id=retry, frames_total=240, frames_done=4))

    assert [shot.id for shot in store.all()] == [first, retry]
    assert store.get(first).frames_done == 96
    assert store.get(retry).risk is Risk.ON_TRACK


# --- CORS: the board is a different origin -------------------------------------------


BOARD = "http://localhost:3000"


def test_the_board_origin_may_read_the_shot_list():
    r = _client().get("/api/shots", headers={"Origin": BOARD})
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == BOARD


def test_a_preflight_from_the_board_is_answered():
    r = _client().options(
        "/api/shots",
        headers={
            "Origin": BOARD,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == BOARD
    assert "GET" in r.headers["access-control-allow-methods"]


def test_an_origin_that_is_not_allow_listed_gets_no_cors_header():
    r = TestClient(create_app(allow_origins=[BOARD])).get(
        "/api/shots", headers={"Origin": "https://evil.example"}
    )
    # Starlette still answers; it is the missing header that makes the browser drop it.
    assert r.status_code == 200
    assert "access-control-allow-origin" not in r.headers


def test_cors_is_off_when_the_allow_list_is_empty():
    r = TestClient(create_app(allow_origins=[])).get("/api/shots", headers={"Origin": BOARD})
    assert "access-control-allow-origin" not in r.headers


def test_cors_origins_reads_the_environment():
    env = {CORS_ORIGINS_ENV: "https://board.example, https://second.example"}
    assert cors_origins(env) == ["https://board.example", "https://second.example"]
    assert cors_origins({CORS_ORIGINS_ENV: ""}) == []
    assert cors_origins({}) == [BOARD, "http://127.0.0.1:3000"]


def test_empty_store_is_truthy_so_the_or_idiom_cannot_discard_it():
    """An empty ShotStore must not be falsy.

    A store is legitimately empty at startup. If ``__len__`` alone made it falsy,
    ``store or ShotStore()`` would quietly swap a caller's real store for a new
    one and the board would poll an object nothing writes to.
    """
    from dailies_api.state import ShotStore

    store = ShotStore()
    assert len(store) == 0
    assert bool(store) is True
    assert (store or ShotStore()) is store
