"""UTC attempt reservations are shared by independent clients and processes."""

import asyncio
import fcntl
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import UTC, datetime

import pytest


async def test_inspection_of_absent_ledger_creates_nothing(tmp_path):
    from insto.service.usage import UsageLedger

    ledger = UsageLedger(tmp_path / "missing" / "usage.db", clock=lambda: 86401)
    assert await ledger.inspect() == {
        "kind": "usage",
        "day": "1970-01-02",
        "used": 0,
        "daily_limit": None,
        "reset_at": 172800,
    }
    assert list(tmp_path.iterdir()) == []


async def test_cap_changes_preserve_attempts_and_are_seen_by_existing_clients(tmp_path):
    from insto.exceptions import DailyBudgetReached
    from insto.service.usage import UsageLedger

    first = UsageLedger(tmp_path / "usage.db", clock=lambda: 86401)
    second = UsageLedger(first.path, clock=lambda: 86401)
    assert (await first.reserve())["used"] == 1
    assert (await second.configure(1))["used"] == 1
    with pytest.raises(DailyBudgetReached):
        await first.reserve()
    assert (await first.inspect())["used"] == 1
    await second.configure(2)
    assert (await first.reserve())["used"] == 2
    await second.configure(None)
    assert (await first.reserve())["used"] == 3


async def test_new_utc_day_resets_usage_and_retains_cap(tmp_path):
    from insto.service.usage import UsageLedger

    moment = int(datetime(2026, 10, 13, 23, 59, 59, tzinfo=UTC).timestamp())
    ledger = UsageLedger(tmp_path / "usage.db", clock=lambda: moment)
    await ledger.configure(1)
    await ledger.reserve()
    moment += 1
    assert (await ledger.inspect())["used"] == 0
    assert (await ledger.inspect())["day"] == "2026-10-14"
    state = await ledger.reserve()
    assert state["used"] == state["daily_limit"] == 1


async def test_cross_process_reservations_never_exceed_ceiling(tmp_path):
    from insto.service.usage import UsageLedger

    ledger = UsageLedger(tmp_path / "usage.db")
    await ledger.configure(13)
    script = """
import asyncio, sys
from insto.service.usage import UsageLedger
from insto.exceptions import DailyBudgetReached
async def run():
    ledger = UsageLedger(sys.argv[1])
    reserved = 0
    for _ in range(10):
        try:
            await ledger.reserve()
            reserved += 1
        except DailyBudgetReached:
            pass
    print(reserved)
asyncio.run(run())
"""
    children = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(ledger.path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=os.environ.copy(),
        )
        for _ in range(4)
    ]
    results = await asyncio.gather(
        *[asyncio.to_thread(child.communicate, timeout=20) for child in children]
    )
    assert all(child.returncode == 0 for child in children), results
    assert all(err == b"" for _, err in results)
    assert sum(int(out) for out, _ in results) == 13
    assert (await ledger.inspect())["used"] == 13


async def test_concurrent_first_reservations_do_not_reset_or_fail(tmp_path):
    from insto.service.usage import UsageLedger

    ledger = UsageLedger(tmp_path / "usage.db")
    await asyncio.gather(*[UsageLedger(ledger.path).reserve() for _ in range(30)])
    assert (await ledger.inspect())["used"] == 30


async def test_initialization_lock_contention_is_bounded(tmp_path):
    from insto.exceptions import UsageStorageError
    from insto.service.usage import UsageLedger

    ledger = UsageLedger(tmp_path / "usage.db")
    lock = os.open(str(ledger.path) + ".init.lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        with pytest.raises(UsageStorageError):
            await asyncio.wait_for(ledger.reserve(), timeout=2.0)
    finally:
        os.close(lock)


@pytest.mark.parametrize("link", ["symlink", "hardlink"])
async def test_linked_ledger_is_refused_without_modifying_other_file(tmp_path, link):
    from insto.exceptions import UsageStorageError
    from insto.service.usage import UsageLedger

    ledger = UsageLedger(tmp_path / "usage.db")
    original = tmp_path / "original.db"
    original.write_bytes(b"private data")
    original.chmod(0o600)
    if link == "symlink":
        ledger.path.symlink_to(original)
    else:
        os.link(original, ledger.path)
    with pytest.raises(UsageStorageError):
        await ledger.reserve()
    assert original.read_bytes() == b"private data"


@pytest.mark.parametrize("damage", ["bytes", "missing-row", "invalid-value"])
async def test_corrupt_capped_ledger_fails_closed(tmp_path, damage):
    from insto.exceptions import UsageStorageError
    from insto.service.usage import UsageLedger

    ledger = UsageLedger(tmp_path / "usage.db")
    await ledger.configure(10)
    if damage == "bytes":
        ledger.path.write_bytes(b"not a database")
    else:
        with closing(sqlite3.connect(ledger.path)) as connection, connection:
            if damage == "missing-row":
                connection.execute("DELETE FROM usage")
            else:
                connection.execute("PRAGMA ignore_check_constraints = ON")
                connection.execute("UPDATE usage SET used = -1")
    with pytest.raises(UsageStorageError):
        await ledger.reserve()


@pytest.mark.parametrize("limit", [True, False, 0, -1, 1_000_001, 1.0, "10"])
async def test_configuration_rejects_invalid_limits_without_creating_a_ledger(tmp_path, limit):
    from insto.service.usage import UsageLedger

    ledger = UsageLedger(tmp_path / "usage.db")
    with pytest.raises(ValueError):
        await ledger.configure(limit)
    assert not ledger.path.exists()
