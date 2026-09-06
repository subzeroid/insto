"""Explicit desktop configuration and non-migrating database preflight."""

from __future__ import annotations

import contextlib
import os
import sqlite3
import stat
import tempfile
import time
import tomllib
from pathlib import Path

import tomli_w

from insto._redact import register_secret
from insto.config import BACKEND_HIKERAPI, Config, normalize_backend
from insto.desktop.access import validate_token
from insto.desktop.errors import DesktopError
from insto.desktop.profile import Profile, _directory, _sync_directory, _trusted_ancestors
from insto.exceptions import BackendError
from insto.service.history import _SCHEMA_VERSION, HistoryStore
from insto.service.watch_service_runner import load_home_config


def check_database_location(home: Path, db_path: Path) -> None:
    """An external database sits in an existing owned private directory under trusted
    parents, like the home itself; inside the home the home's own checks cover it.

    Raises `home_invalid`. Shared by the adopted config parser (every desktop open)
    and `home.inspect`, so inspection and later opens agree.
    """
    if db_path.parent == home or home in db_path.parents:
        return
    try:
        _trusted_ancestors(db_path)
        _directory(db_path.parent)
    except (DesktopError, OSError):
        raise DesktopError("home_invalid") from None


def config_bytes(profile: Profile, token: str) -> bytes:
    validate_token(token)
    return tomli_w.dumps(
        {
            "backend": "hikerapi",
            "db_path": str(profile.home / "store.db"),
            "output_dir": str(profile.home / "output"),
            "hikerapi": {"token": token},
            "aiograpi": {"session_path": str(profile.home / "aiograpi.session.json")},
        }
    ).encode()


def parse_config(profile: Profile, payload: bytes) -> Config:
    try:
        value = tomllib.loads(payload.decode())
        token = value["hikerapi"]["token"]
        validate_token(token)
        if value != tomllib.loads(config_bytes(profile, token).decode()):
            raise ValueError
    except (ValueError, KeyError, TypeError, DesktopError):
        raise DesktopError("profile_ownership") from None
    register_secret(token)
    output = profile.home / "output"
    if os.path.lexists(output):
        _directory(output)
    for leaf in ("aiograpi.session.json", "cli_history"):
        path = profile.home / leaf
        if os.path.lexists(path):
            _database_file(path)
    return Config(
        backend="hikerapi",
        hiker_token=token,
        hiker_proxy=None,
        db_path=profile.home / "store.db",
        output_dir=profile.home / "output",
        cli_history_path=profile.home / "cli_history",
        aiograpi_session_path=profile.home / "aiograpi.session.json",
        watch_webhook_url=None,
    )


def parse_profile_config(profile: Profile, payload: bytes) -> Config:
    """Strict desktop shape for the own profile; the runner's resolver for an adopted home."""
    if not profile.adopted:
        return parse_config(profile, payload)
    try:
        data = tomllib.loads(payload.decode())
    except (UnicodeDecodeError, ValueError):
        raise DesktopError("home_invalid") from None
    # Name the backend before the runner's credential checks: an aiograpi home
    # without credentials is unsupported here, not invalid.
    backend = data.get("backend")
    if isinstance(backend, str) and normalize_backend(backend) != BACKEND_HIKERAPI:
        raise DesktopError("home_backend_unsupported")
    try:
        config = load_home_config(profile.home, data)
    except (ValueError, TypeError, BackendError):
        # TypeError: a non-string path value such as `db_path = 5` reaches Path().
        raise DesktopError("home_invalid") from None
    if config.backend != BACKEND_HIKERAPI:
        # An absent key still defaults through load_config; the resolved value rules.
        raise DesktopError("home_backend_unsupported")
    check_database_location(profile.home, config.db_path)
    token = config.hiker_token
    if not isinstance(token, str):
        raise DesktopError("home_invalid")
    try:
        validate_token(token)
    except DesktopError:
        raise DesktopError("home_invalid") from None
    register_secret(token)
    return config


def adopted_config_bytes(payload: bytes, token: str) -> bytes:
    """Rewrite only the HikerAPI token of an adopted home's TOML; other keys survive.

    The file is re-serialised from its parsed tables, so TOML comments and layout
    are not preserved (as the design documents).
    """
    validate_token(token)
    try:
        data = tomllib.loads(payload.decode())
    except (UnicodeDecodeError, ValueError):
        raise DesktopError("home_invalid") from None
    hikerapi, legacy = data.get("hikerapi"), data.get("hiker")
    if any(value is not None and not isinstance(value, dict) for value in (hikerapi, legacy)):
        raise DesktopError("home_invalid")
    section = hikerapi if hikerapi is not None else legacy
    if section is None:
        section = data.setdefault("hikerapi", {})
    section["token"] = token
    return tomli_w.dumps(data).encode()


def config_payload(profile: Profile, current: bytes | None, token: str) -> bytes:
    if profile.adopted:
        return adopted_config_bytes(current or b"", token)
    return config_bytes(profile, token)


def _database_file(path: Path) -> os.stat_result:
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
    ):
        raise DesktopError("profile_ownership")
    return info


def _fingerprint(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _schema(path: Path, *, immutable: bool) -> None:
    with contextlib.closing(
        sqlite3.connect(
            path.as_uri() + ("?mode=ro&immutable=1" if immutable else "?mode=ro"),
            uri=True,
            timeout=0,
        )
    ) as connection:
        version = connection.execute(
            "SELECT value FROM _meta WHERE key='schema_version'"
        ).fetchall()
        if version != [(str(_SCHEMA_VERSION),)]:
            raise DesktopError("schema_mismatch")
        connection.execute(
            "SELECT user, registration_id, interval_seconds, last_ok, last_error, "
            "consecutive_errors, status FROM watches LIMIT 0"
        )
        connection.execute("SELECT id, cmd, target, ts FROM cli_history LIMIT 0")
        connection.execute(
            "SELECT id, target_pk, captured_at, profile_fields_json, last_post_pks_json, "
            "avatar_url_hash, banner_url_hash FROM snapshots LIMIT 0"
        )


def check_database(path: Path, *, deadline: float | None = None) -> bool:
    """Refuse unsafe files or unsupported schemas without SQLite writes.

    With WAL present, inspect a private disposable copy so SQLite can reconstruct
    its current schema without touching source sidecars. Concurrent source changes
    fail closed; callers can inspect again. Never ignore an uncheckpointed schema.
    """
    if deadline is not None and time.monotonic() >= deadline:
        raise DesktopError("operation_timeout")
    if not os.path.lexists(path):
        # An absent main file does not make existing SQLite sidecars ours. A
        # newly published database could otherwise replay an unrelated WAL.
        if any(
            os.path.lexists(Path(str(path) + suffix)) for suffix in ("-wal", "-shm", "-journal")
        ):
            raise DesktopError("schema_mismatch")
        return False
    try:
        sources = {path: _fingerprint(_database_file(path))}
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(str(path) + suffix)
            if os.path.lexists(sidecar):
                sources[sidecar] = _fingerprint(_database_file(sidecar))
        if Path(str(path) + "-journal") in sources:
            raise DesktopError("schema_mismatch")
        if Path(str(path) + "-wal") in sources:
            with tempfile.TemporaryDirectory(prefix="insto-schema-") as temporary:
                snapshot = Path(temporary) / "store.db"
                for source in (path, Path(str(path) + "-wal")):
                    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    with os.fdopen(descriptor, "rb") as stream:
                        if _fingerprint(os.fstat(stream.fileno())) != sources[source]:
                            raise DesktopError("schema_mismatch")
                        target = snapshot if source == path else Path(str(snapshot) + "-wal")
                        target_fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                        with os.fdopen(target_fd, "wb") as output:
                            while block := stream.read(1024 * 1024):
                                if deadline is not None and time.monotonic() >= deadline:
                                    raise DesktopError("operation_timeout")
                                output.write(block)
                _schema(snapshot, immutable=False)
        else:
            _schema(path, immutable=True)
        for source, fingerprint in sources.items():
            if _fingerprint(_database_file(source)) != fingerprint:
                raise DesktopError("schema_mismatch")
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(str(path) + suffix)
            if os.path.lexists(sidecar) != (sidecar in sources):
                raise DesktopError("schema_mismatch")
        return True
    except (sqlite3.Error, OSError):
        raise DesktopError("schema_mismatch") from None


def initialize_database(
    path: Path, *, deadline: float | None = None, stage_dir: Path | None = None
) -> None:
    if check_database(path, deadline=deadline):
        return
    # Stage outside the profile by default, so process death cannot strand a partial
    # final database or turn a not-yet-bound profile into a populated foreign profile.
    # An adopted home stages beside its database (same filesystem, private temp dir).
    with tempfile.TemporaryDirectory(
        prefix=".desktop-db-", dir=stage_dir if stage_dir is not None else path.parent.parent
    ) as temporary:
        staged = Path(temporary) / "store.db"
        descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(descriptor)
        store = HistoryStore(staged)
        store.close()
        check_database(staged, deadline=deadline)
        with staged.open("rb") as stream:
            os.fsync(stream.fileno())
        # A concurrent writer (the CLI) may have published first: its database is
        # kept and its schema decides; sidecars that appeared meanwhile are refused
        # by the same check. The link never replaces a file that won the race.
        if check_database(path, deadline=deadline):
            return
        if deadline is not None and time.monotonic() >= deadline:
            raise DesktopError("operation_timeout")
        try:
            os.link(staged, path)
        except FileExistsError:
            pass
        else:
            staged.unlink()
        _sync_directory(Path(temporary))
    _sync_directory(path.parent)
    check_database(path, deadline=deadline)
