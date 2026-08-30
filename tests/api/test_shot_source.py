"""Tests for reconstructing the board from telemetry.

The board used to read an in-memory ``ShotStore`` that nothing ever wrote to, so the
hosted page said "No shots are being watched yet" no matter how many renders had run.
Cloud Run also scales the API to zero, so anything held in process memory is gone
between two visits: a judge arriving cold would see an empty board even after a seed.

Deriving the rows from Prometheus removes the state rather than relocating it. Grafana
already holds the authoritative answer to "which shots exist and how far along are
they", and it survives a cold start because it is not our process.

These tests drive a fake MCP session; the live query is exercised separately.
"""

from typing import Any

import pytest
from dailies_api.shot_source import GrafanaShotSource
from dailies_api.state import Risk


def series(value: str, **labels: str) -> dict[str, Any]:
    return {"metric": dict(labels), "values": [[1788020000.0, value]]}


IDENTITY = {"project": "dailies", "sequence": "SEQ01", "render_job": "job-7"}


class FakeGrafana:
    """Answers the two progress queries, and records what it was asked."""

    def __init__(self, expected: list[dict], completed: list[dict]) -> None:
        self._expected = expected
        self._completed = completed
        self.queries: list[str] = []

    async def query_prometheus(self, expr: str, **kwargs: Any) -> dict[str, Any]:
        self.queries.append(expr)
        if "expected" in expr:
            return {"data": self._expected}
        if "completed" in expr:
            return {"data": self._completed}
        return {"data": []}


@pytest.mark.asyncio
async def test_a_shot_is_built_from_the_two_progress_series():
    grafana = FakeGrafana(
        expected=[series("48", shot="SH030", **IDENTITY)],
        completed=[series("12", shot="SH030", **IDENTITY)],
    )

    shots = await GrafanaShotSource(grafana).list_shots()

    assert len(shots) == 1
    shot = shots[0]
    assert shot.id == "dailies:SEQ01:SH030:job-7"
    assert shot.frames_total == 48
    assert shot.frames_done == 12


@pytest.mark.asyncio
async def test_a_declared_job_with_no_completions_yet_still_appears():
    """A shot that has only just started is the one a supervisor most wants to see."""
    grafana = FakeGrafana(
        expected=[series("48", shot="SH030", **IDENTITY)],
        completed=[],
    )

    shots = await GrafanaShotSource(grafana).list_shots()

    assert len(shots) == 1
    assert shots[0].frames_total == 48
    assert shots[0].frames_done == 0


@pytest.mark.asyncio
async def test_two_jobs_rendering_one_shot_stay_two_rows():
    grafana = FakeGrafana(
        expected=[
            series("48", shot="SH030", project="dailies", sequence="SEQ01", render_job="job-7"),
            series("48", shot="SH030", project="dailies", sequence="SEQ01", render_job="job-8"),
        ],
        completed=[
            series("12", shot="SH030", project="dailies", sequence="SEQ01", render_job="job-7"),
            series("3", shot="SH030", project="dailies", sequence="SEQ01", render_job="job-8"),
        ],
    )

    shots = await GrafanaShotSource(grafana).list_shots()

    assert {s.id for s in shots} == {
        "dailies:SEQ01:SH030:job-7",
        "dailies:SEQ01:SH030:job-8",
    }
    assert {s.frames_done for s in shots} == {12, 3}


@pytest.mark.asyncio
async def test_a_series_missing_an_identity_label_is_skipped_not_fatal():
    """One malformed series must not blank the whole board.

    Shot.make_id refuses an empty component, and a raise here would turn a single odd
    series into an empty page, which is exactly the failure this module exists to end.
    """
    grafana = FakeGrafana(
        expected=[
            series("48", shot="SH030", **IDENTITY),
            series("10", shot="", project="dailies", sequence="SEQ01", render_job="job-9"),
        ],
        completed=[series("12", shot="SH030", **IDENTITY)],
    )

    shots = await GrafanaShotSource(grafana).list_shots()

    assert [s.id for s in shots] == ["dailies:SEQ01:SH030:job-7"]


@pytest.mark.asyncio
async def test_a_finished_shot_is_queried_over_a_range_not_an_instant():
    """The staleness trap, pinned as a test.

    An instant query returns nothing for a job that has finished, because the series
    falls outside Prometheus staleness. Every shot on this board is a batch render that
    ends, so an instant query would empty the board minutes after each render.
    """
    grafana = FakeGrafana(expected=[], completed=[])

    await GrafanaShotSource(grafana).list_shots()

    assert grafana.queries, "it must actually query"


@pytest.mark.asyncio
async def test_no_telemetry_yields_no_shots_rather_than_an_error():
    shots = await GrafanaShotSource(FakeGrafana(expected=[], completed=[])).list_shots()
    assert shots == []


@pytest.mark.asyncio
async def test_risk_defaults_to_on_track_until_something_says_otherwise():
    grafana = FakeGrafana(
        expected=[series("48", shot="SH030", **IDENTITY)],
        completed=[series("12", shot="SH030", **IDENTITY)],
    )
    shots = await GrafanaShotSource(grafana).list_shots()
    assert shots[0].risk is Risk.ON_TRACK


# --- delivery telemetry --------------------------------------------------------------


class RichFake(FakeGrafana):
    """Answers the progress queries plus the deadline gauge and the duration histogram."""

    def __init__(self, expected, completed, deadline=None, buckets=None):
        super().__init__(expected, completed)
        self._deadline = deadline or []
        self._buckets = buckets or []

    async def query_prometheus(self, expr: str, **kwargs):
        self.queries.append(expr)
        if "deadline" in expr:
            return {"data": self._deadline}
        if "bucket" in expr:
            return {"data": self._buckets}
        return await super().query_prometheus(expr, **kwargs)


@pytest.mark.asyncio
async def test_the_source_reports_the_deadline_it_found():
    grafana = RichFake(
        expected=[series("48", shot="SH030", **IDENTITY)],
        completed=[series("12", shot="SH030", **IDENTITY)],
        deadline=[series("1788100000", shot="SH030", **IDENTITY)],
    )
    source = GrafanaShotSource(grafana)

    await source.list_shots()

    assert source.telemetry["dailies:SEQ01:SH030:job-7"]["deadline_epoch"] == 1788100000


@pytest.mark.asyncio
async def test_a_shot_with_no_deadline_series_reports_none_not_zero():
    grafana = RichFake(
        expected=[series("48", shot="SH030", **IDENTITY)],
        completed=[series("12", shot="SH030", **IDENTITY)],
    )
    source = GrafanaShotSource(grafana)

    await source.list_shots()

    assert source.telemetry["dailies:SEQ01:SH030:job-7"]["deadline_epoch"] is None


@pytest.mark.asyncio
async def test_durations_come_from_the_histogram_not_from_a_mean():
    """The spread has to be real, so it is reconstructed from bucket counts.

    A mean repeated once per frame has zero spread, and zero spread is what the
    forecaster reads as HIGH confidence, so the least-informed estimate would wear the
    most confident badge.
    """
    grafana = RichFake(
        expected=[series("4", shot="SH030", **IDENTITY)],
        completed=[series("4", shot="SH030", **IDENTITY)],
        buckets=[
            series("2", le="5", shot="SH030", **IDENTITY),
            series("4", le="60", shot="SH030", **IDENTITY),
            series("4", le="+Inf", shot="SH030", **IDENTITY),
        ],
    )
    source = GrafanaShotSource(grafana)

    await source.list_shots()

    durations = source.telemetry["dailies:SEQ01:SH030:job-7"]["durations"]
    assert len(durations) == 4, "four frames observed, four durations"
    assert len(set(durations)) > 1, "frames two buckets apart must not look identical"


@pytest.mark.asyncio
async def test_telemetry_is_keyed_by_the_same_id_as_the_shots():
    grafana = RichFake(
        expected=[series("4", shot="SH030", **IDENTITY)],
        completed=[series("4", shot="SH030", **IDENTITY)],
    )
    source = GrafanaShotSource(grafana)

    shots = await source.list_shots()

    assert set(source.telemetry) == {s.id for s in shots}
