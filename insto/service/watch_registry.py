"""Watch registration domain shared by CLI and desktop; caller owns the transaction."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from typing import Any, Literal

RegistrationKind = Literal["created", "already_active", "reactivated", "full"]
ACTIVE_LIMIT = 3
_LEGACY_SELECT = (
    "SELECT user, registration_id, interval_seconds, last_ok, last_error, "
    "consecutive_errors, status FROM watches"
)
# Every column is bounded in SQL before Python materializes it: SQLite's dynamic
# typing accepts arbitrary TEXT in INTEGER columns (the CHECK constraints compare
# text as greater than any number), so an unguarded 10 MiB cell would be fetched in
# full before public_row() could reject it. Invalid cells become NULL (or -1 for the
# nullable last_ok) and are still reported explicitly as invalid_data.
_PUBLIC_SELECT = (
    "SELECT CASE WHEN length(CAST(user AS BLOB))<=255 THEN user END AS user, "
    "CASE WHEN length(CAST(registration_id AS BLOB))<=128 THEN registration_id END "
    "AS registration_id, "
    "CASE WHEN typeof(interval_seconds)='integer' "
    "AND interval_seconds BETWEEN 300 AND 2147483647 THEN interval_seconds END "
    "AS interval_seconds, "
    "CASE WHEN last_ok IS NULL THEN NULL "
    "WHEN typeof(last_ok)='integer' AND last_ok BETWEEN 0 AND 253402300799 THEN last_ok "
    "ELSE -1 END AS last_ok, "
    "CASE WHEN last_error IS NULL THEN 0 ELSE 1 END AS has_error, "
    "CASE WHEN typeof(consecutive_errors)='integer' "
    "AND consecutive_errors BETWEEN 0 AND 9007199254740991 THEN consecutive_errors END "
    "AS consecutive_errors, "
    "CASE WHEN status IN ('active','paused') THEN status END AS status FROM watches"
)
# Qualify the table column: a bare `user` here would resolve to the CASE alias and
# force a temporary B-tree sort instead of walking the primary-key index.
_LIST_SQL = _PUBLIC_SELECT + " WHERE watches.user>? ORDER BY watches.user ASC LIMIT ?"


class RegistryError(Exception):
    """A bounded internal reason mapped by the desktop adapter to static errors."""


def _transaction(connection: sqlite3.Connection) -> None:
    if not connection.in_transaction:
        raise RuntimeError("watch mutation requires an existing transaction")


def _has_slot(connection: sqlite3.Connection) -> bool:
    count = connection.execute("SELECT COUNT(*) FROM watches WHERE status='active'").fetchone()[0]
    return int(count) < ACTIVE_LIMIT


def register_in_transaction(
    connection: sqlite3.Connection,
    user: str,
    interval: int,
) -> tuple[RegistrationKind, sqlite3.Row | None]:
    _transaction(connection)
    row = connection.execute(_LEGACY_SELECT + " WHERE user=?", (user,)).fetchone()
    if row is not None and row["status"] == "active":
        return "already_active", row
    if not _has_slot(connection):
        return "full", None
    if row is None:
        connection.execute(
            "INSERT INTO watches(user,registration_id,interval_seconds,last_ok,"
            "last_error,consecutive_errors,status) VALUES(?,?,?,NULL,NULL,0,'active')",
            (user, uuid.uuid4().hex, interval),
        )
        kind: RegistrationKind = "created"
    else:
        connection.execute(
            "UPDATE watches SET registration_id=?, interval_seconds=?, last_error=NULL, "
            "consecutive_errors=0, status='active' WHERE user=?",
            (uuid.uuid4().hex, interval, user),
        )
        kind = "reactivated"
    return kind, connection.execute(_LEGACY_SELECT + " WHERE user=?", (user,)).fetchone()


def lookup(connection: sqlite3.Connection, user: str) -> sqlite3.Row:
    row: sqlite3.Row | None = connection.execute(
        _PUBLIC_SELECT + " WHERE watches.user=?", (user,)
    ).fetchone()
    if row is None:
        raise RegistryError("not_found")
    public_row(row)
    return row


def public_row(row: sqlite3.Row) -> dict[str, Any]:
    user, generation = row["user"], row["registration_id"]
    interval, status = row["interval_seconds"], row["status"]
    last_ok, errors = row["last_ok"], row["consecutive_errors"]
    if (
        not isinstance(user, str)
        or re.fullmatch(r"[a-z0-9._]{1,255}", user) is None
        or user in (".", "..")
        or not isinstance(generation, str)
        or not 1 <= len(generation) <= 128
        or type(interval) is not int
        or not 300 <= interval <= 2**31 - 1
        or status not in ("active", "paused")
        or (last_ok is not None and (type(last_ok) is not int or not 0 <= last_ok <= 253402300799))
        or type(errors) is not int
        or not 0 <= errors <= 2**53 - 1
        or row["has_error"] not in (0, 1)
    ):
        raise RegistryError("invalid_data")
    payload = json.dumps([user, generation, status, interval], separators=(",", ":"))
    return {
        "user": user,
        "status": status,
        "interval_seconds": interval,
        "last_ok": last_ok,
        "waiting_first_check": last_ok is None,
        "has_error": bool(row["has_error"]),
        "consecutive_errors": errors,
        "revision": hashlib.sha256(payload.encode()).hexdigest(),
    }


def add(connection: sqlite3.Connection, user: str, interval: int) -> sqlite3.Row:
    _transaction(connection)
    if connection.execute("SELECT 1 FROM watches WHERE user=?", (user,)).fetchone():
        raise RegistryError("exists")
    kind, _ = register_in_transaction(connection, user, interval)
    if kind == "full":
        raise RegistryError("limit")
    return lookup(connection, user)


def mutate(
    connection: sqlite3.Connection,
    action: str,
    user: str,
    expected_revision: str,
    *,
    interval: int | None = None,
) -> sqlite3.Row | None:
    _transaction(connection)
    row = lookup(connection, user)
    if public_row(row)["revision"] != expected_revision:
        raise RegistryError("conflict")
    if action == "remove":
        connection.execute("DELETE FROM watches WHERE user=?", (user,))
        return None
    status = row["status"]
    next_status = {"pause": "paused", "resume": "active"}.get(action, status)
    next_interval = interval if action == "update" else row["interval_seconds"]
    if action not in ("pause", "resume", "update") or next_interval is None:
        raise RegistryError("invalid_data")
    if (next_status, next_interval) == (status, row["interval_seconds"]):
        return row
    resumed = status == "paused" and next_status == "active"
    if resumed and not _has_slot(connection):
        raise RegistryError("limit")
    connection.execute(
        "UPDATE watches SET registration_id=?, status=?, interval_seconds=?, "
        "last_error=CASE WHEN ? THEN NULL ELSE last_error END, "
        "consecutive_errors=CASE WHEN ? THEN 0 ELSE consecutive_errors END WHERE user=?",
        (uuid.uuid4().hex, next_status, next_interval, resumed, resumed, user),
    )
    return lookup(connection, user)


def list_page(
    connection: sqlite3.Connection,
    *,
    after: str,
    limit: int,
) -> tuple[list[dict[str, Any]], str | None]:
    rows = connection.execute(_LIST_SQL, (after, limit + 1)).fetchall()
    items = [public_row(row) for row in rows[:limit]]
    return items, items[-1]["user"] if len(rows) > limit else None
