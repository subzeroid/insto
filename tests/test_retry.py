"""Tests for ``insto.backends._retry.with_retry``."""

from __future__ import annotations

import random

import pytest

from insto.backends._retry import with_retry
from insto.exceptions import (
    AuthInvalid,
    BackendError,
    ProfileNotFound,
    QuotaExhausted,
    RateLimited,
    SchemaDrift,
    Transient,
)


class _SleepRecorder:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def _fixed_rng() -> random.Random:
    return random.Random(0)


async def test_succeeds_on_first_attempt() -> None:
    sleep = _SleepRecorder()
    calls = 0

    @with_retry(sleep=sleep, rng=_fixed_rng())
    async def op() -> str:
        nonlocal calls
        calls += 1
        return "ok"

    assert await op() == "ok"
    assert calls == 1
    assert sleep.delays == []


async def test_rate_limited_retries_then_succeeds() -> None:
    sleep = _SleepRecorder()
    calls = 0

    @with_retry(sleep=sleep, rng=_fixed_rng())
    async def op() -> int:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise RateLimited(retry_after=2.0)
        return 42

    assert await op() == 42
    assert calls == 3
    assert len(sleep.delays) == 2
    for delay in sleep.delays:
        assert 2.0 <= delay <= 2.25 + 1e-9


async def test_rate_limited_huge_retry_after_is_capped() -> None:
    sleep = _SleepRecorder()
    calls = 0

    @with_retry(sleep=sleep, rng=_fixed_rng(), max_rate_limit_delay=10.0)
    async def op() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RateLimited(retry_after=999_999.0)
        return "ok"

    assert await op() == "ok"
    assert len(sleep.delays) == 1
    # capped to 10.0 + jitter (<= 0.25)
    assert sleep.delays[0] <= 10.0 + 0.25 + 1e-9


async def test_rate_limited_negative_retry_after_is_clamped() -> None:
    sleep = _SleepRecorder()
    calls = 0

    @with_retry(sleep=sleep, rng=_fixed_rng())
    async def op() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RateLimited(retry_after=-5.0)
        return "ok"

    assert await op() == "ok"
    assert len(sleep.delays) == 1
    assert 0.0 <= sleep.delays[0] <= 0.25 + 1e-9


async def test_transient_retries_with_backoff() -> None:
    sleep = _SleepRecorder()
    calls = 0

    @with_retry(
        max_attempts=4,
        base_delay=1.0,
        max_delay=8.0,
        sleep=sleep,
        rng=_fixed_rng(),
    )
    async def op() -> str:
        nonlocal calls
        calls += 1
        if calls < 4:
            raise Transient("boom")
        return "ok"

    assert await op() == "ok"
    assert calls == 4
    # 3 sleeps; full-jitter -> each in [0, capped_exp]
    assert len(sleep.delays) == 3
    assert 0.0 <= sleep.delays[0] <= 1.0 + 1e-9
    assert 0.0 <= sleep.delays[1] <= 2.0 + 1e-9
    assert 0.0 <= sleep.delays[2] <= 4.0 + 1e-9


async def test_transient_backoff_capped_at_max_delay() -> None:
    sleep = _SleepRecorder()
    calls = 0

    @with_retry(
        max_attempts=10,
        base_delay=1.0,
        max_delay=2.0,
        sleep=sleep,
        rng=_fixed_rng(),
    )
    async def op() -> str:
        nonlocal calls
        calls += 1
        if calls < 6:
            raise Transient()
        return "ok"

    await op()
    assert all(d <= 2.0 + 1e-9 for d in sleep.delays)


@pytest.mark.parametrize(
    "exc",
    [
        AuthInvalid(),
        QuotaExhausted(),
        SchemaDrift("ep", "field"),
        ProfileNotFound("alice"),
    ],
)
async def test_non_retriable_propagates_immediately(exc: BackendError) -> None:
    sleep = _SleepRecorder()
    calls = 0

    @with_retry(sleep=sleep, rng=_fixed_rng())
    async def op() -> None:
        nonlocal calls
        calls += 1
        raise exc

    with pytest.raises(type(exc)):
        await op()
    assert calls == 1
    assert sleep.delays == []


async def test_exhausted_attempts_reraises_last_rate_limited() -> None:
    sleep = _SleepRecorder()
    calls = 0

    @with_retry(max_attempts=3, sleep=sleep, rng=_fixed_rng())
    async def op() -> None:
        nonlocal calls
        calls += 1
        raise RateLimited(retry_after=0.1)

    with pytest.raises(RateLimited):
        await op()
    assert calls == 3
    # 2 sleeps between the 3 attempts; nothing after the final failure
    assert len(sleep.delays) == 2


async def test_exhausted_attempts_reraises_last_transient() -> None:
    sleep = _SleepRecorder()
    calls = 0

    @with_retry(max_attempts=2, sleep=sleep, rng=_fixed_rng())
    async def op() -> None:
        nonlocal calls
        calls += 1
        raise Transient(f"attempt {calls}")

    with pytest.raises(Transient) as info:
        await op()
    assert calls == 2
    assert "attempt 2" in str(info.value)
    assert len(sleep.delays) == 1


async def test_args_and_kwargs_are_forwarded() -> None:
    @with_retry(sleep=_SleepRecorder(), rng=_fixed_rng())
    async def op(a: int, b: int, *, c: int) -> int:
        return a + b + c

    assert await op(1, 2, c=3) == 6


def test_max_attempts_must_be_positive() -> None:
    with pytest.raises(ValueError):
        with_retry(max_attempts=0)
