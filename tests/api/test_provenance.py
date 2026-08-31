"""A stored answer must say what produced it, or it cannot be trusted later.

The answer store exists so the board survives a restart. It kept WHAT was concluded and
nothing about WHERE the conclusion came from, and that gap published a wrong verdict.

SH200 read "The render for shot SH200 completed successfully with no logged errors" at
high confidence, on the public board, for days. Its own evidence said "No data returned",
because the queries behind it named a metric that does not exist and a shot id with a typo
in it (`SHG00`). The agent's instructions had since been fixed and re-running produced
correct queries and real findings, so nothing was wrong with the running system: the board
was replaying a conclusion from an older agent and presenting it as current.

That is the same class of failure as every other one in this repo. Nothing errored, the
tests passed, and the result was plausible enough to read straight past.

The project's rule everywhere else is that a claim carries what backs it. A diagnosis
without evidence is refused; a visual verdict names the frame it judged. A stored answer
now names the agent that produced it and when, so the board can say "answered 3h ago" and,
when the agent has moved on since, say that too instead of quietly asserting it is current.
"""

import pytest
from dailies_api.answers import AnswerStore
from dailies_api.provenance import agent_fingerprint, fingerprint_of


class TestTheFingerprint:
    def test_the_same_material_always_gives_the_same_fingerprint(self):
        assert fingerprint_of("gemini-2.5-flash", "instructions") == fingerprint_of(
            "gemini-2.5-flash", "instructions"
        )

    def test_changing_the_instruction_changes_the_fingerprint(self):
        """The SH200 case exactly: the model stayed, the instruction was rewritten."""
        assert fingerprint_of("gemini-2.5-flash", "old prompt") != fingerprint_of(
            "gemini-2.5-flash", "new prompt"
        )

    def test_changing_the_model_changes_the_fingerprint(self):
        assert fingerprint_of("gemini-2.5-flash", "same") != fingerprint_of(
            "gemini-3.0-pro", "same"
        )

    def test_the_parts_cannot_be_run_together_into_one_string(self):
        """Joining on a separator that can appear in the material invites a collision.

        Without a delimiter, ("ab", "c") and ("a", "bc") hash identically, and two
        genuinely different agents would silently share a fingerprint.
        """
        assert fingerprint_of("ab", "c") != fingerprint_of("a", "bc")

    def test_it_is_short_enough_to_read_in_a_log_line(self):
        printed = fingerprint_of("a", "b")
        assert 8 <= len(printed) <= 16
        assert printed.isalnum()

    def test_the_running_agent_has_one(self):
        """Imports the real constants, so a rename that breaks the fingerprint fails here."""
        assert fingerprint_of("x") != agent_fingerprint()
        assert agent_fingerprint() == agent_fingerprint()


def store_recording(written: dict, *, produced_by: str, existing: bytes | None = None):
    async def write(name: str, payload: bytes) -> None:
        written[name] = payload

    async def read(name: str) -> bytes | None:
        return existing

    return AnswerStore(write_object=write, read_object=read, produced_by=produced_by)


class TestWhatIsStored:
    @pytest.mark.asyncio
    async def test_a_saved_answer_records_the_agent_that_produced_it(self):
        written: dict[str, bytes] = {}
        await store_recording(written, produced_by="abc123").save(
            "SH200", diagnosis={"cause": "x"}, visual=None
        )
        import json

        payload = json.loads(next(iter(written.values())))
        assert payload["produced_by"] == "abc123"
        assert isinstance(payload["saved_at"], int)

    @pytest.mark.asyncio
    async def test_an_answer_from_this_agent_is_current(self):
        import json

        stored = json.dumps(
            {"shot_id": "SH200", "saved_at": 1, "produced_by": "abc123", "diagnosis": {}}
        ).encode()
        store = store_recording({}, produced_by="abc123", existing=stored)
        assert (await store.load("SH200"))["current"] is True

    @pytest.mark.asyncio
    async def test_an_answer_from_a_different_agent_is_not_current(self):
        import json

        stored = json.dumps(
            {"shot_id": "SH200", "saved_at": 1, "produced_by": "OLD", "diagnosis": {}}
        ).encode()
        store = store_recording({}, produced_by="abc123", existing=stored)
        assert (await store.load("SH200"))["current"] is False

    @pytest.mark.asyncio
    async def test_an_answer_written_before_provenance_existed_is_not_current(self):
        """Absence is not a match. Every answer in the bucket today has no `produced_by`,
        and treating a missing stamp as "probably fine" would exempt precisely the answers
        this exists to catch."""
        import json

        stored = json.dumps({"shot_id": "SH200", "saved_at": 1, "diagnosis": {}}).encode()
        store = store_recording({}, produced_by="abc123", existing=stored)
        assert (await store.load("SH200"))["current"] is False
