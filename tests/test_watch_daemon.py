"""Integration tests for SQLite-to-scheduler watch reconciliation."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Generator
from pathlib import Path

import pytest

from insto._redact import register_secret
from insto.models import WatchSpec
from insto.service.history import HistoryStore
from insto.service.watch import TickFn, WatchManager
from insto.service.watch_daemon import (
    WatchDaemon,
    estimate_watch_load,
    initial_watch_delay,
    startup_offsets,
)
from insto.service.watch_lock import WatchProcessLock


@pytest.fixture
def history(tmp_path: Path) -> Generator[HistoryStore, None, None]:
    store = HistoryStore(tmp_path / "store.db")
    try:
        yield store
    finally:
        store.close()


def _manager(history: HistoryStore, *, repl: bool = False) -> WatchManager:
    return WatchManager(
        WatchProcessLock(history.path),
        release_when_empty=repl,
        now=lambda: 1_000,
    )


def _ticks(calls: list[str]) -> Callable[[str], TickFn]:
    def factory(user: str) -> TickFn:
        async def tick() -> None:
            calls.append(user)

        return tick

    return factory


def _persist_state(
    store: HistoryStore,
    spec: WatchSpec,
    *,
    last_ok: int | None,
) -> WatchSpec:
    assert store.update_watch_state(spec, last_ok=last_ok)
    current = store.get_watch(spec.user)
    assert current is not None
    return current


def test_initial_delay_and_startup_offsets_are_deterministic() -> None:
    no_history = WatchSpec("carol", "c", 300)
    future = WatchSpec("dave", "d", 300, last_ok=950)
    overdue_b = WatchSpec("bob", "b", 300, last_ok=600)
    overdue_a = WatchSpec("alice", "a", 300, last_ok=600)

    assert initial_watch_delay(no_history, now=1_000) == 300
    assert initial_watch_delay(future, now=1_000) == 250
    assert initial_watch_delay(WatchSpec("rollback", "r", 300, last_ok=1_100), now=1_000) == 400
    assert startup_offsets([overdue_b, future, overdue_a], now=1_000) == {
        "alice": 0.0,
        "bob": 2.0,
        "dave": 250.0,
    }


def test_estimate_watch_load_bounds_backend_calls() -> None:
    specs = [WatchSpec("alice", "a", 300), WatchSpec("bob", "b", 600)]
    estimate = estimate_watch_load(specs)
    assert estimate.ticks_per_hour == 18.0
    assert estimate.backend_calls_per_hour_low == 36.0
    assert estimate.backend_calls_per_hour_high == 54.0


async def test_daemon_recovers_active_rows_and_skips_paused(history: HistoryStore) -> None:
    alice = history.register_watch("alice", 300).spec
    bob = history.register_watch("bob", 300).spec
    assert alice is not None and bob is not None
    assert history.update_watch_state(bob, status="paused")
    manager = _manager(history)
    daemon = WatchDaemon(
        history=history,
        manager=manager,
        tick_factory=_ticks([]),
        role="daemon",
        now=lambda: 1_000,
    )

    recovered = await daemon.start()
    assert recovered == 1
    assert [spec.user for spec in manager.list()] == ["alice"]
    assert manager.executor_acquired is True

    await daemon.stop()
    assert manager.executor_acquired is True
    manager.release_executor()


async def test_reconcile_add_remove_pause_and_replace(history: HistoryStore) -> None:
    alice = history.register_watch("alice", 300).spec
    assert alice is not None
    manager = _manager(history)
    daemon = WatchDaemon(
        history=history,
        manager=manager,
        tick_factory=_ticks([]),
        role="daemon",
    )
    await daemon.start()

    bob = history.register_watch("bob", 600).spec
    assert bob is not None
    await daemon.reconcile_once()
    assert [spec.user for spec in manager.list()] == ["alice", "bob"]

    assert history.update_watch_state(alice, status="paused")
    await daemon.reconcile_once()
    assert [spec.user for spec in manager.list()] == ["bob"]

    reactivated = history.register_watch("alice", 900).spec
    assert reactivated is not None
    await daemon.reconcile_once()
    current = manager.get("alice")
    assert current is not None
    assert current.registration_id == reactivated.registration_id
    assert current.interval_seconds == 900

    assert history.delete_watch("bob")
    await daemon.reconcile_once()
    assert [spec.user for spec in manager.list()] == ["alice"]
    await daemon.stop()
    manager.release_executor()


async def test_repl_owner_discovers_rows_from_second_store(history: HistoryStore) -> None:
    other = HistoryStore(history.path)
    manager = _manager(history, repl=True)
    daemon = WatchDaemon(
        history=history,
        manager=manager,
        tick_factory=_ticks([]),
        role="repl",
    )
    try:
        assert await daemon.start() == 0
        assert manager.executor_acquired is False
        assert other.register_watch("alice", 300).kind == "created"
        await daemon.reconcile_once()
        assert manager.executor_acquired is True
        assert [spec.user for spec in manager.list()] == ["alice"]

        assert other.delete_watch("alice")
        await daemon.reconcile_once()
        assert manager.list() == []
        assert manager.executor_acquired is False
    finally:
        await daemon.stop()
        other.close()


async def test_repl_stays_control_only_while_daemon_owns_store(history: HistoryStore) -> None:
    assert history.register_watch("alice", 300).spec is not None
    daemon_manager = _manager(history)
    daemon = WatchDaemon(
        history=history,
        manager=daemon_manager,
        tick_factory=_ticks([]),
        role="daemon",
    )
    await daemon.start()

    repl_manager = _manager(history, repl=True)
    repl = WatchDaemon(
        history=history,
        manager=repl_manager,
        tick_factory=_ticks([]),
        role="repl",
    )
    try:
        assert await repl.start() == 0
        assert repl.control_only is True
        assert repl_manager.list() == []
    finally:
        await repl.stop()
        await daemon.stop()
        daemon_manager.release_executor()


async def test_due_tick_persists_success_and_stale_callback_stops(history: HistoryStore) -> None:
    original = history.register_watch("alice", 300).spec
    assert original is not None
    original = _persist_state(history, original, last_ok=600)
    calls: list[str] = []
    manager = _manager(history)
    daemon = WatchDaemon(
        history=history,
        manager=manager,
        tick_factory=_ticks(calls),
        role="daemon",
        now=lambda: 1_000,
    )
    await daemon.start()
    persisted = history.get_watch("alice")
    for _ in range(50):
        if calls == ["alice"] and persisted is not None and persisted.last_ok == 1_000:
            break
        await asyncio.sleep(0.01)
        persisted = history.get_watch("alice")
    assert calls == ["alice"]
    assert persisted is not None and persisted.last_ok == 1_000

    assert history.delete_watch("alice")
    replacement = history.register_watch("alice", 600).spec
    assert replacement is not None
    assert history.update_watch_state(original, last_error="stale") is False
    assert history.get_watch("alice") == replacement
    await daemon.stop()
    manager.release_executor()


async def test_failed_tick_is_redacted_in_state_and_executor_output(
    history: HistoryStore,
) -> None:
    secret = "watch-secret-123456"
    register_secret(secret)
    assert history.register_watch("alice", 300).spec is not None
    messages: list[str] = []

    def failing_tick_factory(user: str) -> TickFn:
        async def tick() -> None:
            raise RuntimeError(f"backend token={secret}")

        return tick

    manager = _manager(history)
    daemon = WatchDaemon(
        history=history,
        manager=manager,
        tick_factory=failing_tick_factory,
        role="daemon",
        state_output=messages.append,
    )
    await daemon.start()

    state = await manager.tick_once("alice")

    assert state.consecutive_errors == 1
    persisted = history.get_watch("alice")
    assert persisted is not None
    assert persisted.last_error == "backend token=***"
    assert messages == ["@alice: watch error (1/2) · active · backend token=***"]
    await daemon.stop()
    manager.release_executor()


async def test_run_propagates_reconcile_failure_and_drains(
    history: HistoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(history)
    daemon = WatchDaemon(
        history=history,
        manager=manager,
        tick_factory=_ticks([]),
        role="daemon",
        reconcile_seconds=0.01,
    )
    await daemon.start()

    async def broken_list() -> list[WatchSpec]:
        raise RuntimeError("registry failed")

    monkeypatch.setattr(history, "list_watches_async", broken_list)
    with pytest.raises(RuntimeError, match="registry failed"):
        await daemon.run(asyncio.Event())
    assert manager.list() == []
    assert manager.executor_acquired is True
    manager.release_executor()


async def test_start_failure_releases_daemon_lock(
    history: HistoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(history)
    daemon = WatchDaemon(
        history=history,
        manager=manager,
        tick_factory=_ticks([]),
        role="daemon",
    )

    async def broken_list() -> list[WatchSpec]:
        raise RuntimeError("cannot recover")

    monkeypatch.setattr(history, "list_watches_async", broken_list)
    with pytest.raises(RuntimeError, match="cannot recover"):
        await daemon.start()
    assert manager.executor_acquired is False


async def test_run_stops_cleanly_on_event(history: HistoryStore) -> None:
    manager = _manager(history)
    daemon = WatchDaemon(
        history=history,
        manager=manager,
        tick_factory=_ticks([]),
        role="daemon",
        reconcile_seconds=60,
    )
    await daemon.start()
    stop = asyncio.Event()
    task = asyncio.create_task(daemon.run(stop))
    await asyncio.sleep(0)
    stop.set()
    await asyncio.wait_for(task, timeout=1)
    assert manager.executor_acquired is True
    manager.release_executor()
