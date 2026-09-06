"""Exclusive, deadline-bounded lifecycle lease for the desktop watch service."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import plistlib
import re
import stat
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from xml.parsers.expat import ExpatError

from insto.config import Config
from insto.exceptions import BackendError
from insto.service import watch_service as service

_READINESS_SECONDS = 10.0
_STOPPED_STATES = {"not running", "exited", "waiting"}
_STARTING_STATES = {"xpcproxy"}
_STOPPING_STATES = {"SIGTERMed"}


def _outer_fields(output: bytes) -> tuple[dict[str, str], list[str]]:
    """Read job fields, excluding nested launchctl resource/coalition dictionaries."""
    lines = [line.strip() for line in output.decode("utf-8", "replace").splitlines()]
    lines = [line for line in lines if line]
    if lines and lines[0].startswith("gui/") and lines[0].endswith(" = {"):
        if lines[-1] != "}":
            raise BackendError("loaded watch service output is malformed")
        lines = lines[1:-1]
    fields: dict[str, str] = {}
    arguments: list[str] = []
    depth = 0
    in_arguments = False
    for line in lines:
        if in_arguments:
            if line == "}":
                in_arguments = False
                depth = 0
            else:
                arguments.append(line)
            continue
        if line == "}":
            depth -= 1
            if depth < 0:
                raise BackendError("loaded watch service output is malformed")
            continue
        key, separator, value = line.partition(" = ")
        if (
            depth == 0
            and separator
            and key in {"program", "arguments", "path", "state", "pid", "last exit code"}
        ):
            if key in fields:
                raise BackendError("loaded watch service output has ambiguous fields")
            fields[key] = value
            if key == "arguments":
                if value != "{":
                    raise BackendError("loaded watch service arguments are malformed")
                in_arguments = True
        if separator and value == "{":
            depth += 1
    if depth or in_arguments:
        raise BackendError("loaded watch service output is malformed")
    return fields, arguments


def _validated_artifacts(artifacts: tuple[bytes, bytes]) -> tuple[bytes, bytes]:
    """Refuse caller-supplied artifacts that ``_process`` could not parse later."""
    manifest, plist = artifacts
    try:
        json.loads(manifest)
        arguments = plistlib.loads(plist)["ProgramArguments"]
    except (plistlib.InvalidFileException, ExpatError, ValueError, KeyError, TypeError) as exc:
        raise BackendError("watch service artifacts are malformed") from exc
    if not isinstance(arguments, list) or not arguments:
        raise BackendError("watch service artifacts are malformed")
    return artifacts


class ManagedService:
    """Valid only inside ``managed_service``; caller may extend rollback deadline."""

    def __init__(
        self,
        paths: service.ServicePaths,
        config: Config,
        deadline: float,
        *,
        artifacts: tuple[bytes, bytes] | None = None,
    ) -> None:
        self.deadline = deadline
        self._paths = paths
        self._config = config
        self._manifest, self._plist = (
            _validated_artifacts(artifacts)
            if artifacts is not None
            else service._desired(paths, config, None)
        )
        self._db_path = config.db_path.expanduser().absolute()
        self._lock_path = Path(f"{config.db_path.expanduser().resolve()}.watch.lock")
        self._target = f"gui/{os.getuid()}/{paths.label}"
        self._active = True

    def _remaining(self) -> float:
        if not self._active:
            raise BackendError("watch service lease is no longer active")
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise BackendError("watch service operation deadline expired")
        return remaining

    async def _native(
        self, arguments: list[str], *, deadline: float | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        return await service._launchctl(
            arguments,
            timeout=min(10.0, self._remaining()),
            deadline=min(self.deadline, deadline) if deadline is not None else self.deadline,
        )

    def _artifacts(self) -> str:
        service._validate_service_parents(self._paths)
        exists = []
        for path, desired in (
            (self._paths.manifest, self._manifest),
            (self._paths.plist, self._plist),
        ):
            present = os.path.lexists(path)
            if present and not service._existing_matches(path, desired):
                raise BackendError("watch service artifacts do not match the owned runtime")
            exists.append(present)
        return "installed" if all(exists) else "incomplete" if any(exists) else "not_installed"

    def _validate_lock(self, fd: int) -> None:
        info = os.fstat(fd)
        current = self._lock_path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
            or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)
        ):
            raise BackendError("unsafe watch executor lock")

    @contextlib.contextmanager
    def _executor(self, *, create: bool) -> Iterator[tuple[int | None, dict[str, Any]]]:
        self._remaining()
        fd = None
        acquired = False
        try:
            flags = os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
            if create:
                if not os.path.lexists(self._lock_path):
                    info = self._db_path.lstat()
                    if (
                        not stat.S_ISREG(info.st_mode)
                        or info.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) != 0o600
                        or info.st_nlink != 1
                    ):
                        raise BackendError("watch database is unavailable or unsafe")
                flags |= os.O_CREAT
            try:
                fd = os.open(self._lock_path, flags, 0o600)
            except FileNotFoundError:
                if create:
                    raise BackendError("watch executor lock parent is unavailable") from None
                yield None, {"state": "missing", "pid": None}
                return
            self._validate_lock(fd)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                pass
            self._validate_lock(fd)
            raw = os.pread(fd, 64, 0).strip()
            pid = int(raw) if raw.isdigit() and 0 < len(raw) <= 10 else None
            if pid == 0:
                pid = None
            yield fd, {"state": "idle" if acquired else "busy", "pid": pid}
            self._validate_lock(fd)
        except OSError as exc:
            raise BackendError("watch executor lock is unavailable or unsafe") from exc
        finally:
            if fd is not None:
                if acquired:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    @contextlib.contextmanager
    def idle_executor(self) -> Iterator[None]:
        """Hold the stable executor inode without changing its PID contents."""
        with self._executor(create=True) as (_, executor):
            if executor["state"] != "idle":
                raise BackendError("another watch executor is active")
            yield

    def _process(self, output: bytes) -> dict[str, Any]:
        fields, arguments = _outer_fields(output)
        expected = plistlib.loads(self._plist)["ProgramArguments"]
        if (
            fields.get("program") != expected[0]
            or arguments != expected
            or ("path" in fields and fields["path"] != str(self._paths.plist))
        ):
            raise BackendError("loaded watch service runtime provenance is unknown")
        state = fields.get("state")
        if state not in {"running", *_STOPPED_STATES, *_STARTING_STATES, *_STOPPING_STATES}:
            raise BackendError("loaded watch service process state is unknown")
        for key, pattern in (("pid", r"[1-9][0-9]*"), ("last exit code", r"-?[0-9]+")):
            if key == "last exit code" and fields.get(key) == "(never exited)":
                continue
            if key in fields and not re.fullmatch(pattern, fields[key]):
                raise BackendError("loaded watch service process identity is malformed")
        process = service._parse_launchctl_print(
            "\n".join(f"{key} = {value}" for key, value in fields.items()).encode()
        )
        process["state"] = state
        if process["state"] == "running" and not process["pid"]:
            raise BackendError("loaded watch service process identity is unknown")
        return process

    async def inspect_owned(self) -> dict[str, Any]:
        return await self._inspect()

    async def _inspect(self, *, deadline: float | None = None) -> dict[str, Any]:
        self._remaining()
        installation = self._artifacts()
        result = await self._native(["print", self._target], deadline=deadline)
        loaded = result.returncode == 0
        if loaded and installation != "installed":
            raise BackendError("refusing a loaded service with incomplete ownership")
        if not loaded and not service._is_missing(result):
            raise BackendError("watch service registration is unknown")
        process = (
            self._process(result.stdout)
            if loaded
            else {"state": None, "pid": None, "last_exit_code": None}
        )
        with self._executor(create=False) as (_, executor):
            return {
                "installation": installation,
                "registration": "loaded" if loaded else "unloaded",
                "process": process,
                "executor": executor,
            }

    @staticmethod
    def _check_executor(report: dict[str, Any]) -> None:
        executor = report["executor"]
        process = report["process"]
        if executor["state"] == "busy" and (
            process["state"] not in {"running", *_STARTING_STATES, *_STOPPING_STATES}
            or executor["pid"] is None
            or executor["pid"] != process["pid"]
        ):
            raise BackendError("watch executor does not match the owned running service")

    async def _command(self, arguments: list[str]) -> None:
        if (await self._native(arguments)).returncode != 0:
            raise BackendError("watch service native operation failed")

    async def _ready(self) -> dict[str, Any]:
        until = min(self.deadline, time.monotonic() + _READINESS_SECONDS)
        while True:
            report = await self._inspect(deadline=until)
            # A new runner owns the flock before it publishes its PID. Poll
            # that intermediate state, but never declare readiness until the
            # native job and stable executor inode identify the same process.
            if (
                report["process"]["state"] == "running"
                and report["executor"]["state"] == "busy"
                and report["executor"]["pid"] == report["process"]["pid"]
            ):
                return report
            remaining = min(until, self.deadline) - time.monotonic()
            if remaining <= 0:
                raise BackendError("watch service did not become ready")
            await asyncio.sleep(min(0.05, remaining))

    async def ensure_running(self) -> dict[str, Any]:
        report = await self.inspect_owned()
        self._check_executor(report)
        if report["process"]["state"] in {"running", *_STARTING_STATES}:
            return await self._ready()
        # Prove the executor remains idle during artifact publication.
        # Release before startup: the runner must acquire the same inode itself.
        with self.idle_executor():
            self._remaining()
            self._artifacts()
            if report["registration"] == "unloaded":
                for path, desired in (
                    (self._paths.manifest, self._manifest),
                    (self._paths.plist, self._plist),
                ):
                    if not os.path.lexists(path):
                        service._atomic_write(path, desired)
        await self._command(["enable", self._target])
        if report["registration"] == "loaded":
            try:
                await self._command(["kickstart", self._target])
            except BackendError as exc:
                if not isinstance(exc.__cause__, subprocess.TimeoutExpired):
                    raise
                # A timed-out client may already have started the job. The native
                # worker has drained; retain the lease and prove readiness below.
        else:
            await self._command(["bootstrap", f"gui/{os.getuid()}", str(self._paths.plist)])
        return await self._ready()

    async def ensure_stopped(self) -> dict[str, Any]:
        report = await self.inspect_owned()
        self._check_executor(report)
        if report["installation"] != "not_installed":
            await self._command(["disable", self._target])
        if report["registration"] == "loaded":
            await self._command(["bootout", self._target])
        until = min(self.deadline, time.monotonic() + _READINESS_SECONDS)
        while True:
            result = await self._inspect(deadline=until)
            if result["registration"] == "unloaded" and result["executor"]["state"] in {
                "idle",
                "missing",
            }:
                return result
            remaining = min(until, self.deadline) - time.monotonic()
            if remaining <= 0:
                raise BackendError("watch service did not stop")
            await asyncio.sleep(min(0.05, remaining))

    async def remove_registration(self) -> None:
        """Unlink the owned files once the job is unloaded and no executor is running.

        The plist goes first: a death between the two unlinks must never leave an
        autostart plist without the manifest that proves its ownership.
        """
        report = await self.inspect_owned()
        if report["registration"] == "loaded" or report["executor"]["state"] == "busy":
            raise BackendError("watch service is still registered or running")
        for path in (self._paths.plist, self._paths.manifest):
            if os.path.lexists(path):
                path.unlink()
                service._sync_dir(path.parent)


@contextlib.contextmanager
def managed_service(
    *, home: Path, config: Config, deadline: float, artifacts: tuple[bytes, bytes] | None = None
) -> Iterator[ManagedService]:
    """Serialize desktop lifecycle with legacy installation and uninstallation."""
    service._require_macos()
    paths = service.service_paths(home)
    for path in (paths.home, paths.home / "services", paths.directory, paths.log_dir):
        service._private_directory(path)
    service._owned_directory(paths.plist.parent)
    with service._management_lock(paths):
        lease = ManagedService(paths, config, deadline, artifacts=artifacts)
        try:
            yield lease
        finally:
            lease._active = False
