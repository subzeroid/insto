"""Per-process scheduler for persistent watch registrations.

SQLite owns desired state. This manager owns only the local executor lock and
one fixed-delay task per active registration. Each state transition is emitted
as an immutable snapshot and conditionally persisted by the coordinator.

```text
active, errors=0 -- failed tick --> active, errors=1
active, errors=1 -- failed tick --> paused, errors=2
active ---------- success ------> active, errors=0
active ---------- hard error ---> paused
```
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from insto.exceptions import AuthInvalid, Banned
from insto.models import WatchSpec, WatchStatus
from insto.service.watch_lock import WatchProcessLock

TickFn = Callable[[], Awaitable[Any]]
StateChangedFn = Callable[[WatchSpec], Awaitable[bool]]


def format_watch_diff(username: str, diff: dict[str, Any]) -> str:
    """Render one compact watcher result for terminal/log output."""
    if diff.get("first_seen"):
        return f"@{username}: first snapshot — no prior state to diff against"
    changes = diff.get("changes") or {}
    prior = diff.get("previous_usernames") or []
    if not changes and not prior:
        return f"@{username}: no changes"
    parts: list[str] = []
    for field_name in sorted(changes):
        delta = changes[field_name]
        parts.append(f"{field_name}: {delta.get('old')!r} -> {delta.get('new')!r}")
    if prior:
        parts.append(f"aliases: {', '.join(prior)}")
    return f"@{username} changed — " + "; ".join(parts)


class WatchError(Exception):
    """Raised when a local watch cannot be scheduled safely."""


@dataclass
class _Entry:
    user: str
    registration_id: str
    interval_seconds: int
    tick: TickFn
    state_changed: StateChangedFn
    initial_delay: float
    status: WatchStatus = "active"
    last_ok: int | None = None
    last_error: str | None = None
    consecutive_errors: int = 0
    task: asyncio.Task[None] | None = field(default=None, repr=False)
    invoke_task: asyncio.Task[None] | None = field(default=None, repr=False)
    drain_task: asyncio.Task[None] | None = field(default=None, repr=False)

    @classmethod
    def from_spec(
        cls,
        spec: WatchSpec,
        *,
        tick: TickFn,
        state_changed: StateChangedFn,
        initial_delay: float,
    ) -> _Entry:
        return cls(
            user=spec.user,
            registration_id=spec.registration_id,
            interval_seconds=spec.interval_seconds,
            tick=tick,
            state_changed=state_changed,
            initial_delay=initial_delay,
            status=spec.status,
            last_ok=spec.last_ok,
            last_error=spec.last_error,
            consecutive_errors=spec.consecutive_errors,
        )

    def to_spec(self) -> WatchSpec:
        return WatchSpec(
            user=self.user,
            registration_id=self.registration_id,
            interval_seconds=self.interval_seconds,
            last_ok=self.last_ok,
            last_error=self.last_error,
            consecutive_errors=self.consecutive_errors,
            status=self.status,
        )


class WatchManager:
    """Own the sole executor lock and local fixed-delay watch tasks."""

    MAX_WATCHES = 3

    def __init__(
        self,
        process_lock: WatchProcessLock,
        *,
        release_when_empty: bool,
        max_watches: int | None = None,
        now: Callable[[], int] | None = None,
    ) -> None:
        self._entries: dict[str, _Entry] = {}
        self._max = max_watches if max_watches is not None else self.MAX_WATCHES
        self._process_lock = process_lock
        self._release_when_empty = release_when_empty
        self._now = now if now is not None else _now_ts
        self._fatal_error: asyncio.Future[BaseException] | None = None

    @property
    def max_watches(self) -> int:
        return self._max

    @property
    def executor_acquired(self) -> bool:
        return self._process_lock.acquired

    @property
    def fatal_error(self) -> asyncio.Future[BaseException]:
        if self._fatal_error is None:
            self._fatal_error = asyncio.get_running_loop().create_future()
        return self._fatal_error

    def acquire_executor(self) -> None:
        self._process_lock.acquire()

    def release_executor(self) -> None:
        if self._entries:
            raise WatchError("cannot release watch executor while tasks remain")
        self._process_lock.release()

    def add(
        self,
        spec: WatchSpec,
        *,
        tick: TickFn,
        state_changed: StateChangedFn,
        initial_delay: float | None = None,
        start: bool = True,
    ) -> WatchSpec:
        """Schedule a complete persisted registration."""
        if not self.executor_acquired:
            raise WatchError("watch executor lock is not acquired")
        if spec.user in self._entries:
            raise WatchError(f"already watching @{spec.user}")
        if len(self._entries) >= self._max:
            raise WatchError(f"too many active watches (max {self._max})")
        if spec.status != "active":
            raise WatchError(f"cannot schedule paused watch @{spec.user}")

        delay = float(spec.interval_seconds if initial_delay is None else max(0.0, initial_delay))
        entry = _Entry.from_spec(
            spec,
            tick=tick,
            state_changed=state_changed,
            initial_delay=delay,
        )
        self._entries[spec.user] = entry
        if start:
            entry.task = asyncio.create_task(self._loop(entry), name=f"insto-watch:{spec.user}")
        return entry.to_spec()

    async def remove(self, user: str, *, release_when_empty: bool = True) -> bool:
        entry = self._entries.get(user)
        if entry is None:
            return False
        await self._cancel_entry(entry)
        if self._entries.get(user) is entry:
            del self._entries[user]
        if release_when_empty:
            self._release_if_empty()
        return True

    def list(self) -> list[WatchSpec]:
        return [self._entries[user].to_spec() for user in sorted(self._entries)]

    def get(self, user: str) -> WatchSpec | None:
        entry = self._entries.get(user)
        return entry.to_spec() if entry is not None else None

    def __contains__(self, user: object) -> bool:
        return user in self._entries

    def __len__(self) -> int:
        return len(self._entries)

    async def cancel_all(self) -> None:
        entries = list(self._entries.values())
        if entries:
            await asyncio.gather(
                *(self._cancel_entry(entry) for entry in entries),
            )
        for entry in entries:
            if self._entries.get(entry.user) is entry:
                del self._entries[entry.user]
        self._release_if_empty()

    async def tick_once(self, user: str) -> WatchSpec:
        entry = self._entries[user]
        await self._do_tick(entry)
        return entry.to_spec()

    async def _cancel_entry(self, entry: _Entry) -> None:
        # Concurrent remove/stop paths must join one drain, not re-cancel a
        # tick's cleanup or release the executor while another caller waits.
        # Keep the entry registered until the shared drain has finished.
        if entry.drain_task is None:
            entry.drain_task = asyncio.create_task(
                self._drain_entry(entry), name=f"insto-watch-drain:{entry.user}"
            )
        await asyncio.shield(entry.drain_task)

    async def _drain_entry(self, entry: _Entry) -> None:
        tasks: list[asyncio.Task[None]] = []
        for task in (entry.task, entry.invoke_task):
            if task is not None and not task.done():
                task.cancel()
                tasks.append(task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _release_if_empty(self) -> None:
        if self._release_when_empty and not self._entries:
            self._process_lock.release()

    async def _loop(self, entry: _Entry) -> None:
        delay = entry.initial_delay
        try:
            while entry.status == "active":
                await asyncio.sleep(delay)
                if not await self._do_tick(entry):
                    return
                delay = float(entry.interval_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._report_fatal(exc)

    async def _do_tick(self, entry: _Entry) -> bool:
        try:
            await self._run_tick(entry)
        except asyncio.CancelledError:
            raise
        except (Banned, AuthInvalid) as exc:
            entry.status = "paused"
            entry.last_error = str(exc)
            entry.consecutive_errors += 1
            return await self._emit_state(entry)
        except Exception:
            pass
        else:
            self._mark_success(entry)
            return await self._emit_state(entry)

        try:
            await self._run_tick(entry)
        except asyncio.CancelledError:
            raise
        except (Banned, AuthInvalid) as exc:
            entry.status = "paused"
            entry.last_error = str(exc)
            entry.consecutive_errors += 1
        except Exception as exc:
            entry.consecutive_errors += 1
            entry.last_error = str(exc)
            if entry.consecutive_errors >= 2:
                entry.status = "paused"
        else:
            self._mark_success(entry)
        return await self._emit_state(entry)

    def _mark_success(self, entry: _Entry) -> None:
        entry.last_ok = self._now()
        entry.last_error = None
        entry.consecutive_errors = 0

    async def _emit_state(self, entry: _Entry) -> bool:
        try:
            return await entry.state_changed(entry.to_spec())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._report_fatal(exc)
            return False

    def _report_fatal(self, exc: BaseException) -> None:
        future = self.fatal_error
        if not future.done():
            future.set_result(exc)

    async def _run_tick(self, entry: _Entry) -> None:
        invoke = asyncio.create_task(self._invoke(entry), name=f"insto-tick:{entry.user}")
        entry.invoke_task = invoke
        try:
            await invoke
        finally:
            entry.invoke_task = None

    async def _invoke(self, entry: _Entry) -> None:
        await entry.tick()


def _now_ts() -> int:
    return int(time.time())


__all__ = ["StateChangedFn", "TickFn", "WatchError", "WatchManager", "format_watch_diff"]
