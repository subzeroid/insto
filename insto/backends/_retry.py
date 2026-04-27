"""Retry helper for backend SDK calls.

Wraps async functions that talk to a backend SDK (e.g. ``hikerapi.AsyncClient``)
and retries only the two error classes that are safe to retry:

- ``RateLimited`` — sleeps for ``retry_after`` seconds (plus a small jitter)
  before the next attempt.
- ``Transient`` — exponential backoff with jitter (network blip, 5xx).

All other ``BackendError`` subclasses (``AuthInvalid``, ``QuotaExhausted``,
``SchemaDrift``, ``ProfileNotFound``, …) are propagated immediately. After
``max_attempts`` unsuccessful attempts the *last* retriable error is re-raised
as-is — callers see the original exception with its original context.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import ParamSpec, TypeVar

from insto.exceptions import RateLimited, Transient

P = ParamSpec("P")
R = TypeVar("R")

DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_BASE_DELAY = 0.5
DEFAULT_MAX_DELAY = 30.0
# Hard ceiling on Retry-After honoring. A misbehaving (or hostile) backend
# can claim a multi-day cooldown via header injection; cap so a worker
# never sleeps for an absurd interval. Five minutes covers every legitimate
# 429 we expect from HikerAPI.
DEFAULT_MAX_RATE_LIMIT_DELAY = 300.0


def _transient_delay(
    attempt: int,
    base_delay: float,
    max_delay: float,
    rng: random.Random,
) -> float:
    """Exponential backoff with full jitter, capped at ``max_delay``."""

    exp = base_delay * (2 ** (attempt - 1))
    capped = min(exp, max_delay)
    return rng.uniform(0.0, capped)


def with_retry(
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    base_delay: float = DEFAULT_BASE_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
    max_rate_limit_delay: float = DEFAULT_MAX_RATE_LIMIT_DELAY,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rng: random.Random | None = None,
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Decorate an async function to retry ``RateLimited`` / ``Transient`` errors.

    Parameters
    ----------
    max_attempts:
        Total number of attempts (including the first). Must be >= 1.
    base_delay, max_delay:
        Bounds for the exponential backoff used on ``Transient``.
    sleep:
        Coroutine used to wait between attempts; defaults to ``asyncio.sleep``.
        Tests may inject a recorder to avoid wall-clock waits.
    rng:
        Source of jitter; defaults to a module-level ``random.Random()``.
    """

    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")

    jitter_rng = rng if rng is not None else random.Random()

    def decorator(func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            attempt = 0
            while True:
                attempt += 1
                try:
                    return await func(*args, **kwargs)
                except RateLimited as exc:
                    if attempt >= max_attempts:
                        raise
                    base = min(max(0.0, exc.retry_after), max_rate_limit_delay)
                    delay = base + jitter_rng.uniform(0.0, 0.25)
                    await sleep(delay)
                except Transient:
                    if attempt >= max_attempts:
                        raise
                    delay = _transient_delay(attempt, base_delay, max_delay, jitter_rng)
                    await sleep(delay)

        return wrapper

    return decorator
