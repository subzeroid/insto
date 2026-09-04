"""Tests for persistent-state scheduling in `WatchManager`."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import pytest

from insto.exceptions import Banned, Transient
from insto.models import WatchSpec
from insto.service.watch import WatchError, WatchManager
from insto.service.watch_lock import WatchProcessLock


def _spec(
    user: str = "alice",
    *,
    registration_id: str = "reg-1",
    interval: int = 300,
    last_ok: int | None = None,
    last_error: str | None = None,
    consecutive_errors: int = 0,
) -> WatchSpec:
    return WatchSpec(
        user=user,
        registration_id=registration_id,
        interval_seconds=interval,
        last_ok=last_ok,
        last_error=last_error,
        consecutive_errors=consecutive_errors,
    )


def _manager(tmp_path: Path, *, release_when_empty: bool = False) -> WatchManager:
    return WatchManager(
        WatchProcessLock(tmp_path / "store.db"),
        release_when_empty=release_when_empty,
        now=lambda: 1234,
    )


async def _accept(_: WatchSpec) -> bool:
    return True


async def test_manager_requires_executor_and_rejects_duplicates(tmp_path: Path) -> None:
    manager = _manager(tmp_path)

    async def tick() -> None:
        return None

    with pytest.raises(WatchError, match="executor"):
        manager.add(_spec(), tick=tick, state_changed=_accept, start=False)

    manager.acquire_executor()
    manager.add(_spec(), tick=tick, state_changed=_accept, start=False)
    with pytest.raises(WatchError, match="already watching"):
        manager.add(_spec(), tick=tick, state_changed=_accept, start=False)
    await manager.cancel_all()
    manager.release_executor()


async def test_recovered_failure_streak_pauses_on_next_failed_tick(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager.acquire_executor()
    seen: list[WatchSpec] = []
    calls = 0

    async def tick() -> None:
        nonlocal calls
        calls += 1
        raise Transient("network blip")

    async def state_changed(spec: WatchSpec) -> bool:
        seen.append(spec)
        return True

    manager.add(
        _spec(consecutive_errors=1),
        tick=tick,
        state_changed=state_changed,
        start=False,
    )
    updated = await manager.tick_once("alice")

    assert calls == 2
    assert updated.status == "paused"
    assert updated.consecutive_errors == 2
    assert "network blip" in (updated.last_error or "")
    assert seen == [updated]
    await manager.cancel_all()
    manager.release_executor()


async def test_success_resets_recovered_error_state(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager.acquire_executor()
    seen: list[WatchSpec] = []

    async def tick() -> None:
        return None

    async def state_changed(spec: WatchSpec) -> bool:
        seen.append(spec)
        return True

    manager.add(
        _spec(last_error="old", consecutive_errors=1),
        tick=tick,
        state_changed=state_changed,
        start=False,
    )
    updated = await manager.tick_once("alice")

    assert updated.last_ok == 1234
    assert updated.last_error is None
    assert updated.consecutive_errors == 0
    assert seen == [updated]
    await manager.cancel_all()
    manager.release_executor()


async def test_hard_failure_pauses_without_retry(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager.acquire_executor()
    calls = 0

    async def tick() -> None:
        nonlocal calls
        calls += 1
        raise Banned("account suspended")

    manager.add(_spec(), tick=tick, state_changed=_accept, start=False)
    updated = await manager.tick_once("alice")

    assert calls == 1
    assert updated.status == "paused"
    assert "suspended" in (updated.last_error or "")
    await manager.cancel_all()
    manager.release_executor()


async def test_false_state_callback_stops_stale_loop(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager.acquire_executor()
    callback_seen = asyncio.Event()
    calls = 0

    async def tick() -> None:
        nonlocal calls
        calls += 1

    async def stale(_: WatchSpec) -> bool:
        callback_seen.set()
        return False

    manager.add(_spec(interval=1), tick=tick, state_changed=stale, initial_delay=0)
    await asyncio.wait_for(callback_seen.wait(), timeout=1)
    await asyncio.sleep(0.02)
    assert calls == 1
    await manager.cancel_all()
    manager.release_executor()


async def test_callback_exception_reports_fatal_error(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager.acquire_executor()
    failure = RuntimeError("sqlite failed")

    async def tick() -> None:
        return None

    async def broken(_: WatchSpec) -> bool:
        raise failure

    manager.add(_spec(interval=1), tick=tick, state_changed=broken, initial_delay=0)
    reported = await asyncio.wait_for(manager.fatal_error, timeout=1)
    assert reported is failure
    await manager.cancel_all()
    manager.release_executor()


async def test_cancel_all_drains_in_flight_tick_before_repl_release(tmp_path: Path) -> None:
    manager = _manager(tmp_path, release_when_empty=True)
    manager.acquire_executor()
    started = asyncio.Event()
    finished = asyncio.Event()

    async def tick() -> None:
        started.set()
        try:
            await asyncio.sleep(60)
        finally:
            finished.set()

    manager.add(_spec(), tick=tick, state_changed=_accept, start=False)
    task = asyncio.create_task(manager.tick_once("alice"))
    await started.wait()
    await manager.cancel_all()

    assert finished.is_set()
    assert manager.executor_acquired is False
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_daemon_cancel_all_retains_lock_until_explicit_release(tmp_path: Path) -> None:
    manager = _manager(tmp_path, release_when_empty=False)
    manager.acquire_executor()
    await manager.cancel_all()
    assert manager.executor_acquired is True
    manager.release_executor()
    assert manager.executor_acquired is False


async def test_slow_tick_never_overlaps_or_queues_catch_up(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager.acquire_executor()
    started = asyncio.Event()
    release = asyncio.Event()
    active = 0
    peak = 0
    calls = 0

    async def tick() -> None:
        nonlocal active, calls, peak
        calls += 1
        active += 1
        peak = max(peak, active)
        started.set()
        try:
            await release.wait()
        finally:
            active -= 1

    manager.add(_spec(interval=1), tick=tick, state_changed=_accept, initial_delay=0)
    await started.wait()
    await asyncio.sleep(0.02)
    assert calls == 1
    assert peak == 1
    release.set()
    await asyncio.sleep(0.02)
    assert calls == 1
    await manager.cancel_all()
    manager.release_executor()
