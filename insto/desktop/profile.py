"""Owned desktop profile storage; reads do not initialize or repair anything."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import stat
import tempfile
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from insto.desktop.errors import DesktopError
from insto.desktop.protocol import _unique_object

_LIMIT = 65536
_OWNER = "insto-gui"
_STATE_KEYS = {
    "schema_version",
    "managed_by",
    "uid",
    "profile",
    "desired_service",
    "revision",
    "quota_remaining",
    "quota_checked_at",
}
_JOURNAL_KEYS = {
    "schema_version",
    "managed_by",
    "uid",
    "profile",
    "kind",
    "phase",
    "previous_state",
    "previous_running",
    "backup",
    "new_remaining",
}


def _directory(path: Path, *, private: bool = True) -> None:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o7000:
        raise DesktopError("profile_ownership")
    if private and stat.S_IMODE(info.st_mode) != 0o700:
        raise DesktopError("profile_ownership")


def _file(info: os.stat_result) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
        or info.st_size > _LIMIT
    ):
        raise DesktopError("profile_ownership")


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class Profile:
    def __init__(self, root: Path) -> None:
        if not root.is_absolute() or root.resolve() != root:
            raise DesktopError("profile_ownership")
        self.root = root
        self.home = root / "profile"
        self.state = root / "desktop-state.json"
        self.lock_path = root / ".desktop.lock"
        self.config = self.home / "config.toml"
        self.recovery = self.home / "desktop-recovery.json"
        self.backup = self.home / "config.previous.toml"
        self._leased = False

    @classmethod
    def from_environment(cls) -> Profile:
        configured = os.environ.get("INSTO_DESKTOP_ROOT")
        root = (
            Path(configured)
            if configured is not None
            else (Path.home() / "Library/Application Support/insto-gui")
        )
        return cls(root)

    def _existing_root(self) -> bool:
        if self.root.resolve() != self.root:
            raise DesktopError("profile_ownership")
        for ancestor in self.root.parents:
            try:
                info = ancestor.lstat()
            except FileNotFoundError:
                continue
            trusted_sticky = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid not in {0, os.getuid()}
                or info.st_mode & 0o6000
                or (info.st_mode & 0o022 and not trusted_sticky)
            ):
                raise DesktopError("profile_ownership")
        if not os.path.lexists(self.root):
            return False
        _directory(self.root)
        if os.path.lexists(self.home):
            _directory(self.home)
        return True

    def _read(self, path: Path) -> bytes | None:
        try:
            if not self._existing_root() or not os.path.lexists(path):
                return None
            descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                info = os.fstat(descriptor)
                _file(info)
                if info.st_size > _LIMIT:
                    raise DesktopError("profile_ownership")
                payload = os.read(descriptor, _LIMIT + 1)
                if len(payload) > _LIMIT:
                    raise DesktopError("profile_ownership")
                return payload
            finally:
                os.close(descriptor)
        except OSError:
            raise DesktopError("profile_ownership") from None

    def _binding(self, value: dict[str, Any]) -> None:
        if (
            type(value.get("schema_version")) is not int
            or value["schema_version"] != 1
            or value.get("managed_by") != _OWNER
            or type(value.get("uid")) is not int
            or value["uid"] != os.getuid()
            or value.get("profile") != str(self.home)
        ):
            raise DesktopError("profile_ownership")

    def _validate_state(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict) or value.keys() != _STATE_KEYS:
            raise DesktopError("profile_ownership")
        self._binding(value)
        if (
            value["desired_service"] not in ("running", "stopped")
            or not isinstance(value["revision"], str)
            or re.fullmatch(r"[0-9a-f]{32}", value["revision"]) is None
            or type(value["quota_remaining"]) is not int
            or value["quota_remaining"] < 0
            or type(value["quota_checked_at"]) is not int
            or value["quota_checked_at"] < 0
        ):
            raise DesktopError("profile_ownership")
        return value

    def _validate_journal(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict) or value.keys() != _JOURNAL_KEYS:
            raise DesktopError("recovery_required")
        self._binding(value)
        if (
            value["kind"] not in ("setup", "replace")
            or value["phase"]
            not in ("prepared", "stopped", "written", "rollback", "rolled_back", "committed")
            or type(value["previous_running"]) is not bool
            or type(value["new_remaining"]) is not int
            or value["new_remaining"] < 0
        ):
            raise DesktopError("recovery_required")
        if value["kind"] == "replace":
            self._validate_state(value["previous_state"])
            if value["backup"] != self.backup.name:
                raise DesktopError("recovery_required")
        elif (
            value["previous_state"] is not None
            or value["backup"] is not None
            or value["previous_running"]
        ):
            raise DesktopError("recovery_required")
        return value

    def _json(self, path: Path) -> Any:
        payload = self._read(path)
        if payload is None:
            return None
        try:
            value = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
            if not isinstance(value, dict):
                raise DesktopError("profile_ownership")
            return value
        except (ValueError, RecursionError):
            raise DesktopError("profile_ownership") from None

    def read_state(self) -> dict[str, Any] | None:
        value = self._json(self.state)
        return self._validate_state(value) if value is not None else None

    def read_journal(self) -> dict[str, Any] | None:
        value = self._json(self.recovery)
        return self._validate_journal(value) if value is not None else None

    def read_config(self) -> bytes | None:
        return self._read(self.config)

    def read_backup(self) -> bytes | None:
        return self._read(self.backup)

    def new_state(self, *, remaining: int, desired: str) -> dict[str, Any]:
        return self._validate_state(
            {
                "schema_version": 1,
                "managed_by": _OWNER,
                "uid": os.getuid(),
                "profile": str(self.home),
                "desired_service": desired,
                "revision": uuid.uuid4().hex,
                "quota_remaining": remaining,
                "quota_checked_at": int(time.time()),
            }
        )

    def new_journal(
        self,
        *,
        kind: str,
        previous_state: dict[str, Any] | None,
        previous_running: bool,
        remaining: int,
    ) -> dict[str, Any]:
        return self._validate_journal(
            {
                "schema_version": 1,
                "managed_by": _OWNER,
                "uid": os.getuid(),
                "profile": str(self.home),
                "kind": kind,
                "phase": "prepared",
                "previous_state": previous_state,
                "previous_running": previous_running,
                "backup": self.backup.name if kind == "replace" else None,
                "new_remaining": remaining,
            }
        )

    def _write(self, path: Path, payload: bytes, *, create: bool = False) -> None:
        if not self._leased or len(payload) > _LIMIT:
            raise DesktopError("storage_error")
        self._existing_root()
        _directory(path.parent)
        if os.path.lexists(path):
            _file(path.lstat())
            if create:
                raise DesktopError("storage_error")
        # Stage outside the profile: a process death before the first journal
        # publication must not make our own orphan look like a foreign profile.
        # The application root is equally private; stale stages are never read
        # as committed config or used as recovery authority.
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=self.root)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            # All publishers hold the profile flock. A single rename avoids
            # stranding a two-link backup if killed between link and unlink.
            os.replace(temporary, path)
            _sync_directory(path.parent)
            if path.parent != self.root:
                _sync_directory(self.root)
        except OSError:
            raise DesktopError("storage_error") from None
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)

    def _write_json(self, path: Path, value: dict[str, Any]) -> None:
        self._write(path, (json.dumps(value, sort_keys=True, allow_nan=False) + "\n").encode())

    def write_state(self, value: dict[str, Any]) -> None:
        self._write_json(self.state, self._validate_state(value))

    def write_config(self, payload: bytes) -> None:
        self._write(self.config, payload)

    def write_journal(self, value: dict[str, Any]) -> None:
        self._write_json(self.recovery, self._validate_journal(value))

    def write_backup(self, payload: bytes) -> None:
        self._write(self.backup, payload, create=True)

    def _remove(self, path: Path) -> None:
        if not self._leased:
            raise DesktopError("storage_error")
        if self._read(path) is not None:
            path.unlink()
            _sync_directory(path.parent)

    def remove_journal(self) -> None:
        self._remove(self.recovery)

    def remove_backup(self) -> None:
        self._remove(self.backup)

    def remove_state(self) -> None:
        self._remove(self.state)

    @contextlib.contextmanager
    def locked(self, *, initialize: bool = False) -> Iterator[None]:
        descriptor: int | None = None
        acquired = False
        try:
            if not self._existing_root():
                if not initialize:
                    raise DesktopError("not_configured")
                _directory(self.root.parent, private=False)
                try:
                    self.root.mkdir(mode=0o700)
                    _sync_directory(self.root.parent)
                except FileExistsError:
                    _directory(self.root)
            descriptor = os.open(
                self.lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
            )
            info = os.fstat(descriptor)
            _file(info)
            current = self.lock_path.lstat()
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                raise DesktopError("profile_ownership")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise DesktopError("profile_busy") from None
            acquired = True
            if not self.home.exists():
                self.home.mkdir(mode=0o700)
                _sync_directory(self.root)
            _directory(self.home)
            state = self.read_state()
            if state is None and any(self.home.iterdir()):
                journal = self.read_journal()
                if journal is None or journal["kind"] != "setup":
                    raise DesktopError("profile_ownership")
            if state is None and not initialize and self.read_journal() is None:
                raise DesktopError("not_configured")
            self._leased = True
            yield
        except OSError:
            raise DesktopError("storage_error") from None
        finally:
            if acquired:
                self._leased = False
                assert descriptor is not None
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            if descriptor is not None:
                os.close(descriptor)
