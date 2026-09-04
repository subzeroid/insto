"""Secure POSIX advisory lock for the watcher executor of one SQLite store."""

from __future__ import annotations

import errno
import fcntl
import os
import stat
from pathlib import Path


class WatchLockError(Exception):
    """Base error for an unusable watcher lock path."""


class WatchLockBusyError(WatchLockError):
    """Raised when another process already executes watches for the store."""


class WatchProcessLock:
    """Own one stable lock inode derived from a canonical database path."""

    def __init__(self, db_path: Path) -> None:
        canonical_db = db_path.expanduser().resolve(strict=False)
        self._db_path = canonical_db
        self._path = Path(f"{canonical_db}.watch.lock")
        self._fd: int | None = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def acquired(self) -> bool:
        return self._fd is not None

    def acquire(self) -> None:
        if self._fd is not None:
            return

        self._path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self._path, flags, 0o600)
        except OSError as exc:
            raise WatchLockError(f"unsafe or unusable watch lock path: {self._path}") from exc

        try:
            lock_stat = os.fstat(fd)
            if not stat.S_ISREG(lock_stat.st_mode):
                raise WatchLockError(f"watch lock path is not a regular file: {self._path}")
            if lock_stat.st_uid != os.getuid():
                raise WatchLockError(f"watch lock path is not owned by current user: {self._path}")
            os.fchmod(fd, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise WatchLockBusyError(
                    f"watch executor already active for {self._db_path}"
                ) from exc
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    raise WatchLockBusyError(
                        f"watch executor already active for {self._db_path}"
                    ) from exc
                raise
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode())
            self._fd = fd
        except BaseException:
            os.close(fd)
            raise

    def release(self) -> None:
        fd = self._fd
        if fd is None:
            return
        self._fd = None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


__all__ = ["WatchLockBusyError", "WatchLockError", "WatchProcessLock"]
