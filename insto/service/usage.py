"""Local UTC-day HTTP attempt accounting, shared across processes.

Reservations count attempts, including retries and failed transports. They are
conservative and do not claim to equal the provider's invoiced quota. The free
balance endpoint is excluded at the backend's HTTP boundary.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import sqlite3
import stat
import tempfile
import time
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from insto.exceptions import DailyBudgetReached, UsageStorageError

MAX_DAILY_LIMIT = 1_000_000
_SCHEMA = """CREATE TABLE usage (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    day TEXT NOT NULL,
    used INTEGER NOT NULL CHECK (used >= 0),
    daily_limit INTEGER CHECK (daily_limit BETWEEN 1 AND 1000000)
)"""


def usage_path(database: Path) -> Path:
    """A separate ledger for this configured history database; no migration."""
    return database.with_name(database.name + ".usage.db")


def validate_limit(limit: Any) -> None:
    if limit is not None and (type(limit) is not int or not 1 <= limit <= MAX_DAILY_LIMIT):
        raise ValueError("invalid daily request-attempt limit")


class UsageLedger:
    """Each reservation reads the current cap under an atomic SQLite write lock.

    No cached configuration means a running daemon observes desktop changes on
    its next request. No connection is retained between calls or threads.
    """

    def __init__(self, path: str | Path, *, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self._clock = clock

    def _today(self) -> tuple[str, int]:
        today = datetime.fromtimestamp(self._clock(), tz=UTC).date()
        reset = datetime.combine(today + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
        return today.isoformat(), int(reset.timestamp())

    def _check_file(self) -> None:
        info = self.path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise UsageStorageError()

    def _ensure(self) -> None:
        if os.path.lexists(self.path):
            self._check_file()
            return
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock = os.open(
            self.path.with_name(self.path.name + ".init.lock"),
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        try:
            info = os.fstat(lock)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
            ):
                raise UsageStorageError()
            deadline = time.monotonic() + 1.0
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise UsageStorageError() from None
                    time.sleep(0.01)
            if not os.path.lexists(self.path):
                self._initialize()
        finally:
            os.close(lock)
        self._check_file()

    def _initialize(self) -> None:
        descriptor, name = tempfile.mkstemp(prefix=".usage-", suffix=".db", dir=self.path.parent)
        os.close(descriptor)
        temporary = Path(name)
        try:
            # Publish a complete initialized database atomically. A concurrent
            # process sees either no file or a valid ledger, never an empty DB.
            with closing(sqlite3.connect(temporary)) as connection, connection:
                connection.execute(_SCHEMA)
                connection.execute("INSERT INTO usage VALUES (1, ?, 0, NULL)", (self._today()[0],))
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)

    def _read(self, connection: sqlite3.Connection) -> dict[str, Any]:
        rows = connection.execute(
            "SELECT day, used, daily_limit FROM usage WHERE id = 1"
        ).fetchall()
        if len(rows) != 1 or connection.execute("SELECT COUNT(*) FROM usage").fetchone() != (1,):
            raise UsageStorageError()
        stored_day, used, limit = rows[0]
        if (
            not isinstance(stored_day, str)
            or date.fromisoformat(stored_day).isoformat() != stored_day
            or type(used) is not int
            or used < 0
        ):
            raise UsageStorageError()
        validate_limit(limit)
        day, reset_at = self._today()
        return {
            "kind": "usage",
            "day": day,
            "used": used if day == stored_day else 0,
            "daily_limit": limit,
            "reset_at": reset_at,
        }

    def _operate(self, operation: str, limit: int | None = None) -> dict[str, Any]:
        try:
            if operation == "inspect" and not os.path.lexists(self.path):
                day, reset_at = self._today()
                return {
                    "kind": "usage",
                    "day": day,
                    "used": 0,
                    "daily_limit": None,
                    "reset_at": reset_at,
                }
            if operation != "inspect":
                self._ensure()
            self._check_file()
            mode = "ro" if operation == "inspect" else "rw"
            with closing(
                sqlite3.connect(
                    self.path.absolute().as_uri() + "?mode=" + mode,
                    uri=True,
                    timeout=1.0,
                    isolation_level=None,
                )
            ) as connection:
                if operation != "inspect":
                    connection.execute("BEGIN IMMEDIATE")
                state = self._read(connection)
                if operation == "inspect":
                    return state
                if operation == "reserve":
                    if state["daily_limit"] is not None and state["used"] >= state["daily_limit"]:
                        raise DailyBudgetReached()
                    state["used"] += 1
                else:
                    state["daily_limit"] = limit
                connection.execute(
                    "UPDATE usage SET day = ?, used = ?, daily_limit = ? WHERE id = 1",
                    (state["day"], state["used"], state["daily_limit"]),
                )
                connection.commit()
                return state
        except (OSError, sqlite3.Error, ValueError, OverflowError):
            # Existing corruption never turns into a fresh unlimited ledger.
            raise UsageStorageError() from None

    async def inspect(self) -> dict[str, Any]:
        """Read locally; an absent ledger or parent is never created."""
        return await asyncio.to_thread(self._operate, "inspect")

    async def configure(self, daily_limit: int | None) -> dict[str, Any]:
        validate_limit(daily_limit)
        return await asyncio.to_thread(self._operate, "configure", daily_limit)

    async def reserve(self) -> dict[str, Any]:
        """Commit one attempt before transport, refusing an exhausted cap."""
        return await asyncio.to_thread(self._operate, "reserve")


def install_usage_ledger(backend: object, database: Path) -> None:
    """Install accounting on backends that support it, leaving offline fakes free."""
    install = getattr(backend, "install_usage_ledger", None)
    if install is not None:
        install(UsageLedger(usage_path(database)))
