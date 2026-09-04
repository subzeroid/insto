"""Tests for the per-database watcher executor lock."""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
from pathlib import Path

import pytest

from insto.service.watch_lock import (
    WatchLockBusyError,
    WatchLockError,
    WatchProcessLock,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX advisory lock")


def test_lock_excludes_second_descriptor_and_can_be_reacquired(tmp_path: Path) -> None:
    db = tmp_path / "store.db"
    first = WatchProcessLock(db)
    second = WatchProcessLock(db.parent / "." / db.name)

    first.acquire()
    assert first.acquired is True
    with pytest.raises(WatchLockBusyError, match="already active"):
        second.acquire()

    first.release()
    first.release()
    second.acquire()
    assert second.acquired is True
    second.release()


def test_lock_uses_canonical_database_identity(tmp_path: Path) -> None:
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    db = real_dir / "store.db"
    db.touch()
    alias_dir = tmp_path / "alias"
    alias_dir.symlink_to(real_dir, target_is_directory=True)

    direct = WatchProcessLock(db)
    alias = WatchProcessLock(alias_dir / "store.db")
    assert direct.path == alias.path

    direct.acquire()
    with pytest.raises(WatchLockBusyError):
        alias.acquire()
    direct.release()


def test_distinct_databases_do_not_contend(tmp_path: Path) -> None:
    first = WatchProcessLock(tmp_path / "one.db")
    second = WatchProcessLock(tmp_path / "two.db")
    first.acquire()
    second.acquire()
    assert first.acquired and second.acquired
    second.release()
    first.release()


def test_lock_file_is_private_regular_and_close_on_exec(tmp_path: Path) -> None:
    lock = WatchProcessLock(tmp_path / "store.db")
    lock.acquire()
    try:
        stat = lock.path.stat()
        assert stat.st_mode & 0o777 == 0o600
        assert lock.path.is_file()
        assert lock._fd is not None  # type: ignore[attr-defined]
        flags = fcntl.fcntl(lock._fd, fcntl.F_GETFD)  # type: ignore[attr-defined]
        assert flags & fcntl.FD_CLOEXEC
    finally:
        lock.release()
    assert lock.path.exists()


def test_lock_rejects_symlink_and_non_regular_path(tmp_path: Path) -> None:
    db = tmp_path / "store.db"
    lock_path = Path(f"{db.resolve(strict=False)}.watch.lock")
    target = tmp_path / "target"
    target.touch()
    lock_path.symlink_to(target)
    with pytest.raises(WatchLockError, match="lock path"):
        WatchProcessLock(db).acquire()

    lock_path.unlink()
    lock_path.mkdir()
    with pytest.raises(WatchLockError, match="lock path"):
        WatchProcessLock(db).acquire()


def test_lock_rejects_existing_file_owned_by_another_uid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "store.db"
    lock_path = Path(f"{db.resolve(strict=False)}.watch.lock")
    lock_path.touch(mode=0o600)
    actual_uid = lock_path.stat().st_uid
    monkeypatch.setattr(os, "getuid", lambda: actual_uid + 1)

    with pytest.raises(WatchLockError, match="not owned"):
        WatchProcessLock(db).acquire()


def test_process_exit_releases_lock_without_unlinking(tmp_path: Path) -> None:
    db = tmp_path / "store.db"
    code = (
        "from pathlib import Path; "
        "from insto.service.watch_lock import WatchProcessLock; "
        f"lock=WatchProcessLock(Path({str(db)!r})); "
        "lock.acquire(); print('READY', flush=True)"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdout, stderr = child.communicate(timeout=5)
    assert child.returncode == 0, stderr
    assert stdout.strip() == "READY"

    parent = WatchProcessLock(db)
    parent.acquire()
    parent.release()
    assert parent.path.exists()
