"""Answers survive a restart.

Cloud Run replaces the instance on every deploy and scales to zero when idle, so the
in-memory ShotStore loses every diagnosis and every visual verdict. A judge opening the
board cold sees a farm nobody has ever asked about, and pressing Diagnose costs a Vertex
call to rediscover something the system already knew.

Object storage rather than a database, and the trade is deliberate. There is already a
bucket, a client and IAM for it; a database would be a service to provision, a dependency
to declare and a connection to manage, for a workload that is one small write per
diagnosis and one read per shot by exact key. Cloud Storage is strongly consistent for a
read of a known object name, which is the only access pattern here.

What it is NOT suitable for is concurrent writers to one key, and that is fine: the
diagnose route already holds a per-shot lock, so two investigations of the same shot
cannot race.
"""

import json

import pytest
from dailies_api.answers import AnswerStore, object_name


def test_a_shot_id_becomes_a_safe_object_name():
    # Ids carry colons. Legal in an object name, but encoding keeps the key readable in a
    # bucket listing and survives an id scheme that later admits a slash.
    name = object_name("dailies:SEQ01:SH201:vqa-bad")
    assert name.startswith("answers/")
    assert "/" not in name[len("answers/") :], "the id must not create a directory"
    assert name.endswith(".json")


def test_different_shots_never_collide():
    a = object_name("dailies:SEQ01:SH201:job-1")
    b = object_name("dailies:SEQ01:SH201:job-2")
    assert a != b


@pytest.mark.asyncio
async def test_an_answer_survives_being_written_and_read_back():
    written: dict[str, bytes] = {}

    async def write(name: str, data: bytes) -> None:
        written[name] = data

    async def read(name: str) -> bytes | None:
        return written.get(name)

    store = AnswerStore(write_object=write, read_object=read, produced_by="test-agent")
    await store.save(
        "dailies:SEQ01:SH201:job-1", diagnosis={"cause": "x"}, visual={"verdict": "suspect"}
    )

    back = await store.load("dailies:SEQ01:SH201:job-1")
    assert back is not None
    assert back["diagnosis"] == {"cause": "x"}
    assert back["visual"] == {"verdict": "suspect"}


@pytest.mark.asyncio
async def test_a_shot_nobody_asked_about_reads_back_as_nothing():
    async def write(name: str, data: bytes) -> None:  # pragma: no cover
        raise AssertionError

    async def read(name: str) -> bytes | None:
        return None

    store = AnswerStore(write_object=write, read_object=read, produced_by="test-agent")
    assert await store.load("dailies:SEQ01:SH999:job-1") is None


@pytest.mark.asyncio
async def test_corrupt_stored_json_is_ignored_rather_than_crashing_the_board():
    """A half-written object must cost one shot's history, never the page."""

    async def write(name: str, data: bytes) -> None:  # pragma: no cover
        raise AssertionError

    async def read(name: str) -> bytes | None:
        return b"{not json"

    store = AnswerStore(write_object=write, read_object=read, produced_by="test-agent")
    assert await store.load("dailies:SEQ01:SH201:job-1") is None


@pytest.mark.asyncio
async def test_a_storage_failure_on_save_does_not_lose_the_answer_to_the_caller():
    """Persistence is a convenience. Failing to write must not fail the diagnosis."""

    async def write(name: str, data: bytes) -> None:
        raise RuntimeError("bucket said no")

    async def read(name: str) -> bytes | None:
        return None

    store = AnswerStore(write_object=write, read_object=read, produced_by="test-agent")
    await store.save("dailies:SEQ01:SH201:job-1", diagnosis={"cause": "x"}, visual=None)


@pytest.mark.asyncio
async def test_what_is_written_is_readable_json():
    """A person debugging at 2am should be able to `gcloud storage cat` this."""
    written: dict[str, bytes] = {}

    async def write(name: str, data: bytes) -> None:
        written[name] = data

    async def read(name: str) -> bytes | None:
        return written.get(name)

    store = AnswerStore(write_object=write, read_object=read, produced_by="test-agent")
    await store.save("dailies:SEQ01:SH201:job-1", diagnosis={"cause": "x"}, visual=None)

    payload = json.loads(next(iter(written.values())).decode())
    assert payload["shot_id"] == "dailies:SEQ01:SH201:job-1"
    assert "saved_at" in payload, "when it was answered matters when reading it back later"
