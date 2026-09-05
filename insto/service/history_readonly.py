"""Bounded saved snapshot reads without storage initialization or provider access."""

from __future__ import annotations

import json
import math
import re
import sqlite3
from collections.abc import Callable, Generator
from dataclasses import dataclass
from typing import Any

from insto.service.history import _PROFILE_TRACKED_FIELDS

Check = Callable[[], None]
RAW_LIMIT = 65536
MAX_TIME = 253402300799
MAX_ID = 9223372036854775807
_PK = re.compile(r"[1-9][0-9]{0,63}")
_USER = re.compile(r"[A-Za-z0-9._]{1,255}")
_HASH = re.compile(r"[0-9a-f]{64}")
_BOOLS = {"is_verified", "is_business", "is_private"}
_COUNTS = {"follower_count", "following_count", "media_count"}
_BYTES = "(length(CAST(profile_fields_json AS BLOB)) + length(CAST(last_post_pks_json AS BLOB)))"
_TYPES = "(typeof(profile_fields_json)='text' AND typeof(last_post_pks_json)='text')"
_BODY_OK = f"({_TYPES} AND {_BYTES} <= {RAW_LIMIT})"
_AVATAR_OK = (
    "(avatar_url_hash IS NULL OR (typeof(avatar_url_hash)='text' "
    "AND length(CAST(avatar_url_hash AS BLOB))=64))"
)
_BANNER_OK = (
    "(banner_url_hash IS NULL OR (typeof(banner_url_hash)='text' "
    "AND length(CAST(banner_url_hash AS BLOB))=64))"
)
PROJECTION = f"""
    id AS snapshot_id,
    CASE WHEN typeof(target_pk)='text' AND length(CAST(target_pk AS BLOB)) BETWEEN 1 AND 64
         THEN target_pk END AS safe_target_pk,
    CASE WHEN typeof(captured_at)='integer' AND captured_at BETWEEN 0 AND {MAX_TIME}
         THEN captured_at END AS safe_captured_at,
    {_BYTES} AS payload_bytes, {_TYPES} AS payload_types_valid,
    CASE WHEN {_BODY_OK} THEN CAST(profile_fields_json AS BLOB) END AS profile_json,
    CASE WHEN {_BODY_OK} THEN CAST(last_post_pks_json AS BLOB) END AS posts_json,
    ({_AVATAR_OK} AND {_BANNER_OK}) AS hashes_valid,
    CASE WHEN {_AVATAR_OK} THEN avatar_url_hash END AS avatar,
    CASE WHEN {_BANNER_OK} THEN banner_url_hash END AS banner
"""


class HistoryReadError(Exception):
    def __init__(self, code: str = "history_corrupt") -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class Metadata:
    identifier: int
    target_pk: str
    captured_at: int

    @property
    def key(self) -> tuple[int, int]:
        return self.captured_at, self.identifier

    def dto(self) -> dict[str, Any]:
        return {"id": str(self.identifier), "target_pk": self.target_pk,
                "captured_at": self.captured_at}


@dataclass(frozen=True, slots=True)
class SavedSnapshot:
    meta: Metadata
    fields: dict[str, Any]
    avatar: str | None
    banner: str | None


def metadata(row: sqlite3.Row) -> Metadata:
    identifier, pk, stamp = row["snapshot_id"], row["safe_target_pk"], row["safe_captured_at"]
    if (
        type(identifier) is not int or not 1 <= identifier <= MAX_ID
        or type(pk) is not str or _PK.fullmatch(pk) is None
        or type(stamp) is not int or not 0 <= stamp <= MAX_TIME
    ):
        raise HistoryReadError()
    return Metadata(identifier, pk, stamp)


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise HistoryReadError()
        result[key] = value
    return result


def _constant(value: str) -> Any:
    raise HistoryReadError()


def _float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise HistoryReadError()
    return number


def _json(raw: object, check: Check) -> Any:
    # Payloads arrive as BLOB (review gate G5): SQLite permits invalid UTF-8 inside
    # TEXT, and the sqlite3 module raises OperationalError while fetching such a
    # cell as str, which would turn one corrupt row into storage_error for the whole
    # request. Decoding here keeps every byte-level fault on the per-record path.
    check()
    if type(raw) is not bytes:
        raise HistoryReadError()
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique,
                           parse_constant=_constant, parse_float=_float)
    except (UnicodeDecodeError, TypeError, ValueError, RecursionError):
        raise HistoryReadError() from None
    check()
    return value


def snapshot(row: sqlite3.Row, check: Check) -> SavedSnapshot:
    check()
    meta = metadata(row)
    if row["payload_types_valid"] != 1 or type(row["payload_bytes"]) is not int:
        raise HistoryReadError()
    if row["payload_bytes"] > RAW_LIMIT:
        raise HistoryReadError("history_oversized")
    if row["hashes_valid"] != 1:
        raise HistoryReadError()
    for name in ("avatar", "banner"):
        value = row[name]
        if value is not None and (type(value) is not str or _HASH.fullmatch(value) is None):
            raise HistoryReadError()
    fields = _json(row["profile_json"], check)
    posts = _json(row["posts_json"], check)
    if type(fields) is not dict or type(posts) is not list:
        raise HistoryReadError()
    for key, value in fields.items():
        check()
        if type(value) not in (str, bool, int, float, type(None)):
            raise HistoryReadError()
        if type(value) is str:
            try:
                value.encode("utf-8")
            except UnicodeError:
                raise HistoryReadError() from None
        if key not in _PROFILE_TRACKED_FIELDS or value is None:
            continue
        if key in _BOOLS:
            valid = type(value) is bool
        elif key in _COUNTS:
            valid = type(value) is int and 0 <= value <= 9007199254740991
        else:
            valid = type(value) is str
        if not valid:
            raise HistoryReadError()
        if key == "username" and (_USER.fullmatch(value) is None or value in {".", ".."}):
            raise HistoryReadError()
    for post in posts:
        check()
        if type(post) is not str:
            raise HistoryReadError()
        try:
            post.encode("utf-8")
        except UnicodeError:
            raise HistoryReadError() from None
    check()
    return SavedSnapshot(meta, fields, row["avatar"], row["banner"])


def comparison(old: SavedSnapshot, new: SavedSnapshot, check: Check) -> dict[str, Any]:
    check()
    changes: list[dict[str, Any]] = []
    unknown: list[str] = []
    for field in _PROFILE_TRACKED_FIELDS:
        check()
        if field not in old.fields or field not in new.fields:
            unknown.append(field)
        elif old.fields[field] != new.fields[field]:
            changes.append({"field": field, "old": old.fields[field], "new": new.fields[field]})
    for field in ("avatar", "banner"):
        before, after = getattr(old, field), getattr(new, field)
        if before != after:
            changes.append({"field": field, "old": before, "new": after})
    check()
    return {"kind": "comparison", "older": old.meta.dto(), "newer": new.meta.dto(),
            "changes": changes, "unknown_fields": unknown}


def scan_sql(
    ceiling: int, frontier: tuple[int, int] | None, target_pk: str | None, limit: int,
) -> tuple[str, tuple[Any, ...]]:
    """Key-only keyset scan: ordered (id, captured_at) pairs, never JSON payloads.

    A global scan still sorts through a temporary B-tree, but it now holds two
    integers per row instead of the projected record (review gate G4). The row-value
    comparison lets idx_snapshots_target_ts bound the range for target scans and
    same-PK predecessors instead of walking every newer entry.
    """
    predicates = ["snapshots.id <= ?"]
    arguments: list[Any] = [ceiling]
    if target_pk is not None:
        predicates.append("snapshots.target_pk = ?")
        arguments.append(target_pk)
    if frontier is not None:
        predicates.append("(snapshots.captured_at, snapshots.id) < (?, ?)")
        arguments.extend(frontier)
    sql = (
        "SELECT snapshots.id, snapshots.captured_at FROM snapshots WHERE "
        + " AND ".join(predicates)
        + " ORDER BY snapshots.captured_at DESC, snapshots.id DESC LIMIT ?"
    )
    arguments.append(limit)
    return sql, tuple(arguments)


def batch_sql(count: int) -> str:
    """Projection for one key batch by primary key; callers restore key order."""
    return "SELECT " + PROJECTION + " FROM snapshots WHERE id IN (" + ",".join("?" * count) + ")"


class Reader:
    def __init__(self, connection: sqlite3.Connection, check: Check) -> None:
        self.connection = connection
        self.check = check

    def ceiling(self) -> int:
        self.check()
        cursor = self.connection.execute("SELECT MAX(id) FROM snapshots")
        try:
            value = cursor.fetchone()[0]
        finally:
            cursor.close()
        self.check()
        if value is None:
            return 0
        if type(value) is not int or not 1 <= value <= MAX_ID:
            raise HistoryReadError()
        return value

    def selected(self, identifier: int) -> sqlite3.Row | None:
        self.check()
        cursor = self.connection.execute(
            "SELECT " + PROJECTION + " FROM snapshots WHERE id=?", (identifier,),
        )
        try:
            row: sqlite3.Row | None = cursor.fetchone()
        finally:
            cursor.close()
        self.check()
        return row

    def rows(
        self, ceiling: int, frontier: tuple[int, int] | None,
        target_pk: str | None, limit: int,
    ) -> Generator[sqlite3.Row, None, None]:
        self.check()
        sql, arguments = scan_sql(ceiling, frontier, target_pk, limit)
        keys = self.connection.execute(sql, arguments)
        remaining = limit
        try:
            while remaining:
                self.check()
                batch = keys.fetchmany(min(16, remaining))
                self.check()
                if not batch:
                    return
                remaining -= len(batch)
                yield from self._projected([int(key[0]) for key in batch])
        finally:
            keys.close()

    def _projected(self, identifiers: list[int]) -> Generator[sqlite3.Row, None, None]:
        cursor = self.connection.execute(batch_sql(len(identifiers)), identifiers)
        try:
            found = {row["snapshot_id"]: row for row in cursor.fetchall()}
        finally:
            cursor.close()
        self.check()
        for identifier in identifiers:
            row = found.get(identifier)
            if row is None:
                # Keys and payloads come from one read transaction, so a missing
                # payload means the id column itself is unreadable, not a delete.
                raise HistoryReadError()
            self.check()
            yield row

    def predecessor(self, current: Metadata, ceiling: int) -> sqlite3.Row | None:
        self.check()
        sql, arguments = scan_sql(ceiling, current.key, current.target_pk, 1)
        cursor = self.connection.execute(sql, arguments)
        try:
            key = cursor.fetchone()
        finally:
            cursor.close()
        self.check()
        if key is None:
            return None
        return self.selected(int(key[0]))
