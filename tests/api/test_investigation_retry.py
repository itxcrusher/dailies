"""A transient rate limit must not reach the board as a failed investigation.

Measured on 2026-08-29 against the deployed project: ten concurrent generateContent
calls as the runtime service account returned two 429s. The trial project's Vertex
concurrency allowance is low, and the ADK agent loop makes several model calls per
investigation (one per tool round trip), so it trips that allowance on almost every run.
The first real end-to-end diagnosis failed for exactly this reason.

429 is the one model-side failure that means "ask again", not "this cannot be answered".
Surfacing it as a 502 tells a supervisor their render cannot be diagnosed when in fact
nothing about the render is wrong.
"""

import pytest
from dailies_api.investigation import RETRY_ATTEMPTS, run_with_retry


class Exhausted(Exception):
    """Stands in for google.adk.models.google_llm._ResourceExhaustedError."""

    def __init__(self) -> None:
        super().__init__("429 RESOURCE_EXHAUSTED. Resource exhausted. Please try again later.")


@pytest.mark.asyncio
async def test_a_rate_limited_call_is_retried_and_succeeds():
    calls = 0

    async def flaky() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise Exhausted()
        return "the answer"

    assert await run_with_retry(flaky, delay=0) == "the answer"
    assert calls == 2


@pytest.mark.asyncio
async def test_it_gives_up_rather_than_retrying_for_ever():
    calls = 0

    async def always() -> str:
        nonlocal calls
        calls += 1
        raise Exhausted()

    with pytest.raises(Exhausted):
        await run_with_retry(always, delay=0)

    assert calls == RETRY_ATTEMPTS, "bounded: the route has a timeout and a caller waiting"


@pytest.mark.asyncio
async def test_a_failure_that_is_not_a_rate_limit_is_not_retried():
    """Retrying a bad model id or a malformed request just burns the caller's minute."""
    calls = 0

    async def broken() -> str:
        nonlocal calls
        calls += 1
        raise ValueError("model not found")

    with pytest.raises(ValueError):
        await run_with_retry(broken, delay=0)

    assert calls == 1


@pytest.mark.asyncio
async def test_a_first_time_success_is_not_delayed():
    async def fine() -> str:
        return "ok"

    assert await run_with_retry(fine, delay=999) == "ok"
