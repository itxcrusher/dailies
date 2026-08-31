"""One retry policy, used by both model callers.

A Vertex 429 on this trial project is a brief concurrency spike, not a daily cap, and it
is the one model-side failure meaning "ask again" rather than "this cannot be answered".

The investigator got a retry when its first real end-to-end run came back 502 on one.
Visual QA did not, and then failed the same way in production, more often: the diagnose
route now makes two model calls per press, so it trips the allowance roughly twice as
readily. Duplicating the policy would have meant two places to get the backoff wrong, so
it moved here.
"""

import pytest
from dailies_api.retry import RETRY_ATTEMPTS, is_rate_limited, run_with_retry


class Exhausted(Exception):
    def __init__(self) -> None:
        super().__init__("429 RESOURCE_EXHAUSTED. Resource exhausted. Please try again later.")


def test_a_vertex_rate_limit_is_recognised_by_message_and_by_class_name():
    assert is_rate_limited(Exhausted())
    assert is_rate_limited(RuntimeError("429 RESOURCE_EXHAUSTED"))

    class _ResourceExhaustedError(Exception):
        pass

    assert is_rate_limited(_ResourceExhaustedError("no message about codes"))


def test_an_ordinary_failure_is_not_a_rate_limit():
    assert not is_rate_limited(ValueError("model not found"))
    assert not is_rate_limited(RuntimeError("400 INVALID_ARGUMENT"))


@pytest.mark.asyncio
async def test_a_rate_limited_call_is_retried_and_succeeds():
    calls = 0

    async def flaky() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise Exhausted()
        return "answer"

    assert await run_with_retry(flaky, delay=0) == "answer"
    assert calls == 3


@pytest.mark.asyncio
async def test_it_gives_up_rather_than_retrying_for_ever():
    calls = 0

    async def always() -> str:
        nonlocal calls
        calls += 1
        raise Exhausted()

    with pytest.raises(Exhausted):
        await run_with_retry(always, delay=0)
    assert calls == RETRY_ATTEMPTS, "bounded: a caller is waiting and the route has a timeout"


@pytest.mark.asyncio
async def test_anything_else_is_not_retried():
    calls = 0

    async def broken() -> str:
        nonlocal calls
        calls += 1
        raise ValueError("model not found")

    with pytest.raises(ValueError):
        await run_with_retry(broken, delay=0)
    assert calls == 1, "retrying a retired model id only spends the caller's minute"


@pytest.mark.asyncio
async def test_the_visual_check_retries_a_rate_limit():
    """The regression this module exists for, at the level it actually failed."""
    from dailies_api.visual_qa import check_frame

    calls = 0

    async def flaky(*, image, mime_type, instruction, prompt):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise Exhausted()
        return '{"verdict":"suspect","observation":"a magenta cube","confidence":"high"}'

    verdict = await check_frame(b"x", shot="SH201", model=flaky, retry_delay=0)
    assert verdict["verdict"] == "suspect"
    assert calls == 2
