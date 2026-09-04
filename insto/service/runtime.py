"""Shared resource lifecycle for one-shot, REPL, and watcher-daemon modes."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal

import httpx

from insto._redact import redact_secrets
from insto.backends._base import OSINTBackend
from insto.backends._cdn import DEFAULT_TIMEOUT as CDN_TIMEOUT
from insto.config import Config
from insto.service.facade import OsintFacade
from insto.service.history import HistoryStore
from insto.service.watch import TickFn, WatchManager, format_watch_diff
from insto.service.watch_daemon import WatchDaemon
from insto.service.watch_lock import WatchProcessLock

RuntimeRole = Literal["oneshot", "repl", "daemon"]
BackendFactory = Callable[[Config], OSINTBackend]
CdnClientFactory = Callable[[Config], httpx.AsyncClient]
WatchOutput = Callable[[str], None]


@dataclass(slots=True)
class Runtime:
    config: Config
    role: RuntimeRole
    history: HistoryStore
    facade: OsintFacade
    manager: WatchManager
    coordinator: WatchDaemon | None
    coordinator_task: asyncio.Task[None] | None = None


def _default_cdn_client(config: Config) -> httpx.AsyncClient:
    if config.hiker_proxy:
        return httpx.AsyncClient(
            follow_redirects=False,
            timeout=CDN_TIMEOUT,
            proxy=config.hiker_proxy,
        )
    return httpx.AsyncClient(follow_redirects=False, timeout=CDN_TIMEOUT)


@asynccontextmanager
async def open_runtime(
    config: Config,
    *,
    role: RuntimeRole,
    backend_factory: BackendFactory,
    cdn_client_factory: CdnClientFactory | None = None,
    watch_output: WatchOutput | None = None,
) -> AsyncIterator[Runtime]:
    """Construct and tear down every shared resource exactly once."""
    history = HistoryStore(config.db_path)
    backend: OSINTBackend | None = None
    cdn_client: httpx.AsyncClient | None = None
    facade: OsintFacade | None = None
    manager: WatchManager | None = None
    runtime: Runtime | None = None
    try:
        backend = backend_factory(config)
        factory = cdn_client_factory if cdn_client_factory is not None else _default_cdn_client
        cdn_client = factory(config)
        manager = WatchManager(
            WatchProcessLock(history.path),
            release_when_empty=role == "repl",
        )
        facade = OsintFacade(
            backend=backend,
            history=history,
            config=config,
            cdn_client=cdn_client,
            watches=manager,
            watch_role=role,
        )

        coordinator: WatchDaemon | None = None
        if role != "oneshot":

            async def tick(user: str) -> None:
                diff = await facade.diff_and_snapshot(user)
                if watch_output is not None:
                    # Rendering is observational: a closed pipe or unavailable
                    # REPL output surface must not turn a successful backend
                    # tick into a retry or paused watch.
                    with contextlib.suppress(Exception):
                        watch_output(format_watch_diff(user, diff))

            def build_tick(user: str) -> TickFn:
                async def run_tick() -> None:
                    await tick(user)

                return run_tick

            coordinator = WatchDaemon(
                history=history,
                manager=manager,
                tick_factory=build_tick,
                role=role,
                state_output=watch_output,
            )
            facade.watch_daemon = coordinator

        runtime = Runtime(config, role, history, facade, manager, coordinator)
        if coordinator is not None and role == "daemon":
            await coordinator.start()
        elif coordinator is not None:
            runtime.coordinator_task = asyncio.create_task(
                _run_repl_coordinator(coordinator, watch_output),
                name="insto-repl-watch-coordinator",
            )
        yield runtime
    finally:
        coordinator_stop_failed = False
        if runtime is not None and runtime.coordinator is not None:
            try:
                await runtime.coordinator.stop()
            except Exception:
                coordinator_stop_failed = True
            if runtime.coordinator_task is not None:
                if coordinator_stop_failed and not runtime.coordinator_task.done():
                    runtime.coordinator_task.cancel()
                await asyncio.gather(runtime.coordinator_task, return_exceptions=True)
        if manager is not None:
            with contextlib.suppress(Exception):
                await manager.cancel_all()
        if cdn_client is not None:
            with contextlib.suppress(Exception):
                await cdn_client.aclose()
        if backend is not None:
            with contextlib.suppress(Exception):
                await backend.aclose()
        with contextlib.suppress(Exception):
            history.close()
        if manager is not None and manager.executor_acquired:
            with contextlib.suppress(Exception):
                manager.release_executor()


async def _run_repl_coordinator(coordinator: WatchDaemon, watch_output: WatchOutput | None) -> None:
    try:
        await coordinator.run(asyncio.Event())
    except Exception as exc:
        if watch_output is not None:
            watch_output(redact_secrets(f"watch executor stopped: {exc}"))


__all__ = ["Runtime", "RuntimeRole", "open_runtime"]
