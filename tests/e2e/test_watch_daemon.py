"""POSIX subprocess coverage for the persistent foreground watcher."""

from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from insto.service.history import HistoryStore

PYTHON = sys.executable

pytestmark = pytest.mark.skipif(os.name != "posix", reason="daemon uses POSIX locks/signals")


def _run_insto(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [PYTHON, "-m", "insto", *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def _start_daemon(env: dict[str, str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [PYTHON, "-m", "insto", "watch-daemon"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _stop_daemon(process: subprocess.Popen[str]) -> tuple[str, str]:
    if process.poll() is None:
        process.send_signal(signal.SIGTERM)
    try:
        return process.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        return process.communicate(timeout=5)


def _wait_until(predicate: Callable[[], bool], *, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _watch_last_ok(db_path: Path) -> int | None:
    with sqlite3.connect(db_path) as connection:
        row = connection.execute("SELECT last_ok FROM watches WHERE user = 'alice'").fetchone()
    return None if row is None else row[0]


def _snapshot_count(db_path: Path) -> int:
    with sqlite3.connect(db_path) as connection:
        return int(connection.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0])


def _make_watch_due(db_path: Path) -> None:
    store = HistoryStore(db_path)
    try:
        spec = store.get_watch("alice")
        assert spec is not None
        assert store.update_watch_state(spec, last_ok=0)
    finally:
        store.close()


def test_daemon_ticks_rejects_second_owner_and_restarts(
    insto_env: dict[str, str],
) -> None:
    db_path = Path(insto_env["INSTO_DB_PATH"])
    registration = _run_insto(["@alice", "-c", "watch"], insto_env)
    assert registration.returncode == 0, registration.stderr
    assert "insto watch-daemon" in registration.stdout
    _make_watch_due(db_path)

    first = _start_daemon(insto_env)
    try:
        assert _wait_until(lambda: (_watch_last_ok(db_path) or 0) > 0), (
            _stop_daemon(first),
            "first daemon did not persist a successful tick",
        )
        assert _snapshot_count(db_path) >= 1

        contender = _run_insto(["watch-daemon"], insto_env)
        assert contender.returncode != 0
        assert "watch executor already active" in contender.stderr

        time.sleep(0.1)
        first.send_signal(signal.SIGTERM)
        stdout, stderr = first.communicate(timeout=10)
        assert first.returncode == 0, f"stdout={stdout!r}; stderr={stderr!r}"
        settled_count = _snapshot_count(db_path)
        time.sleep(0.1)
        assert _snapshot_count(db_path) == settled_count
    finally:
        if first.poll() is None:
            _stop_daemon(first)

    _make_watch_due(db_path)
    second = _start_daemon(insto_env)
    try:
        assert _wait_until(lambda: _snapshot_count(db_path) > settled_count), (
            _stop_daemon(second),
            "restarted daemon did not perform another tick",
        )
        time.sleep(0.1)
        second.send_signal(signal.SIGTERM)
        stdout, stderr = second.communicate(timeout=10)
        assert second.returncode == 0, f"stdout={stdout!r}; stderr={stderr!r}"
    finally:
        if second.poll() is None:
            _stop_daemon(second)
