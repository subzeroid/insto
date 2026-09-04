"""Tests for shared one-shot, REPL, and daemon resource construction."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import httpx
import pytest

from insto.config import Config
from insto.service.history import HistoryStore
from insto.service.runtime import open_runtime
from insto.service.watch_lock import WatchProcessLock
from tests.fakes import FakeBackend


class ClosingBackend(FakeBackend):
    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self.events = events

    async def aclose(self) -> None:
        self.events.append("backend")


class ClosingClient(httpx.AsyncClient):
    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self.events = events

    async def aclose(self) -> None:
        self.events.append("cdn")
        await super().aclose()


def _config(tmp_path: Path) -> Config:
    return Config(output_dir=tmp_path / "out", db_path=tmp_path / "store.db")


async def test_one_shot_runtime_never_acquires_executor_and_cleans_up(tmp_path: Path) -> None:
    config = _config(tmp_path)
    events: list[str] = []
    backend = ClosingBackend(events)
    client = ClosingClient(events)
    history: HistoryStore | None = None

    async with open_runtime(
        config,
        role="oneshot",
        backend_factory=lambda _: backend,
        cdn_client_factory=lambda _: client,
    ) as runtime:
        history = runtime.history
        assert runtime.role == "oneshot"
        assert runtime.coordinator is None
        assert runtime.manager.executor_acquired is False
        assert runtime.facade.watch_role == "oneshot"

    assert events == ["cdn", "backend"]
    assert history is not None
    with pytest.raises(sqlite3.ProgrammingError):
        history.schema_version()


async def test_daemon_runtime_acquires_before_yield_and_releases_last(tmp_path: Path) -> None:
    config = _config(tmp_path)
    seed = HistoryStore(config.db_path)
    try:
        assert seed.register_watch("alice", 300).spec is not None
    finally:
        seed.close()

    events: list[str] = []
    backend = ClosingBackend(events)
    client = ClosingClient(events)
    runtime_manager = None
    async with open_runtime(
        config,
        role="daemon",
        backend_factory=lambda _: backend,
        cdn_client_factory=lambda _: client,
    ) as runtime:
        runtime_manager = runtime.manager
        assert runtime.coordinator is not None
        assert runtime.manager.executor_acquired is True
        assert [spec.user for spec in runtime.manager.list()] == ["alice"]

    assert events == ["cdn", "backend"]
    assert runtime_manager is not None and runtime_manager.executor_acquired is False
    probe = WatchProcessLock(config.db_path)
    probe.acquire()
    probe.release()


async def test_repl_runtime_claims_lazily_when_registration_appears(tmp_path: Path) -> None:
    config = _config(tmp_path)
    events: list[str] = []
    async with open_runtime(
        config,
        role="repl",
        backend_factory=lambda _: ClosingBackend(events),
        cdn_client_factory=lambda _: ClosingClient(events),
    ) as runtime:
        assert runtime.coordinator is not None
        assert runtime.manager.executor_acquired is False
        assert runtime.history.register_watch("alice", 300).spec is not None
        runtime.coordinator.request_reconcile()

        for _ in range(20):
            if runtime.manager.executor_acquired:
                break
            await asyncio.sleep(0.01)
        assert runtime.manager.executor_acquired is True
        assert [spec.user for spec in runtime.manager.list()] == ["alice"]

    assert events == ["cdn", "backend"]


async def test_partial_construction_failure_closes_backend(tmp_path: Path) -> None:
    config = _config(tmp_path)
    events: list[str] = []
    backend = ClosingBackend(events)

    def broken_client(_: Config) -> httpx.AsyncClient:
        raise RuntimeError("cdn init failed")

    with pytest.raises(RuntimeError, match="cdn init failed"):
        async with open_runtime(
            config,
            role="oneshot",
            backend_factory=lambda _: backend,
            cdn_client_factory=broken_client,
        ):
            pytest.fail("runtime yielded after construction failure")

    assert events == ["backend"]
    reopened = HistoryStore(config.db_path)
    reopened.close()
