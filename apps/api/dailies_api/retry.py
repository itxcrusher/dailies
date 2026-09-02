"""Retrying a model call that was rate-limited, and nothing else.

A Vertex 429 on this project is a brief concurrency spike rather than a daily cap.
Measured on 2026-08-29: ten concurrent ``generateContent`` calls as the runtime service
account returned two 429s, while sequential calls never did. The trial project's
allowance is smaller than what an agent loop asks for, since each tool round trip is
another model call.

It is also the one model-side failure that means *ask again* rather than *this cannot be
answered*, which is why it is the only one retried here. Retrying a retired model id or a
malformed request only spends the caller's minute to arrive at the same place.

**Shared because it was needed twice.** The investigator got a retry when its first real
end-to-end run came back 502 on a 429. Visual QA did not, and then failed the same way in
production, more often: the diagnose route makes two model calls per press, so it trips
the allowance roughly twice as readily. Duplicating the policy would have left two places
to get the backoff wrong.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

__all__ = [
    "RETRY_ATTEMPTS",
    "RETRY_DELAY_SECONDS",
    "is_rate_limited",
    "is_transient_server_error",
    "run_with_retry",
]

_log = logging.getLogger(__name__)

T = TypeVar("T")

#: How many times to attempt a call that comes back rate-limited.
#:
#: Bounded, not indefinite: a supervisor is waiting and the route has a timeout, and a
#: retry loop that outlives either is worse than a clear failure. Three covers the
#: observed case, which is a burst rather than an exhausted quota.
RETRY_ATTEMPTS = 3

#: Seconds before the first retry, doubling after that. The allowance recovers in about a
#: second, so the first backoff only has to outlast a burst.
RETRY_DELAY_SECONDS = 2.0


def is_rate_limited(exc: BaseException) -> bool:
    """Whether ``exc`` is the model saying "ask again".

    Matched on the class name and the message rather than by importing the ADK's
    ``_ResourceExhaustedError`` or the genai ``ClientError``. Both are reachable, and one
    is private: depending on a private path at runtime already cost this project an
    entire render when the OTel SDK moved ``LogRecord`` between versions, passing locally
    and dying in the container. The 429 code appears in the message text on every variant
    seen so far, so neither check is load bearing alone.
    """
    return (
        "ResourceExhausted" in type(exc).__name__
        or "RESOURCE_EXHAUSTED" in str(exc)
        or "429" in str(exc)
    )


def is_transient_server_error(exc: BaseException) -> bool:
    """Whether ``exc`` is the far side being briefly unavailable.

    Grafana Cloud intermittently answers ``GET /api/datasources/uid/{uid}`` with a 503, and
    the MCP server resolves the datasource that way on every single tool call. Measured in
    production on 2026-09-02: the board rendered "the telemetry source could not be read"
    on one load and served three shots on the next, with nothing changed in between.

    That one is worth retrying and a 404 is not. A missing datasource stays missing however
    many times it is asked; a 503 is the server saying *not right now*.
    """
    message = str(exc)
    return any(code in message for code in ("status 503", "status 502", "status 504")) or (
        "unavailable" in message.lower()
    )


async def run_with_retry(
    call: Callable[[], Awaitable[T]],
    *,
    attempts: int = RETRY_ATTEMPTS,
    delay: float = RETRY_DELAY_SECONDS,
    retry_on: Callable[[BaseException], bool] = is_rate_limited,
) -> T:
    """Run ``call``, retrying only what ``retry_on`` accepts, with exponential backoff.

    Anything ``retry_on`` rejects is raised immediately and unchanged. The predicate is a
    parameter rather than a fixed rule because the two callers retry different things: a
    model call retries a rate limit, and a telemetry read retries a briefly unavailable
    server. Retrying either condition in the other's place would spend a supervisor's
    minute arriving at the same failure.
    """
    backoff = delay
    for attempt in range(1, attempts + 1):
        try:
            return await call()
        except Exception as exc:
            if not retry_on(exc) or attempt == attempts:
                raise
            _log.warning(
                "Retryable failure (attempt %d of %d); retrying in %.1fs",
                attempt,
                attempts,
                backoff,
            )
            await asyncio.sleep(backoff)
            backoff *= 2
    raise AssertionError("unreachable: the loop either returns or raises")  # pragma: no cover
