"""Reconcile persistent SQLite watch state into one local scheduler.

```text
SQLite rows -> reconcile -> local WatchManager tasks
     ^                              |
     +------ conditional state -----+
             user + registration id
```
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from insto._redact import redact_secrets
from insto.models import WatchSpec
from insto.service.history import HistoryStore
from insto.service.watch import TickFn, WatchManager
from insto.service.watch_lock import WatchLockBusyError

WatchExecutorRole = Literal["repl", "daemon"]


@dataclass(frozen=True, slots=True)
class WatchLoadEstimate:
    ticks_per_hour: float
    backend_calls_per_hour_low: float
    backend_calls_per_hour_high: float


def initial_watch_delay(spec: WatchSpec, *, now: float) -> float:
    if spec.last_ok is None:
        return float(spec.interval_seconds)
    return max(0.0, float(spec.last_ok + spec.interval_seconds) - now)


def startup_offsets(specs: list[WatchSpec], *, now: float) -> dict[str, float]:
    delays: dict[str, float] = {}
    overdue_index = 0
    for spec in sorted(specs, key=lambda item: item.user):
        delay = initial_watch_delay(spec, now=now)
        if delay == 0:
            delay = float(overdue_index * 2)
            overdue_index += 1
        delays[spec.user] = delay
    return delays


def estimate_watch_load(specs: list[WatchSpec]) -> WatchLoadEstimate:
    ticks = sum(3600.0 / spec.interval_seconds for spec in specs if spec.status == "active")
    return WatchLoadEstimate(
        ticks_per_hour=ticks,
        backend_calls_per_hour_low=ticks * 2,
        backend_calls_per_hour_high=ticks * 3,
    )


class WatchDaemon:
    """Shared foreground/REPL coordinator for persistent watches."""

    def __init__(
        self,
        *,
        history: HistoryStore,
        manager: WatchManager,
        tick_factory: Callable[[str], TickFn],
        role: WatchExecutorRole,
        reconcile_seconds: float = 2.0,
        now: Callable[[], float] | None = None,
        state_output: Callable[[str], None] | None = None,
    ) -> None:
        self._history = history
        self._manager = manager
        self._tick_factory = tick_factory
        self._role = role
        self._reconcile_seconds = reconcile_seconds
        self._now = now if now is not None else time.time
        self._state_output = state_output
        self._wake = asyncio.Event()
        self._internal_stop = asyncio.Event()
        self._started = False
        self._control_only = False

    @property
    def control_only(self) -> bool:
        return self._control_only

    async def start(self) -> int:
        if self._started:
            return len(self._manager)
        if self._role == "daemon":
            self._manager.acquire_executor()
        self._started = True
        try:
            await self.reconcile_once(recovering=True)
        except BaseException:
            await self._manager.cancel_all()
            if self._manager.executor_acquired:
                self._manager.release_executor()
            self._started = False
            raise
        return len(self._manager)

    def request_reconcile(self) -> None:
        self._wake.set()

    async def reconcile_once(self, *, recovering: bool = False) -> None:
        rows = await self._history.list_watches_async()
        persisted = {row.user: row for row in rows if row.status == "active"}

        if not self._manager.executor_acquired and persisted:
            try:
                self._manager.acquire_executor()
            except WatchLockBusyError:
                if self._role == "daemon":
                    raise
                self._control_only = True
                return
        self._control_only = False

        local = {row.user: row for row in self._manager.list()}
        replacements = {
            user
            for user in persisted.keys() & local.keys()
            if persisted[user].registration_id != local[user].registration_id
            or persisted[user].interval_seconds != local[user].interval_seconds
        }
        removals = (local.keys() - persisted.keys()) | replacements
        for user in sorted(removals):
            await self._manager.remove(user, release_when_empty=False)

        delays = startup_offsets(list(persisted.values()), now=self._now()) if recovering else {}
        for user in sorted((persisted.keys() - local.keys()) | replacements):
            spec = persisted[user]
            self._manager.add(
                spec,
                tick=self._tick_factory(user),
                state_changed=self._persist_state,
                initial_delay=delays.get(user, initial_watch_delay(spec, now=self._now())),
            )

        if self._role == "repl" and not persisted and not self._manager.list():
            self._manager.release_executor()

    async def run(self, stop_event: asyncio.Event) -> None:
        if not self._started:
            await self.start()
        reconcile_task = asyncio.create_task(self._reconcile_loop(), name="insto-watch-reconcile")
        stop_task = asyncio.create_task(stop_event.wait(), name="insto-watch-stop")
        internal_stop_task = asyncio.create_task(
            self._internal_stop.wait(), name="insto-watch-internal-stop"
        )
        try:
            waiters: set[asyncio.Future[Any]] = {
                reconcile_task,
                stop_task,
                internal_stop_task,
                self._manager.fatal_error,
            }
            done, _ = await asyncio.wait(
                waiters,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if self._manager.fatal_error in done:
                raise self._manager.fatal_error.result()
            if reconcile_task in done:
                await reconcile_task
        finally:
            for task in (reconcile_task, stop_task, internal_stop_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(
                reconcile_task,
                stop_task,
                internal_stop_task,
                return_exceptions=True,
            )
            await self._manager.cancel_all()
            self._started = False

    async def stop(self) -> None:
        self._internal_stop.set()
        await self._manager.cancel_all()
        self._started = False

    async def _reconcile_loop(self) -> None:
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self._reconcile_seconds)
            self._wake.clear()
            await self.reconcile_once()

    async def _persist_state(self, spec: WatchSpec) -> bool:
        safe_error = redact_secrets(spec.last_error) if spec.last_error is not None else None
        updated = await self._history.update_watch_state_async(
            spec,
            last_ok=spec.last_ok,
            last_error=safe_error,
            consecutive_errors=spec.consecutive_errors,
            status=spec.status,
        )
        if updated and safe_error is not None and self._state_output is not None:
            # State is already durable. Terminal/log delivery is best-effort
            # and must not stop the executor or rewrite the tick outcome.
            with contextlib.suppress(Exception):
                self._state_output(
                    f"@{spec.user}: watch error ({spec.consecutive_errors}/2) "
                    f"· {spec.status} · {safe_error}"
                )
        return updated


__all__ = [
    "WatchDaemon",
    "WatchExecutorRole",
    "WatchLoadEstimate",
    "estimate_watch_load",
    "initial_watch_delay",
    "startup_offsets",
]
