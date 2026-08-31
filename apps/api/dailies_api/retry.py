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

__all__ = ["RETRY_ATTEMPTS", "RETRY_DELAY_SECONDS", "is_rate_limited", "run_with_retry"]

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


async def run_with_retry(
    call: Callable[[], Awaitable[T]],
    *,
    attempts: int = RETRY_ATTEMPTS,
    delay: float = RETRY_DELAY_SECONDS,
) -> T:
    """Run ``call``, retrying only a rate limit, with exponential backoff.

    Everything other than a rate limit is raised immediately and unchanged.
    """
    backoff = delay
    for attempt in range(1, attempts + 1):
        try:
            return await call()
        except Exception as exc:
            if not is_rate_limited(exc) or attempt == attempts:
                raise
            _log.warning(
                "Model call rate-limited (attempt %d of %d); retrying in %.1fs",
                attempt,
                attempts,
                backoff,
            )
            await asyncio.sleep(backoff)
            backoff *= 2
    raise AssertionError("unreachable: the loop either returns or raises")  # pragma: no cover
