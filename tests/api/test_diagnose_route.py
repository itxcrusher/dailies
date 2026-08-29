"""Tests for POST /api/shots/{shot_id}/diagnose.

The one write route on the board API, and the only one that reaches outside the process.
Three things are pinned here, all of them failures that have to be distinguishable from
each other by a supervisor reading the board:

- an unknown shot is a 404, spelled the same way the detail route spells it;
- an **unconfigured** deployment is a 503 that names the missing variable. A missing
  ``DAILIES_MCP_URL`` must never render as a shot with nothing wrong;
- a diagnosis that came back is stored on the shot, so the board shows it on the next
  poll rather than only in the response to whoever pressed the button.

Nothing here reaches Gemini or Grafana: the diagnoser is injected.
"""

import json
import logging

import pytest
from dailies_api.investigation import InvestigationFailed
from dailies_api.main import (
    LOKI_UID_ENV,
    MCP_URL_ENV,
    PROMETHEUS_UID_ENV,
    create_app,
    mcp_settings,
)
from dailies_api.state import Shot, ShotStore
from fastapi import HTTPException
from fastapi.testclient import TestClient

DIAGNOSIS = {
    "shot": "SH030",
    "cause": "jacket_diffuse.exr was missing, so 12 delivered frames are untextured.",
    "evidence": [{"query": '{shot="SH030"} |= "Unable to open"', "finding": "WARN on 40-52"}],
    "confidence": "high",
}


def store_with(shot_id="SH030", **fields):
    store = ShotStore()
    store.upsert(Shot(id=shot_id, frames_total=120, **fields))
    return store


async def canned(shot_id):
    return {**DIAGNOSIS, "shot": shot_id}


def test_diagnose_404s_for_an_unknown_shot():
    client = TestClient(create_app(store_with(), diagnose=canned))
    r = client.post("/api/shots/NOPE/diagnose")
    assert r.status_code == 404
    assert "NOPE" in r.json()["detail"]


def test_diagnose_stores_the_diagnosis_on_the_shot_and_returns_it():
    store = store_with()
    client = TestClient(create_app(store, diagnose=canned))

    r = client.post("/api/shots/SH030/diagnose")

    assert r.status_code == 200
    assert r.json()["diagnosis"] == DIAGNOSIS
    # Stored, not merely returned: the board reads the store on its next poll.
    assert store.get("SH030").diagnosis == DIAGNOSIS
    assert client.get("/api/shots/SH030").json()["diagnosis"] == DIAGNOSIS


def test_diagnose_leaves_the_rest_of_the_shot_alone():
    store = store_with(frames_done=52)
    client = TestClient(create_app(store, diagnose=canned))
    body = client.post("/api/shots/SH030/diagnose").json()
    assert body["frames_done"] == 52
    assert body["frames_total"] == 120


def test_diagnose_503s_when_the_mcp_url_is_unset(monkeypatch):
    monkeypatch.delenv(MCP_URL_ENV, raising=False)
    client = TestClient(create_app(store_with()))

    r = client.post("/api/shots/SH030/diagnose")

    assert r.status_code == 503
    detail = r.json()["detail"]
    assert MCP_URL_ENV in detail
    # An unconfigured deployment must not read as a shot with no problems.
    assert client.get("/api/shots/SH030").json()["diagnosis"] is None


def test_the_configuration_is_read_per_request_not_at_startup(monkeypatch):
    """A Cloud Run revision reads its environment; the app is built before it is set.

    The second half stops at the settings helper rather than posting again: the next
    step after a resolved URL is a Cloud Run ID token, and driving that from a test
    would put the metadata server on the other end of it.
    """
    monkeypatch.delenv(MCP_URL_ENV, raising=False)
    client = TestClient(create_app(store_with()))
    assert client.post("/api/shots/SH030/diagnose").status_code == 503

    monkeypatch.setenv(MCP_URL_ENV, "https://mcp.example.invalid")
    assert mcp_settings()["mcp_url"] == "https://mcp.example.invalid"


def test_mcp_settings_reads_all_three_variables():
    settings = mcp_settings(
        {
            MCP_URL_ENV: "https://mcp.example.invalid",
            PROMETHEUS_UID_ENV: "grafanacloud-prom",
            LOKI_UID_ENV: "grafanacloud-logs",
        }
    )
    assert settings == {
        "mcp_url": "https://mcp.example.invalid",
        "prometheus_uid": "grafanacloud-prom",
        "loki_uid": "grafanacloud-logs",
    }


def test_mcp_settings_leaves_an_unset_datasource_uid_unset():
    """Never a default: a UID is per-stack, and a wrong one queries the wrong data."""
    settings = mcp_settings({MCP_URL_ENV: "https://mcp.example.invalid"})
    assert settings["prometheus_uid"] is None
    assert settings["loki_uid"] is None


def test_mcp_settings_refuses_a_missing_url_by_naming_it():
    with pytest.raises(HTTPException) as raised:
        mcp_settings({})
    assert raised.value.status_code == 503
    assert MCP_URL_ENV in raised.value.detail


def test_an_unusable_answer_is_a_502_rather_than_a_stored_diagnosis():
    async def refuses(shot_id):
        raise InvestigationFailed("the model answered prose, not JSON: 'no idea'")

    store = store_with()
    client = TestClient(create_app(store, diagnose=refuses), raise_server_exceptions=False)

    r = client.post("/api/shots/SH030/diagnose")

    assert r.status_code == 502
    assert store.get("SH030").diagnosis is None


def test_a_transport_failure_is_logged_rather_than_returned_verbatim(caplog):
    """Two failures cross this branch in production and neither used to reach a log.

    The transport's own message carries the private MCP endpoint URL and up to 500
    characters of whatever the far side answered - including Cloud Run's raw 401/403
    page. This route is bound to allUsers, so that text belongs in Cloud Logging where
    an operator can read it, and not in a body handed to an anonymous browser.
    """
    from dailies_api.mcp_transport import MCPProtocolError

    upstream = (
        "'tools/call' could not reach the MCP server at "
        "https://dailies-mcp-grafana-000000000000.us-central1.run.app/mcp: "
        "'<html><title>403 Forbidden</title></html>'"
    )

    async def unreachable(shot_id):
        raise MCPProtocolError(upstream)

    store = store_with()
    client = TestClient(create_app(store, diagnose=unreachable), raise_server_exceptions=False)

    with caplog.at_level(logging.WARNING, logger="dailies_api.main"):
        r = client.post("/api/shots/SH030/diagnose")

    assert r.status_code == 502
    detail = r.json()["detail"]
    assert "SH030" in detail
    assert "dailies-mcp-grafana" not in detail
    assert "run.app" not in detail
    assert "403" not in detail
    # The operator's copy survives, in full.
    assert any(upstream in record.getMessage() for record in caplog.records)
    assert store.get("SH030").diagnosis is None


def test_an_unusable_answer_is_logged_as_well_as_returned(caplog):
    """Without this the response body was the only copy of the cause, and it went to a browser."""

    async def refuses(shot_id):
        raise InvestigationFailed("the model answered prose, not JSON: 'no idea'")

    client = TestClient(create_app(store_with(), diagnose=refuses), raise_server_exceptions=False)

    with caplog.at_level(logging.WARNING, logger="dailies_api.main"):
        r = client.post("/api/shots/SH030/diagnose")

    assert r.status_code == 502
    # The investigator's sentence names the model's answer, not an internal host, so it
    # is still passed through as written.
    assert "not JSON" in r.json()["detail"]
    assert any("SH030" in record.getMessage() for record in caplog.records)


def test_the_diagnose_route_is_a_post():
    client = TestClient(create_app(store_with(), diagnose=canned))
    assert client.get("/api/shots/SH030/diagnose").status_code == 405


def test_the_detail_route_still_resolves_beside_the_diagnose_route():
    """`/api/shots/{id}` and `/api/shots/{id}/diagnose` must not shadow each other."""
    client = TestClient(create_app(store_with(shot_id="a:b:c:d"), diagnose=canned))
    assert client.get("/api/shots/a:b:c:d").status_code == 200
    assert client.post("/api/shots/a:b:c:d/diagnose").status_code == 200


@pytest.mark.parametrize("origin", ["https://board.example"])
def test_the_board_origin_may_post_a_diagnosis(origin):
    """The button lives on the board, which is cross-origin. POST has to be allowed."""
    client = TestClient(create_app(store_with(), diagnose=canned, allow_origins=[origin]))
    r = client.options(
        "/api/shots/SH030/diagnose",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
        },
    )
    assert r.status_code == 200
    assert "POST" in r.headers["access-control-allow-methods"]


def test_diagnose_json_is_the_diagnosis_the_investigator_returned():
    """No reshaping between the agent's answer and what the board reads."""
    store = store_with()
    client = TestClient(create_app(store, diagnose=canned))
    body = client.post("/api/shots/SH030/diagnose").json()
    assert json.loads(json.dumps(body["diagnosis"])) == DIAGNOSIS


def test_a_failure_from_the_model_side_is_a_502_that_names_it():
    """Driven against a real server on 2026-08-29: this was a bodyless 500.

    Anything the investigation raises that is not already typed - a Vertex
    misconfiguration, a retired model id, a quota refusal - reached the caller as
    "Internal Server Error" with nothing in it. On the board that is a button that does
    nothing, and in a demo it is unfixable in the moment because the cause is only in
    the server log.
    """

    async def explodes(shot_id):
        raise ValueError("No API key was provided.")

    store = store_with()
    client = TestClient(create_app(store, diagnose=explodes), raise_server_exceptions=False)

    r = client.post("/api/shots/SH030/diagnose")

    assert r.status_code == 502
    detail = r.json()["detail"]
    assert "ValueError" in detail
    assert "No API key was provided." in detail
    assert store.get("SH030").diagnosis is None


def test_mcp_settings_keys_are_exactly_what_build_diagnoser_takes():
    """The route spreads one into the other, and nothing else covers that seam.

    ``build_diagnoser(**mcp_settings())`` is the only call that binds these two
    signatures, and every other test here either injects a diagnoser or stops at the
    settings. Renaming a parameter on one side would otherwise pass the whole suite and
    fail on the first button press in production.
    """
    import inspect

    from dailies_api.investigation import build_diagnoser

    settings = mcp_settings({MCP_URL_ENV: "https://mcp.example.invalid"})
    parameters = inspect.signature(build_diagnoser).parameters
    required = {
        name
        for name, parameter in parameters.items()
        if parameter.default is inspect.Parameter.empty
    }

    assert set(settings) <= set(parameters)
    assert required <= set(settings)
