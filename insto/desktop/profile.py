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
_BINDING_KEYS = {"schema_version", "managed_by", "uid", "home"}
RETAINED_REGISTRATION = "migration-registration.json"


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


def _valid_home(root: Path, home: Path) -> bool:
    """An adopted home is absolute, resolved, and disjoint from the desktop root."""
    return (
        home.is_absolute()
        and home.resolve() == home
        and home != root
        and root not in home.parents
        and home not in root.parents
    )


def _trusted_ancestors(path: Path) -> None:
    for ancestor in path.parents:
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


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class Profile:
    def __init__(self, root: Path, home: Path | None = None) -> None:
        if not root.is_absolute() or root.resolve() != root:
            raise DesktopError("profile_ownership")
        if home is not None and not _valid_home(root, home):
            raise DesktopError("home_invalid")
        self.root = root
        self.adopted = home is not None
        self.home = home if home is not None else root / "profile"
        self.binding = root / "desktop-home.json"
        # The desktop's own state sits beside its profile; an adopted home keeps
        # its desktop state inside itself so two bindings never share intent.
        self.state = (self.home if self.adopted else root) / "desktop-state.json"
        self.lock_path = root / ".desktop.lock"
        # An adopted home is also locked in place: two desktop roots (two
        # INSTO_DESKTOP_ROOTs) bound to the same home serialize on this file.
        self.home_lock_path = self.home / ".desktop.lock" if self.adopted else None
        self.config = self.home / "config.toml"
        self.recovery = self.home / "desktop-recovery.json"
        self.backup = self.home / "config.previous.toml"
        self._leased = False

    @classmethod
    def own_from_environment(cls) -> Profile:
        """The desktop's own profile, ignoring any adopted-home binding."""
        configured = os.environ.get("INSTO_DESKTOP_ROOT")
        root = (
            Path(configured)
            if configured is not None
            else (Path.home() / "Library/Application Support/insto-gui")
        )
        return cls(root)

    @classmethod
    def from_environment(cls) -> Profile:
        own = cls.own_from_environment()
        binding = own.read_binding()
        if binding is None:
            return own
        return cls(own.root, home=Path(binding["home"]))

    def _existing_root(self) -> bool:
        if self.root.resolve() != self.root:
            raise DesktopError("profile_ownership")
        _trusted_ancestors(self.root)
        if self.adopted:
            # An adopted home must already exist under the same ancestry rule as
            # the root, whether or not the root exists yet: nothing is created
            # inside a home that has not been checked.
            if not os.path.lexists(self.home):
                raise DesktopError("home_invalid")
            try:
                _trusted_ancestors(self.home)
                _directory(self.home)
            except DesktopError:
                raise DesktopError("home_invalid") from None
        if not os.path.lexists(self.root):
            return False
        _directory(self.root)
        if not self.adopted and os.path.lexists(self.home):
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
        ):
            raise DesktopError("profile_ownership")
        quota, checked = value["quota_remaining"], value["quota_checked_at"]
        if (quota is None) != (checked is None) or (
            quota is not None
            and (type(quota) is not int or quota < 0 or type(checked) is not int or checked < 0)
        ):
            raise DesktopError("profile_ownership")
        return value

    def _validate_journal(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict) or value.keys() != _JOURNAL_KEYS:
            raise DesktopError("recovery_required")
        self._binding(value)
        if (
            value["kind"] not in ("setup", "replace", "migrate")
            or value["phase"]
            not in (
                "prepared",
                "stopped",
                "written",
                "published",
                "started",
                "rollback",
                "rolled_back",
                "committed",
            )
            or type(value["previous_running"]) is not bool
            or (
                value["new_remaining"] is not None
                and (type(value["new_remaining"]) is not int or value["new_remaining"] < 0)
            )
            or (value["kind"] != "migrate" and value["new_remaining"] is None)
        ):
            raise DesktopError("recovery_required")
        if value["kind"] == "replace":
            self._validate_state(value["previous_state"])
            if value["backup"] != self.backup.name:
                raise DesktopError("recovery_required")
        elif value["kind"] == "migrate":
            self._validate_state(value["previous_state"])
            if value["backup"] != RETAINED_REGISTRATION:
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

    def _validate_binding(self, value: Any) -> dict[str, Any]:
        if (
            not isinstance(value, dict)
            or value.keys() != _BINDING_KEYS
            or type(value["schema_version"]) is not int
            or value["schema_version"] != 1
            or value["managed_by"] != _OWNER
            or type(value["uid"]) is not int
            or value["uid"] != os.getuid()
            or not isinstance(value["home"], str)
        ):
            raise DesktopError("home_invalid")
        if not _valid_home(self.root, Path(value["home"])):
            raise DesktopError("home_invalid")
        return value

    def read_binding(self) -> dict[str, Any] | None:
        value = self._json(self.binding)
        return self._validate_binding(value) if value is not None else None

    def write_binding(self, home: Path) -> None:
        value = {
            "schema_version": 1,
            "managed_by": _OWNER,
            "uid": os.getuid(),
            "home": str(home),
        }
        self._write_json(self.binding, self._validate_binding(value))

    def remove_binding(self) -> None:
        self._remove(self.binding)

    def new_state(self, *, remaining: int | None, desired: str) -> dict[str, Any]:
        return self._validate_state(
            {
                "schema_version": 1,
                "managed_by": _OWNER,
                "uid": os.getuid(),
                "profile": str(self.home),
                "desired_service": desired,
                "revision": uuid.uuid4().hex,
                "quota_remaining": remaining,
                "quota_checked_at": int(time.time()) if remaining is not None else None,
            }
        )

    def new_journal(
        self,
        *,
        kind: str,
        previous_state: dict[str, Any] | None,
        previous_running: bool,
        remaining: int | None,
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
                "backup": self.backup.name
                if kind == "replace"
                else RETAINED_REGISTRATION
                if kind == "migrate"
                else None,
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
        # Stage outside the own profile: a process death before the first journal
        # publication must not make our own orphan look like a foreign profile.
        # The application root is equally private; stale stages are never read
        # as committed config or used as recovery authority. An adopted home is
        # populated by definition, so its files stage next to their target.
        stage_dir = path.parent if self.adopted else self.root
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=stage_dir)
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

    def _flock(self, path: Path, descriptors: list[int], acquired: list[int]) -> None:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        descriptors.append(descriptor)
        info = os.fstat(descriptor)
        _file(info)
        current = path.lstat()
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise DesktopError("profile_ownership")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DesktopError("profile_busy") from None
        acquired.append(descriptor)

    @contextlib.contextmanager
    def locked(self, *, initialize: bool = False, verify_binding: bool = True) -> Iterator[None]:
        descriptors: list[int] = []
        acquired: list[int] = []
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
            self._flock(self.lock_path, descriptors, acquired)
            if verify_binding:
                bound = self.read_binding()
                expected = Path(bound["home"]) if bound is not None else self.root / "profile"
                if expected != self.home:
                    # Resolved before the lock, re-bound since: a stale profile object
                    # must not act on the wrong home. Retryable: resolve again.
                    raise DesktopError("profile_busy")
            if self.home_lock_path is not None:
                self._flock(self.home_lock_path, descriptors, acquired)
            if not self.adopted and not self.home.exists():
                self.home.mkdir(mode=0o700)
                _sync_directory(self.root)
            _directory(self.home)
            state = self.read_state()
            # An adopted home is populated by definition; only the own profile
            # refuses to take over unknown files (its own lock file aside).
            if (
                state is None
                and not self.adopted
                and any(path.name != ".desktop.lock" for path in self.home.iterdir())
            ):
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
            for descriptor in reversed(acquired):
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            for descriptor in descriptors:
                os.close(descriptor)

    @contextlib.contextmanager
    def shared_lease(self, holder: Profile) -> Iterator[None]:
        """Write through a second Profile object while `holder` owns this root's lock.

        Both objects share `root/.desktop.lock`; a second flock in the same process
        would report the root as busy, so an adoption writes the new home's state
        under the lease the own profile already holds. The adopted home's own lock
        (`home/.desktop.lock`) is still taken here, so two desktop roots can never
        write the same adopted home at once.
        """
        if not holder._leased or holder.lock_path != self.lock_path or self._leased:
            raise DesktopError("storage_error")
        descriptors: list[int] = []
        acquired: list[int] = []
        try:
            if self.home_lock_path is not None:
                self._flock(self.home_lock_path, descriptors, acquired)
            self._leased = True
            yield
        except OSError:
            raise DesktopError("storage_error") from None
        finally:
            self._leased = False
            for descriptor in acquired:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            for descriptor in descriptors:
                os.close(descriptor)
