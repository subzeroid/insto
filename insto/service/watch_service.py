"""Safe macOS LaunchAgent management for the persistent watch service."""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import fcntl
import hashlib
import importlib
import json
import os
import plistlib
import re
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import Any, Protocol, cast
from xml.parsers.expat import ExpatError

from insto.config import Config, config_dir
from insto.exceptions import BackendError

_MANAGED_BY = "insto-watch-service"
_LAUNCHCTL = "/bin/launchctl"
_READINESS_SECONDS = 10.0
_MISSING_DIAGNOSTICS = (
    "could not find service",
    "service cannot be found",
    "no such process",
)
RETAINED_REGISTRATION = "migration-registration.json"
_RETAINED_MAX_BYTES = 262144


@dataclass(frozen=True)
class ServicePaths:
    home: Path
    label: str
    directory: Path
    manifest: Path
    plist: Path
    log_dir: Path


def service_paths(home: Path | None = None) -> ServicePaths:
    canonical = (home or config_dir()).expanduser().resolve()
    digest = hashlib.sha256(os.fsencode(canonical)).hexdigest()[:16]
    label = f"io.insto.watch.{os.getuid()}.{digest}"
    directory = canonical / "services" / "watch"
    return ServicePaths(
        home=canonical,
        label=label,
        directory=directory,
        manifest=directory / "manifest.json",
        plist=Path.home() / "Library" / "LaunchAgents" / f"{label}.plist",
        log_dir=directory / "logs",
    )


def read_private_file(path: Path, *, max_bytes: int = 65536) -> bytes:
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise BackendError(f"refusing to read private service file: {path}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise BackendError(f"refusing non-private service file: {path}")
        if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise BackendError(f"refusing group/world-accessible service file: {path}")
        if info.st_size > max_bytes:
            raise BackendError(f"service file exceeds size limit: {path}")
        data = os.read(fd, max_bytes + 1)
        if len(data) > max_bytes:
            raise BackendError(f"service file exceeds size limit: {path}")
        return data
    except OSError as exc:
        raise BackendError(f"could not safely read service file: {path}") from exc
    finally:
        os.close(fd)


def read_manifest(paths: ServicePaths) -> dict[str, Any]:
    return parse_manifest(paths, read_private_file(paths.manifest))


def parse_manifest(paths: ServicePaths, payload: bytes) -> dict[str, Any]:
    """Validate manifest bytes already read as this home's owned manifest."""
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        # RecursionError: a deeply nested document within the byte limit.
        raise BackendError("invalid watch service manifest") from exc
    keys = {
        "schema_version",
        "managed_by",
        "uid",
        "label",
        "config_home",
        "python",
        "backend",
        "db_path",
        "output_dir",
        "aiograpi_session_path",
        "env_file",
    }
    if not isinstance(value, dict) or set(value) != keys:
        raise BackendError("invalid watch service manifest schema")
    string_keys = {
        "managed_by",
        "label",
        "config_home",
        "python",
        "backend",
        "db_path",
        "output_dir",
        "aiograpi_session_path",
    }
    if (
        type(value["schema_version"]) is not int
        or type(value["uid"]) is not int
        or any(not isinstance(value[key], str) for key in string_keys)
    ):
        raise BackendError("invalid watch service manifest value types")
    if (
        value["schema_version"] != 1
        or value["managed_by"] != _MANAGED_BY
        or value["uid"] != os.getuid()
        or value["label"] != paths.label
        or value["config_home"] != str(paths.home)
    ):
        raise BackendError("watch service manifest ownership mismatch")
    path_keys = ("config_home", "python", "db_path", "output_dir", "aiograpi_session_path")
    if any(not Path(value[k]).is_absolute() for k in path_keys):
        raise BackendError("watch service manifest contains a non-absolute path")
    env_file = value["env_file"]
    if env_file is not None and (not isinstance(env_file, str) or not Path(env_file).is_absolute()):
        raise BackendError("watch service manifest contains an invalid env file")
    return value


class _ConfigResolver(Protocol):
    def __call__(self, home: Path, env_file: Path | None) -> Config: ...


def _resolve_config(home: Path, env_file: Path | None) -> Config:
    module = importlib.import_module("insto.service.watch_service_runner")
    resolver = cast(_ConfigResolver, module.resolve_service_config)
    return resolver(home, env_file)


def _require_macos() -> None:
    if sys.platform != "darwin":
        raise BackendError("watch service management is supported only on macOS")


def _private_directory(path: Path) -> None:
    if os.path.lexists(path):
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise BackendError(f"refusing unsafe service directory: {path}")
        if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise BackendError(f"refusing insecure existing service directory: {path}")
        return
    path.mkdir(mode=0o700, parents=True)


def _owned_directory(path: Path) -> None:
    """Create a directory or verify its final component is an owned real directory."""
    if not os.path.lexists(path):
        path.mkdir(mode=0o700, parents=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise BackendError(f"refusing unsafe service parent directory: {path}")


def _validate_directory(path: Path, *, private: bool) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise BackendError(f"required service directory is unavailable: {path}") from exc
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise BackendError(f"refusing unsafe service directory: {path}")
    if private and info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise BackendError(f"refusing insecure service directory: {path}")


def _validate_service_parents(paths: ServicePaths) -> None:
    _validate_directory(paths.home, private=True)
    _validate_directory(paths.home / "services", private=True)
    _validate_directory(paths.directory, private=True)
    _validate_directory(paths.plist.parent, private=False)


@contextlib.contextmanager
def _management_lock(paths: ServicePaths) -> Iterator[None]:
    lock_path = paths.directory / "management.lock"
    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    fd: int | None = None
    try:
        fd = os.open(lock_path, flags, 0o600)
        before = os.fstat(fd)
        current = os.stat(lock_path, follow_symlinks=False)
        if (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino):
            raise BackendError("watch service management lock changed")
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
        ):
            raise BackendError("unsafe watch service management lock")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception as exc:
        if fd is not None:
            os.close(fd)
        if isinstance(exc, BackendError):
            raise
        raise BackendError("watch service management is already in progress") from exc
    try:
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _sync_dir(path: Path) -> None:
    """Make a directory's entries durable; publications are never claimed before this.

    An ``OSError`` propagates raw in this layer (the desktop layer maps it). On
    macOS ``fsync`` on a directory flushes to the drive cache; ``F_FULLFSYNC`` is
    out of scope.
    """
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _durable_temp(path: Path, content: bytes) -> Path:
    """Write a private, fsynced temporary sibling of ``path`` and return it."""
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        os.fchmod(fd, 0o600)
        stream = os.fdopen(fd, "wb")
        fd = -1  # the stream owns and closes the descriptor from here on
        with stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    return tmp


def _atomic_write(path: Path, content: bytes) -> None:
    tmp = _durable_temp(path, content)
    try:
        # Linking a completed private temporary file publishes it atomically
        # while refusing to replace a target created after our preflight.
        os.link(tmp, path)
        tmp.unlink()
        _sync_dir(path.parent)
    except Exception:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def _replace_file(path: Path, content: bytes) -> None:
    tmp = _durable_temp(path, content)
    try:
        os.replace(tmp, path)
        _sync_dir(path.parent)
    except Exception:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


Registration = tuple[bytes | None, bytes | None]


def _read_component(path: Path) -> bytes | None:
    return read_private_file(path) if os.path.lexists(path) else None


def read_registration(paths: ServicePaths) -> Registration:
    """Exact on-disk manifest and plist bytes; None for an absent component."""
    return _read_component(paths.manifest), _read_component(paths.plist)


def _encode_registration(pair: Registration) -> dict[str, str | None]:
    return {
        "manifest": base64.b64encode(pair[0]).decode() if pair[0] is not None else None,
        "plist": base64.b64encode(pair[1]).decode() if pair[1] is not None else None,
    }


def _decode_registration(value: object) -> Registration:
    if not isinstance(value, dict) or set(value) != {"manifest", "plist"}:
        raise BackendError("retained migration registration is invalid")
    decoded: list[bytes | None] = []
    for key in ("manifest", "plist"):
        raw = value[key]
        if raw is None:
            decoded.append(None)
        elif isinstance(raw, str):
            try:
                decoded.append(base64.b64decode(raw, validate=True))
            except (binascii.Error, ValueError) as exc:
                # ValueError: non-ASCII text is refused before validation.
                raise BackendError("retained migration registration is invalid") from exc
        else:
            raise BackendError("retained migration registration is invalid")
    return decoded[0], decoded[1]


def retain_registration(
    paths: ServicePaths, *, previous: Registration, candidate: Registration
) -> None:
    """Retain both sides of a migration in one durable document; never overwrites."""
    path = paths.directory / RETAINED_REGISTRATION
    if os.path.lexists(path):
        raise BackendError("a migration registration is already retained")
    payload = json.dumps(
        {
            "schema_version": 1,
            "previous": _encode_registration(previous),
            "candidate": _encode_registration(candidate),
        },
        sort_keys=True,
    ).encode()
    if len(payload) > _RETAINED_MAX_BYTES:
        raise BackendError("retained migration registration is too large")
    _atomic_write(path, payload)


def read_retained_registration(paths: ServicePaths) -> dict[str, Registration] | None:
    path = paths.directory / RETAINED_REGISTRATION
    if not os.path.lexists(path):
        return None
    try:
        value = json.loads(read_private_file(path, max_bytes=_RETAINED_MAX_BYTES).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        # RecursionError: a deeply nested document within the byte limit.
        raise BackendError("retained migration registration is invalid") from exc
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "previous", "candidate"}
        or type(value["schema_version"]) is not int  # refuses true and 1.0
        or value["schema_version"] != 1
    ):
        raise BackendError("retained migration registration is invalid")
    return {
        "previous": _decode_registration(value["previous"]),
        "candidate": _decode_registration(value["candidate"]),
    }


def discard_retained_registration(paths: ServicePaths) -> None:
    path = paths.directory / RETAINED_REGISTRATION
    if os.path.lexists(path):
        read_private_file(path, max_bytes=_RETAINED_MAX_BYTES)
        path.unlink()
        _sync_dir(paths.directory)


def replace_registration(
    paths: ServicePaths, expected: Registration, desired: Registration
) -> None:
    """Publish `desired` over a registration that is exactly `expected` (None = absent)."""
    targets = (paths.manifest, paths.plist)
    for path, before in zip(targets, expected, strict=True):
        present = os.path.lexists(path)
        if before is None:
            if present:
                raise BackendError("watch service registration changed during migration")
        elif not _existing_matches(path, before):
            raise BackendError("watch service registration changed during migration")
    transitions = tuple(zip(targets, expected, desired, strict=True))
    # Removals first and plist-first: a death mid-way must never leave an
    # autostart plist without the manifest that proves its ownership.
    for path, before, content in reversed(transitions):
        if content is None and before is not None:
            path.unlink()
            _sync_dir(path.parent)
    for path, before, content in transitions:
        if content is None:
            continue
        if before is None:
            _atomic_write(path, content)  # link-based: refuses a file that appeared meanwhile
        else:
            _replace_file(path, content)


def _run_launchctl(
    arguments: list[str], *, timeout: float = 10
) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            [_LAUNCHCTL, *arguments], capture_output=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BackendError("launchctl operation failed or timed out") from exc


async def _launchctl(
    arguments: list[str], *, timeout: float | None = None, deadline: float | None = None
) -> subprocess.CompletedProcess[bytes]:
    def run() -> subprocess.CompletedProcess[bytes]:
        limit = timeout
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BackendError("launchctl operation deadline expired")
            limit = min(10.0 if limit is None else limit, remaining)
        if limit is None:
            return _run_launchctl(arguments)
        return _run_launchctl(arguments, timeout=limit)

    worker = asyncio.create_task(asyncio.to_thread(run))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError as cancellation:
        # A thread cannot be cancelled. Keep the caller's management lock
        # until launchctl has actually stopped mutating native state.
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        with contextlib.suppress(BaseException):
            worker.result()
        raise cancellation


def _is_missing(result: subprocess.CompletedProcess[bytes]) -> bool:
    diagnostic = (result.stderr + b"\n" + result.stdout).decode("utf-8", "replace").lower()
    return result.returncode != 0 and any(marker in diagnostic for marker in _MISSING_DIAGNOSTICS)


def _parse_launchctl_print(output: bytes) -> dict[str, Any]:
    text = output.decode("utf-8", "replace")
    state = re.search(r"(?m)^\s*state\s*=\s*([A-Za-z_-]+)\s*$", text)
    pid = re.search(r"(?m)^\s*pid\s*=\s*(\d+)\s*$", text)
    last_exit = re.search(r"(?m)^\s*last exit code\s*=\s*(-?\d+)\s*$", text)
    return {
        "state": state.group(1) if state else None,
        "pid": int(pid.group(1)) if pid else None,
        "last_exit_code": int(last_exit.group(1)) if last_exit else None,
    }


def _desired(paths: ServicePaths, config: Config, env_file: Path | None) -> tuple[bytes, bytes]:
    python = os.path.abspath(sys.executable)
    canonical_env = os.path.abspath(env_file.expanduser()) if env_file is not None else None
    manifest = {
        "schema_version": 1,
        "managed_by": _MANAGED_BY,
        "uid": os.getuid(),
        "label": paths.label,
        "config_home": str(paths.home),
        "python": python,
        "backend": config.backend,
        "db_path": str(config.db_path.expanduser().resolve()),
        "output_dir": str(config.output_dir.expanduser().resolve()),
        "aiograpi_session_path": str(config.aiograpi_session_path.expanduser().resolve()),
        "env_file": canonical_env,
    }
    manifest_bytes = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
    plist = _plist_document(paths, manifest, dont_write_bytecode=sys.dont_write_bytecode)
    return manifest_bytes, plistlib.dumps(plist, fmt=plistlib.FMT_XML, sort_keys=True)


def _plist_document(
    paths: ServicePaths, manifest: dict[str, Any], *, dont_write_bytecode: bool = False
) -> dict[str, Any]:
    return {
        "Label": paths.label,
        "ProgramArguments": [
            manifest["python"],
            "-I",
            *(["-B"] if dont_write_bytecode else []),
            "-m",
            "insto.service.watch_service_runner",
            str(paths.manifest),
        ],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "WorkingDirectory": str(paths.home),
        "Umask": 0o77,
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": "/dev/null",
    }


def _existing_matches(path: Path, desired: bytes) -> bool:
    if not os.path.lexists(path):
        return False
    return read_private_file(path) == desired


def _operation_report(
    paths: ServicePaths,
    *,
    changed: bool,
    installed: bool,
    loaded: bool,
    db_path: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "changed": changed,
        "registered": loaded,
        "installation": "installed" if installed else "not_installed",
        "registration": "loaded" if loaded else "unloaded",
        "paths": {"log_dir": str(paths.log_dir), "db": db_path},
    }


async def install_service(
    *, home: Path | None = None, env_file: Path | None = None
) -> dict[str, Any]:
    _require_macos()
    if env_file is not None and not env_file.expanduser().is_absolute():
        raise BackendError("watch service env file path must be absolute")
    paths = service_paths(home)
    _private_directory(paths.home)
    _private_directory(paths.home / "services")
    _private_directory(paths.directory)
    _private_directory(paths.log_dir)
    # LaunchAgents is conventionally 0755 and is managed by macOS/the user;
    # only the plist placed inside it is private.
    _owned_directory(paths.plist.parent)
    with _management_lock(paths):
        _validate_service_parents(paths)
        config = _resolve_config(paths.home, env_file)
        manifest, plist = _desired(paths, config, env_file)
        manifest_exists = os.path.lexists(paths.manifest)
        plist_exists = os.path.lexists(paths.plist)
        target = f"gui/{os.getuid()}"
        domain = await _launchctl(["print", target])
        if domain.returncode != 0:
            raise BackendError("launchctl user GUI domain is unavailable")
        service = await _launchctl(["print", f"{target}/{paths.label}"])
        if service.returncode == 0:
            if not (
                manifest_exists
                and plist_exists
                and _existing_matches(paths.manifest, manifest)
                and _existing_matches(paths.plist, plist)
            ):
                raise BackendError("refusing to adopt an unknown loaded LaunchAgent")
            return _operation_report(
                paths,
                changed=False,
                installed=True,
                loaded=True,
                db_path=str(config.db_path.expanduser().resolve()),
            )
        if not _is_missing(service):
            raise BackendError("could not determine LaunchAgent registration state")
        if manifest_exists and not _existing_matches(paths.manifest, manifest):
            raise BackendError("refusing to replace a different watch service manifest")
        if plist_exists and not _existing_matches(paths.plist, plist):
            raise BackendError("refusing to replace a different LaunchAgent plist")
        if not manifest_exists:
            _atomic_write(paths.manifest, manifest)
        if not plist_exists:
            _atomic_write(paths.plist, plist)
        enabled = await _launchctl(["enable", f"{target}/{paths.label}"])
        if enabled.returncode != 0:
            raise BackendError("launchctl could not enable the LaunchAgent")
        loaded = await _launchctl(["bootstrap", target, str(paths.plist)])
        if loaded.returncode != 0:
            raise BackendError("launchctl could not register the LaunchAgent")
        verified = await _launchctl(["print", f"{target}/{paths.label}"])
        if verified.returncode != 0:
            raise BackendError("launchctl did not confirm LaunchAgent registration")
        return _operation_report(
            paths,
            changed=True,
            installed=True,
            loaded=True,
            db_path=str(config.db_path.expanduser().resolve()),
        )


def _matches_owned_plist(paths: ServicePaths, manifest: dict[str, Any], document: object) -> bool:
    # Removal can be initiated by either interpreter mode. Installation still
    # requires an exact byte match and never silently migrates a registration.
    return any(
        document == _plist_document(paths, manifest, dont_write_bytecode=flag)
        for flag in (False, True)
    )


async def uninstall_service(*, home: Path | None = None) -> dict[str, Any]:
    _require_macos()
    paths = service_paths(home)
    manifest_exists = os.path.lexists(paths.manifest)
    plist_exists = os.path.lexists(paths.plist)
    if not manifest_exists and not plist_exists:
        if not os.path.lexists(paths.directory):
            return _operation_report(
                paths,
                changed=False,
                installed=False,
                loaded=False,
                db_path=str(paths.home / "store.db"),
            )
        _validate_directory(paths.home, private=True)
        _validate_directory(paths.home / "services", private=True)
        _validate_directory(paths.directory, private=True)
        with _management_lock(paths):
            if os.path.lexists(paths.manifest) or os.path.lexists(paths.plist):
                raise BackendError("watch service artifacts changed during uninstall")
            return _operation_report(
                paths,
                changed=False,
                installed=False,
                loaded=False,
                db_path=str(paths.home / "store.db"),
            )
    _validate_service_parents(paths)
    with _management_lock(paths):
        if not os.path.lexists(paths.manifest):
            raise BackendError("refusing to remove a LaunchAgent of unknown ownership")
        manifest = read_manifest(paths)
        plist_exists = os.path.lexists(paths.plist)
        if plist_exists:
            try:
                actual_plist = plistlib.loads(read_private_file(paths.plist))
            except (plistlib.InvalidFileException, ExpatError) as exc:
                raise BackendError("invalid LaunchAgent plist") from exc
            if not _matches_owned_plist(paths, manifest, actual_plist):
                raise BackendError("LaunchAgent plist ownership mismatch")
        target = f"gui/{os.getuid()}/{paths.label}"
        current = await _launchctl(["print", target])
        if current.returncode == 0 and not plist_exists:
            raise BackendError("refusing to unload a service with incomplete ownership documents")
        if current.returncode == 0:
            result = await _launchctl(["bootout", target])
            if result.returncode != 0:
                raise BackendError("launchctl could not unregister the LaunchAgent")
            # bootout returns before launchd has necessarily dropped the label.
            # Poll for absence the way the lifecycle waits for a stop, bounded by
            # one shared deadline so a hung launchctl cannot exceed the budget.
            until = time.monotonic() + _READINESS_SECONDS
            while True:
                remaining = until - time.monotonic()
                if remaining <= 0:
                    break
                current = await _launchctl(["print", target], deadline=until)
                if _is_missing(current):
                    break
                await asyncio.sleep(min(0.05, remaining))
        if not _is_missing(current):
            raise BackendError("could not confirm LaunchAgent absence")
        # The plist goes first: a death between the two unlinks must never leave
        # an autostart plist without the manifest that proves its ownership.
        if plist_exists:
            paths.plist.unlink()
            _sync_dir(paths.plist.parent)
        paths.manifest.unlink()
        _sync_dir(paths.directory)
        return _operation_report(
            paths,
            changed=True,
            installed=False,
            loaded=False,
            db_path=str(manifest["db_path"]),
        )


async def service_status(*, home: Path | None = None) -> dict[str, Any]:
    _require_macos()
    paths = service_paths(home)
    manifest_exists = os.path.lexists(paths.manifest)
    plist_exists = os.path.lexists(paths.plist)
    installation = (
        "installed"
        if manifest_exists and plist_exists
        else "not_installed"
        if not manifest_exists and not plist_exists
        else "incomplete"
    )
    manifest: dict[str, Any] | None = read_manifest(paths) if manifest_exists else None
    result = await _launchctl(["print", f"gui/{os.getuid()}/{paths.label}"])
    registered = result.returncode == 0
    if not registered and not _is_missing(result):
        registration = "unknown"
    else:
        registration = "loaded" if registered else "unloaded"
    process = (
        _parse_launchctl_print(result.stdout)
        if registered
        else {"state": None, "pid": None, "last_exit_code": None}
    )
    watches: list[dict[str, Any]] = []
    database_state = "missing"
    database_error: str | None = None
    try:
        from insto.service.history import read_watches_readonly_async

        db = Path(manifest["db_path"]) if manifest else paths.home / "store.db"
        specs = await read_watches_readonly_async(db)
        if specs is not None:
            from datetime import datetime

            database_state = "ready"
            watches = [
                {
                    "username": spec.user,
                    "status": spec.status,
                    "interval_seconds": spec.interval_seconds,
                    "last_ok": datetime.fromtimestamp(spec.last_ok, UTC)
                    .isoformat()
                    .replace("+00:00", "Z")
                    if spec.last_ok is not None
                    else None,
                    "has_error": spec.last_error is not None,
                }
                for spec in specs
            ]
    except ImportError as exc:
        raise BackendError("watch database reader is unavailable") from exc
    except BackendError:
        database_state = "unavailable"
        database_error = "watch database could not be read safely"
    except (OSError, OverflowError, ValueError):
        database_state = "unavailable"
        database_error = "watch database could not be read safely"
        watches = []
    python = str(manifest["python"]) if manifest else None
    db_path = str(manifest["db_path"]) if manifest else str(paths.home / "store.db")
    return {
        "schema_version": 1,
        "installation": installation,
        "registration": registration,
        "process": process,
        "paths": {
            "home": str(paths.home),
            "manifest": str(paths.manifest),
            "plist": str(paths.plist),
            "db": db_path,
            "log_dir": str(paths.log_dir),
            "python": python,
        },
        "interpreter_available": bool(
            python and Path(python).is_file() and os.access(python, os.X_OK)
        ),
        "database_state": database_state,
        "database_error": database_error,
        "watches": watches,
    }
