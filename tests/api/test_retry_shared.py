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
from dailies_api.retry import (
    RETRY_ATTEMPTS,
    is_rate_limited,
    is_transient_server_error,
    run_with_retry,
)


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


class TestTransientServerErrors:
    """A 503 from Grafana must not empty the board.

    The MCP server resolves the datasource by uid on every tool call, and Grafana Cloud
    intermittently answers that endpoint with a 503. Measured in production on 2026-09-02:
    the board rendered "the telemetry source could not be read" on one load and served
    three shots on the next, with nothing changed in between. Over a four-week judging
    window that is a page some judges open broken.
    """

    def test_a_503_is_worth_asking_again(self):
        error = RuntimeError(
            "Grafana MCP tool 'query_prometheus' reported an error: 'getting backend: get "
            "datasource by uid grafanacloud-prom: [GET /datasources/uid/{uid}] "
            "getDataSourceByUID (status 503): {}'"
        )
        assert is_transient_server_error(error)

    def test_502_and_504_count_too(self):
        assert is_transient_server_error(RuntimeError("bad gateway (status 502)"))
        assert is_transient_server_error(RuntimeError("gateway timeout (status 504)"))

    def test_a_404_is_not_transient(self):
        """A missing datasource stays missing however many times it is asked. Retrying it
        spends a supervisor's wait arriving at the same answer."""
        assert not is_transient_server_error(RuntimeError("datasource not found (status 404)"))

    def test_a_401_is_not_transient(self):
        """An expired token is not going to un-expire on the second attempt."""
        assert not is_transient_server_error(RuntimeError("unauthorized (status 401)"))

    @pytest.mark.asyncio
    async def test_a_transient_failure_is_retried_and_then_succeeds(self):
        attempts = []

        async def flaky():
            attempts.append(1)
            if len(attempts) < 2:
                raise RuntimeError("getDataSourceByUID (status 503)")
            return "three shots"

        result = await run_with_retry(
            flaky, attempts=3, delay=0.0, retry_on=is_transient_server_error
        )
        assert result == "three shots"
        assert len(attempts) == 2

    @pytest.mark.asyncio
    async def test_the_predicate_is_respected_and_a_rate_limit_is_not_retried_here(self):
        """The two callers retry different things. A telemetry read must not sit through a
        model's backoff, and a model call must not retry a datasource outage."""
        attempts = []

        async def rate_limited():
            attempts.append(1)
            raise RuntimeError("429 RESOURCE_EXHAUSTED")

        with pytest.raises(RuntimeError):
            await run_with_retry(
                rate_limited, attempts=3, delay=0.0, retry_on=is_transient_server_error
            )
        assert len(attempts) == 1, "a rate limit is not a transient server error"
