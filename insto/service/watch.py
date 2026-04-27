"""`WatchManager` — async scheduler for `/watch` commands.

Each registered watch owns one `asyncio.Task` running a periodic loop. The
loop sleeps `interval_seconds`, then invokes a per-watch `tick` callable.
A tick has its own retry-once-then-fail policy:

- on first exception, the tick is retried once (unless the exception is a
  hard non-retriable `Banned` / `AuthInvalid`, which pauses immediately);
- two consecutive failed ticks (or one hard error) flip the watch status
  to `paused` and stop further ticks.

`WatchManager` is owned by the facade and lives for the REPL session only —
watches are not persisted across restarts. `cancel_all()` is awaited from
the REPL shutdown path; it cancels every loop task AND any in-flight tick
task, then joins them via `gather(..., return_exceptions=True)` so the
event loop can close cleanly without leaving detached coroutines that
would write to a closed history store.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from insto.exceptions import AuthInvalid, Banned
from insto.models import WatchSpec, WatchStatus

TickFn = Callable[[], Awaitable[Any]]


class WatchError(Exception):
    """Raised by `WatchManager.add` when a watch cannot be registered."""


@dataclass
class _Entry:
    user: str
    interval_seconds: int
    tick: TickFn
    status: WatchStatus = "active"
    last_ok: int | None = None
    last_error: str | None = None
    consecutive_errors: int = 0
    task: asyncio.Task[None] | None = field(default=None, repr=False)
    invoke_task: asyncio.Task[None] | None = field(default=None, repr=False)

    def to_spec(self) -> WatchSpec:
        return WatchSpec(
            user=self.user,
            interval_seconds=self.interval_seconds,
            last_ok=self.last_ok,
            last_error=self.last_error,
            status=self.status,
        )


class WatchManager:
    """Per-session async scheduler for `/watch` ticks.

    The manager is intentionally thin: it owns the dict of entries, the
    periodic loop task per entry, and the tick state machine. It never
    talks to the backend / history directly — the caller passes in a
    `tick` callable that does whatever the command-layer wants on each
    tick (snapshot + diff, notification, etc).
    """

    MAX_WATCHES = 3

    def __init__(self, max_watches: int | None = None) -> None:
        self._entries: dict[str, _Entry] = {}
        self._max = max_watches if max_watches is not None else self.MAX_WATCHES

    @property
    def max_watches(self) -> int:
        return self._max

    def add(
        self,
        user: str,
        interval_seconds: int,
        *,
        tick: TickFn,
        start: bool = True,
    ) -> WatchSpec:
        """Register a new watch and (optionally) start its periodic loop.

        Raises `WatchError` if `user` is already watched or the manager is
        full. The interval is taken at face value; the `/watch` command
        enforces the 5-minute floor before calling this.
        """
        if user in self._entries:
            raise WatchError(f"already watching @{user}")
        if len(self._entries) >= self._max:
            raise WatchError(
                f"too many active watches (max {self._max}); drop one with /unwatch first"
            )
        entry = _Entry(user=user, interval_seconds=interval_seconds, tick=tick)
        self._entries[user] = entry
        if start:
            entry.task = asyncio.create_task(self._loop(entry), name=f"insto-watch:{user}")
        return entry.to_spec()

    def remove(self, user: str) -> bool:
        """Cancel and forget the watch for `user`. Returns False if absent."""
        entry = self._entries.pop(user, None)
        if entry is None:
            return False
        # Cancel both the loop task AND any in-flight tick — without the
        # latter, a `_invoke` coroutine started before remove() can keep
        # running detached and write to history after the entry is gone.
        for t in (entry.task, entry.invoke_task):
            if t is not None and not t.done():
                t.cancel()
        return True

    def list(self) -> list[WatchSpec]:
        """Return current watch specs, ordered by user."""
        return [self._entries[u].to_spec() for u in sorted(self._entries)]

    def get(self, user: str) -> WatchSpec | None:
        entry = self._entries.get(user)
        return entry.to_spec() if entry is not None else None

    def __contains__(self, user: object) -> bool:
        return user in self._entries

    def __len__(self) -> int:
        return len(self._entries)

    async def cancel_all(self) -> None:
        """Cancel every watch loop and any in-flight tick, then await them."""
        tasks: list[asyncio.Task[None]] = []
        for entry in self._entries.values():
            for t in (entry.task, entry.invoke_task):
                if t is not None and not t.done():
                    t.cancel()
                    tasks.append(t)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._entries.clear()

    async def tick_once(self, user: str) -> WatchSpec:
        """Run one tick for `user` synchronously; updates state. Public for tests."""
        entry = self._entries[user]
        await self._do_tick(entry)
        return entry.to_spec()

    # ------------------------------------------------------------------ internal

    async def _loop(self, entry: _Entry) -> None:
        try:
            while True:
                await asyncio.sleep(entry.interval_seconds)
                await self._do_tick(entry)
                if entry.status != "active":
                    return
        except asyncio.CancelledError:
            raise

    async def _do_tick(self, entry: _Entry) -> None:
        try:
            await self._run_tick(entry)
        except asyncio.CancelledError:
            raise
        except (Banned, AuthInvalid) as exc:
            entry.status = "paused"
            entry.last_error = str(exc)
            return
        except Exception as exc:
            first_error = exc
        else:
            entry.last_ok = _now_ts()
            entry.last_error = None
            entry.consecutive_errors = 0
            return

        # single retry on tick
        try:
            await self._run_tick(entry)
        except asyncio.CancelledError:
            raise
        except (Banned, AuthInvalid) as exc:
            entry.status = "paused"
            entry.last_error = str(exc)
            return
        except Exception as exc:
            entry.consecutive_errors += 1
            entry.last_error = str(exc)
            if entry.consecutive_errors >= 2:
                entry.status = "paused"
            return
        else:
            _ = first_error  # consumed; retry succeeded
            entry.last_ok = _now_ts()
            entry.last_error = None
            entry.consecutive_errors = 0

    async def _run_tick(self, entry: _Entry) -> None:
        # Run the tick in a tracked child task so `cancel_all()` can drain
        # it explicitly. Without tracking, a cancellation of the loop task
        # would leave the inner coroutine running detached and racing with
        # facade / history teardown.
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


__all__ = ["WatchError", "WatchManager"]
