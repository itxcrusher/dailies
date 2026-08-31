"""The diagnose route must not be a free money tap.

POST /api/shots/{id}/diagnose is bound to allUsers because judges have to reach it, and
every press costs a Vertex Gemini call plus several Grafana queries. Shot ids are
enumerable from the public GET /api/shots, so anyone who can read the board can spend
the project's credits in a loop, bounded only by max_instance_count.

A cooldown is also the better demo behaviour: pressing the button twice should show the
answer instantly, not spend a minute recomputing one that has not changed.
"""

import asyncio

import pytest
from dailies_api.main import DIAGNOSIS_COOLDOWN_SECONDS, create_app
from dailies_api.state import Shot, ShotStore
from fastapi.testclient import TestClient

DIAGNOSIS = {
    "shot": "SH030",
    "cause": "a texture failed to resolve",
    "evidence": [{"query": '{shot="SH030"}', "finding": "Unable to open file"}],
    "confidence": "high",
}


def store_with(shot_id: str = "dailies:SEQ01:SH030:job-7") -> ShotStore:
    store = ShotStore()
    store.upsert(Shot(id=shot_id, frames_total=48, frames_done=12))
    return store


def counting_diagnoser(calls: list[str]):
    async def diagnose(shot_id: str) -> dict:
        calls.append(shot_id)
        return DIAGNOSIS

    return diagnose


def test_a_second_press_inside_the_cooldown_reuses_the_stored_answer():
    calls: list[str] = []
    client = TestClient(create_app(store_with(), diagnose=counting_diagnoser(calls)))
    url = "/api/shots/dailies:SEQ01:SH030:job-7/diagnose"

    first = client.post(url)
    second = client.post(url)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["diagnosis"] == DIAGNOSIS
    assert calls == ["dailies:SEQ01:SH030:job-7"], "the investigator must run exactly once"


def test_the_cooldown_is_per_shot_not_global():
    """One shot's cooldown must never suppress a different shot's first diagnosis."""
    store = store_with()
    store.upsert(Shot(id="dailies:SEQ01:SH040:job-8", frames_total=4, frames_done=4))
    calls: list[str] = []
    client = TestClient(create_app(store, diagnose=counting_diagnoser(calls)))

    client.post("/api/shots/dailies:SEQ01:SH030:job-7/diagnose")
    client.post("/api/shots/dailies:SEQ01:SH040:job-8/diagnose")

    assert len(calls) == 2


def test_the_cooldown_is_long_enough_to_be_worth_having():
    assert DIAGNOSIS_COOLDOWN_SECONDS >= 30


@pytest.mark.asyncio
async def test_two_simultaneous_presses_run_one_investigation():
    """The in-flight guard, which the cooldown alone does not give.

    A cooldown keyed on a stored diagnosis does nothing before the first one finishes,
    and an investigation takes minutes. Without a guard, ten clicks during that window
    are ten concurrent Vertex calls.
    """
    started = 0
    release = asyncio.Event()

    async def slow(shot_id: str) -> dict:
        nonlocal started
        started += 1
        await release.wait()
        return DIAGNOSIS

    from httpx import ASGITransport, AsyncClient

    app = create_app(store_with(), diagnose=slow)
    url = "http://test/api/shots/dailies:SEQ01:SH030:job-7/diagnose"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        both = asyncio.gather(client.post(url), client.post(url))
        await asyncio.sleep(0.05)
        release.set()
        responses = await both

    assert all(r.status_code == 200 for r in responses)
    assert started == 1, "a second press while one is in flight must not start another"


# --- an incomplete answer must not be cached -----------------------------------------
#
# Found by turning the logs on. SH201's visual check failed once, the route still
# returned 200 with a diagnosis, and the cooldown stamped that as a success. For the next
# five minutes every press replayed the stale answer, so the visual check never ran again
# and the shot looked permanently broken. Every "consistent failure" being investigated
# was one old bad answer being handed back.
#
# The cooldown exists to stop a supervisor spending a Vertex call on a question already
# answered. An answer missing half of itself is not one.


def test_an_answer_with_no_visual_verdict_is_not_cached():
    calls: list[str] = []

    async def diagnoser(shot_id: str) -> dict:
        calls.append(shot_id)
        return DIAGNOSIS

    async def failed_look(shot_id: str):
        return None

    client = TestClient(
        create_app(store_with(), diagnose=counting_diagnoser(calls), inspect=failed_look)
    )
    url = "/api/shots/dailies:SEQ01:SH030:job-7/diagnose"

    client.post(url)
    client.post(url)

    assert len(calls) == 2, "an incomplete answer must be retried, not served from the cooldown"


def test_a_complete_answer_is_still_cached():
    """The cooldown must keep doing its job for answers that are actually whole."""
    calls: list[str] = []

    async def good_look(shot_id: str) -> dict:
        return {"verdict": "suspect", "observation": "a magenta cube", "confidence": "high"}

    client = TestClient(
        create_app(store_with(), diagnose=counting_diagnoser(calls), inspect=good_look)
    )
    url = "/api/shots/dailies:SEQ01:SH030:job-7/diagnose"

    client.post(url)
    client.post(url)

    assert len(calls) == 1, "a whole answer should not be recomputed"


def test_with_no_visual_checker_a_diagnosis_alone_is_complete():
    """A deployment with no frames bucket is not permanently uncacheable."""
    calls: list[str] = []
    client = TestClient(create_app(store_with(), diagnose=counting_diagnoser(calls), inspect=None))
    url = "/api/shots/dailies:SEQ01:SH030:job-7/diagnose"

    client.post(url)
    client.post(url)

    assert len(calls) == 1


# --- the button must not lie about what it did ---------------------------------------


def test_a_cached_answer_says_so():
    """ "Re-run" that silently returns the old answer looks like a broken button.

    The cooldown is right to exist: this route is allUsers-bound, shot ids are
    enumerable, and every press costs a Vertex call. But a supervisor pressing Re-run and
    seeing the page not change has learned nothing, and will press it again. The response
    says whether it recomputed, so the board can tell them.
    """
    calls: list[str] = []
    client = TestClient(create_app(store_with(), diagnose=counting_diagnoser(calls)))
    url = "/api/shots/dailies:SEQ01:SH030:job-7/diagnose"

    first = client.post(url)
    second = client.post(url)

    assert first.headers["x-dailies-answer"] == "fresh"
    assert second.headers["x-dailies-answer"] == "cached"
    assert len(calls) == 1


def test_a_cached_answer_says_how_old_it_is():
    """ "Answered a moment ago" is actionable; "nothing happened" is not."""
    calls: list[str] = []
    client = TestClient(create_app(store_with(), diagnose=counting_diagnoser(calls)))
    url = "/api/shots/dailies:SEQ01:SH030:job-7/diagnose"

    client.post(url)
    second = client.post(url)

    age = int(second.headers["x-dailies-answer-age"])
    assert 0 <= age < DIAGNOSIS_COOLDOWN_SECONDS


def test_a_fresh_answer_reports_no_age():
    client = TestClient(create_app(store_with(), diagnose=counting_diagnoser([])))
    first = client.post("/api/shots/dailies:SEQ01:SH030:job-7/diagnose")

    assert first.headers["x-dailies-answer"] == "fresh"
    assert first.headers.get("x-dailies-answer-age") in (None, "0")


def test_a_restored_answer_does_not_start_a_cooldown_nobody_served(monkeypatch):
    """A cold start must not refuse to diagnose a shot this instance never answered.

    Cloud Run scales to zero and replaces the instance on every deploy, so the shot store
    is rebuilt from the persisted answers while the in-memory cooldown map starts empty.
    Absence there means "never diagnosed here", and it has to be distinguishable from
    "diagnosed long ago" rather than defaulting to 0.0, because that default is not a
    point in the distant past. It is the clock's origin, and the clock is per sandbox.

    **The origin was measured, not assumed.** A freshly deployed revision answered a
    restored shot with ``X-Dailies-Answer-Age: 44`` while nothing had yet stamped the
    map, so the reported age was ``time.monotonic()`` itself: 44 seconds after the
    sandbox started. Every restored shot was therefore inside the cooldown for the
    instance's first five minutes, which is exactly the demo path. The service idles
    down between visits, a reviewer opens the board, presses Diagnose, and is told to
    wait five minutes for an answer nobody asked for today.

    The clock is patched because the bug is invisible without it: a developer machine has
    been up for days, so the same defect computes an age of several hundred thousand
    seconds and sails past the cooldown. This test failed only in production until the
    clock became an input.
    """
    # Young, like a fresh sandbox, and still advancing so nothing waiting on elapsed
    # time stalls. A frozen clock would be a different lie from the one being tested.
    ticks = iter(44.0 + n * 0.001 for n in range(1_000_000))
    monkeypatch.setattr("dailies_api.main.time.monotonic", lambda: next(ticks))

    # Built into the store rather than mutated after the fact. ``get`` hands back a deep
    # copy, so setting the diagnosis on a retrieved shot changes nothing the route reads,
    # and this test passed against the broken code until it stopped doing that.
    store = ShotStore()
    store.upsert(
        Shot(id="dailies:SEQ01:SH030:job-7", frames_total=48, frames_done=12, diagnosis=DIAGNOSIS)
    )

    calls: list[str] = []
    client = TestClient(create_app(store, diagnose=counting_diagnoser(calls), inspect=None))

    answer = client.post("/api/shots/dailies:SEQ01:SH030:job-7/diagnose")

    assert answer.status_code == 200
    assert calls == ["dailies:SEQ01:SH030:job-7"], "a restored answer must not block a re-run"
    assert answer.headers["X-Dailies-Answer"] == "fresh"
