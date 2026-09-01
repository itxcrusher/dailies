"""An empty board must say WHY it is empty.

The board rebuilds itself from telemetry on every load and holds no state of its own, so
a Grafana hiccup empties it. Until now the page then read:

    0 WATCHED. No shots are being watched yet. Run a render and this board fills itself.

Which is false in that situation. The farm is not idle; the telemetry source could not be
read. A supervisor is told the system is working and has nothing to do, when what actually
happened is that the thing feeding it broke.

Observed on 2026-09-01: a transient 503 from Grafana's datasource API emptied the live
board while three renders sat in Prometheus. It is this project's own thesis pointed at
itself, for the third time in a day: **a failure presenting as "nothing here" rather than
as an error.** It matters more than it used to, because the board is now live through four
weeks of judging and every page load depends on Grafana answering.

An unconfigured deployment is deliberately NOT a failure. With no source wired there is
genuinely nothing to read, and "no shots yet" is the honest thing to say.
"""

from typing import ClassVar

import pytest
from dailies_api.main import create_app
from dailies_api.state import Shot, ShotStore
from fastapi.testclient import TestClient


class Working:
    telemetry: ClassVar[dict] = {}

    async def list_shots(self):
        return [Shot(id="dailies:SEQ01:SH200:vqa-good", frames_total=4, frames_done=4)]


class Broken:
    telemetry: ClassVar[dict] = {}

    async def list_shots(self):
        raise RuntimeError("getting backend: get datasource by uid grafanacloud-prom (status 503)")


def test_a_readable_source_reports_readable():
    body = TestClient(create_app(ShotStore(), shot_source=Working())).get("/api/shots").json()
    assert body["telemetry_readable"] is True
    assert len(body["shots"]) == 1


def test_a_failing_source_reports_unreadable():
    body = TestClient(create_app(ShotStore(), shot_source=Broken())).get("/api/shots").json()
    assert body["telemetry_readable"] is False
    assert body["shots"] == []


def test_no_source_configured_is_not_a_failure():
    """A deployment with nothing wired has nothing to read, and saying "no shots yet" is
    the truth. Reporting it as a telemetry failure would cry wolf on every local run."""
    body = TestClient(create_app(ShotStore())).get("/api/shots").json()
    assert body["telemetry_readable"] is True


def test_a_failing_source_still_serves_what_is_already_held():
    """Unchanged behaviour, pinned because the fix must not turn a degraded board into a
    broken one. A supervisor mid-incident would rather see the last known state, labelled,
    than an empty page."""
    store = ShotStore()
    store.upsert(Shot(id="dailies:SEQ01:SH201:vqa-bad", frames_total=4, frames_done=4))
    body = TestClient(create_app(store, shot_source=Broken())).get("/api/shots").json()
    assert len(body["shots"]) == 1, "held shots must survive a telemetry failure"
    assert body["telemetry_readable"] is False, "and must be labelled as possibly stale"


@pytest.mark.parametrize("source", [Working(), None])
def test_the_field_defaults_to_true_so_a_stale_client_is_not_alarmed(source):
    """The board treats false as "something is wrong". Anything that cannot answer the
    question must therefore answer true, or every deployment that has not been rebuilt
    would show a telemetry warning it has no evidence for."""
    kwargs = {"shot_source": source} if source is not None else {}
    body = TestClient(create_app(ShotStore(), **kwargs)).get("/api/shots").json()
    assert body["telemetry_readable"] is True
