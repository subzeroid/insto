"""Bounded managed-profile access; no creation, migration or C1 snapshot copies."""

from __future__ import annotations

import contextlib
import os
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from insto.config import Config
from insto.desktop.configuration import _database_file, parse_profile_config
from insto.desktop.errors import DesktopError
from insto.desktop.profile import Profile
from insto.service.history import _SCHEMA_VERSION


def check_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise DesktopError("operation_timeout")


def inspect_profile_files(profile: Profile, *, deadline: float) -> tuple[dict[str, Any], Config]:
    check_deadline(deadline)
    state = profile.read_state()
    if profile.read_journal() is not None or profile.read_backup() is not None:
        raise DesktopError("recovery_required")
    if state is None:
        raise DesktopError("not_configured")
    payload = profile.read_config()
    if payload is None:
        raise DesktopError("recovery_required")
    config = parse_profile_config(profile, payload)
    check_deadline(deadline)
    if profile.read_journal() is not None or profile.read_backup() is not None:
        raise DesktopError("recovery_required")
    if profile.read_state() != state or profile.read_config() != payload:
        raise DesktopError("profile_busy")
    check_deadline(deadline)
    return state, config


def _files(path: Path) -> tuple[int, int]:
    if not os.path.lexists(path):
        if any(os.path.lexists(Path(str(path) + s)) for s in ("-wal", "-shm", "-journal")):
            raise DesktopError("schema_mismatch")
        raise DesktopError("not_configured")
    main = _database_file(path)
    for suffix in ("-wal", "-shm", "-journal"):
        side = Path(str(path) + suffix)
        if os.path.lexists(side):
            _database_file(side)
            if suffix == "-journal":
                raise DesktopError("schema_mismatch")
    return main.st_dev, main.st_ino


def _schema(connection: sqlite3.Connection, deadline: float) -> None:
    try:
        version = connection.execute(
            "SELECT value FROM _meta WHERE key='schema_version' LIMIT 2"
        ).fetchall()
        if [tuple(row) for row in version] != [(str(_SCHEMA_VERSION),)]:
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
    except sqlite3.Error as error:
        check_deadline(deadline)
        code = getattr(error, "sqlite_errorcode", 0) & 0xFF
        if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            raise DesktopError("profile_busy") from None
        if code in (sqlite3.SQLITE_NOTADB, sqlite3.SQLITE_CORRUPT):
            # A garbage or torn file is storage damage, not a version mismatch
            # (review gate G10); the user needs recovery, not an upgrade hint.
            raise DesktopError("storage_error") from None
        raise DesktopError("schema_mismatch") from None


@contextlib.contextmanager
def _connection(
    path: Path,
    *,
    deadline: float,
    write: bool,
) -> Iterator[sqlite3.Connection]:
    connection: sqlite3.Connection | None = None
    try:
        check_deadline(deadline)
        identity = _files(path)
        connection = sqlite3.connect(
            path.as_uri() + ("?mode=rw" if write else "?mode=ro"),
            uri=True,
            isolation_level=None,
            timeout=min(1.0, max(0.0, deadline - time.monotonic())),
        )
        connection.row_factory = sqlite3.Row
        connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        if not write:
            connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        _schema(connection, deadline)
        if _files(path) != identity:
            raise DesktopError("profile_ownership")
        check_deadline(deadline)
        yield connection
        check_deadline(deadline)
        if write:
            connection.commit()
            check_deadline(deadline)
    except sqlite3.Error as error:
        check_deadline(deadline)
        code = getattr(error, "sqlite_errorcode", 0) & 0xFF
        if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            raise DesktopError("profile_busy") from None
        raise DesktopError("storage_error") from None
    except OSError:
        check_deadline(deadline)
        raise DesktopError("profile_ownership") from None
    finally:
        if connection is not None:
            connection.set_progress_handler(None, 0)
            with contextlib.suppress(sqlite3.Error):
                connection.rollback()
            connection.close()


@contextlib.contextmanager
def read_database(profile: Profile, *, deadline: float) -> Iterator[sqlite3.Connection]:
    _, config = inspect_profile_files(profile, deadline=deadline)
    with _connection(config.db_path, deadline=deadline, write=False) as connection:
        yield connection


@contextlib.contextmanager
def write_database(profile: Profile, *, deadline: float) -> Iterator[sqlite3.Connection]:
    # Validate before acquiring a lease: locked() may create home/lock files.
    _, config = inspect_profile_files(profile, deadline=deadline)
    try:
        _files(config.db_path)
    except OSError:
        raise DesktopError("profile_ownership") from None
    with profile.locked():
        _, config = inspect_profile_files(profile, deadline=deadline)
        with _connection(config.db_path, deadline=deadline, write=True) as connection:
            yield connection
