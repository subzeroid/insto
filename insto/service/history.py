"""sqlite-backed history, snapshot, and watch store.

A single `sqlite3.Connection` lives on the `HistoryStore` for the whole
session. The connection is opened with `check_same_thread=False` so that
async wrappers can run statements through `asyncio.to_thread(...)` without
hitting the default thread-affinity check; a `threading.Lock` serialises
the actual cursor work so the connection itself is never touched
concurrently.

Three tables hold session state:

- `cli_history(cmd, target, ts)` — every command issued in REPL or CLI
  mode, used by the welcome screen ("recent targets") and the `/history`
  command. Pruned to 90 days.
- `watches(user, interval_seconds, last_ok, last_error, status)` — every
  registered `/watch <user>`; mirror of `WatchSpec`. CRUD only — the watch
  scheduler itself lives in `service/facade.py`.
- `snapshots(target_pk, captured_at, profile_fields_json, last_post_pks_json,
  avatar_url_hash, banner_url_hash)` — periodic copies of profile state for
  diffing renames / bio edits / pfp swaps. Pruned to 30 days *and* a max of
  100 rows per target_pk.

A `_meta(key, value)` table carries `schema_version`. v0.1 is version 1
with an empty migration list; future versions add entries to
`_MIGRATIONS` and bump `_SCHEMA_VERSION`. Migration runs under
`BEGIN IMMEDIATE` so a second insto process attempting the same migration
blocks on the SQLite write lock and then re-checks the version (no-op
if already migrated).

Retention `prune()` is dispatched as a background task on session start
via `asyncio.to_thread(...)`; it never blocks the welcome screen.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import json
import os
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from insto.exceptions import BackendError
from insto.models import Profile, Snapshot, WatchSpec

_SCHEMA_VERSION = 1

_MIGRATIONS: dict[int, str] = {}

_LOCK_RETRY_DELAYS_MS: tuple[int, ...] = (100, 250, 500)

CLI_HISTORY_RETENTION_DAYS = 90
SNAPSHOT_RETENTION_DAYS = 30
SNAPSHOT_MAX_PER_TARGET = 100

_PROFILE_TRACKED_FIELDS: tuple[str, ...] = (
    "username",
    "full_name",
    "biography",
    "external_url",
    "is_verified",
    "is_business",
    "is_private",
    "follower_count",
    "following_count",
    "media_count",
    "public_email",
    "public_phone",
    "business_category",
)

T = TypeVar("T")


def hash_url(url: str | None) -> str | None:
    """Return sha256 hex digest of a URL (or None if url is None/empty)."""
    if not url:
        return None
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _now_ts() -> int:
    return int(time.time())


def _with_lock_retry(func: Callable[[], T]) -> T:
    """Run `func` and retry on `database is locked` with backoff 100/250/500 ms.

    Falls through with `BackendError` if all retries are exhausted, so the
    caller sees a single readable message rather than a sqlite traceback.
    """
    last_exc: sqlite3.OperationalError | None = None
    for delay_ms in (0, *_LOCK_RETRY_DELAYS_MS):
        if delay_ms:
            time.sleep(delay_ms / 1000.0)
        try:
            return func()
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower():
                raise
            last_exc = exc
    raise BackendError(
        "sqlite is locked — another insto session running?"
    ) from last_exc


class HistoryStore:
    """sqlite-backed history / snapshot / watch store.

    Construct once per session and reuse. `close()` releases the underlying
    connection; the instance becomes unusable afterwards. The store is
    thread-safe in the sense that all calls serialise through a single
    `threading.Lock`, but it is not designed for cross-process concurrent
    *writes* — sqlite locking and the `_with_lock_retry` helper are the
    line of defence against that.
    """

    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        self._lock = threading.Lock()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(db_path),
            check_same_thread=False,
            timeout=5.0,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with contextlib.suppress(OSError):
            os.chmod(db_path, 0o600)
        self._init_schema()
        self._migrate_to_latest()

    @property
    def path(self) -> Path:
        return self._path

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _init_schema(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.executescript(
                """
                CREATE TABLE IF NOT EXISTS _meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cli_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cmd TEXT NOT NULL,
                    target TEXT,
                    ts INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_history_ts
                    ON cli_history(ts);

                CREATE TABLE IF NOT EXISTS watches (
                    user TEXT PRIMARY KEY,
                    interval_seconds INTEGER NOT NULL,
                    last_ok INTEGER,
                    last_error TEXT,
                    status TEXT NOT NULL DEFAULT 'active'
                );

                CREATE TABLE IF NOT EXISTS snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target_pk TEXT NOT NULL,
                    captured_at INTEGER NOT NULL,
                    profile_fields_json TEXT NOT NULL,
                    last_post_pks_json TEXT NOT NULL,
                    avatar_url_hash TEXT,
                    banner_url_hash TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_snapshots_target_ts
                    ON snapshots(target_pk, captured_at);
                """
            )

    def _migrate_to_latest(self) -> None:
        def _do() -> None:
            with self._lock:
                cur = self._conn.cursor()
                cur.execute("BEGIN IMMEDIATE")
                try:
                    row = cur.execute(
                        "SELECT value FROM _meta WHERE key = 'schema_version'"
                    ).fetchone()
                    current = int(row["value"]) if row is not None else None
                    if current is None:
                        cur.execute(
                            "INSERT INTO _meta(key, value) VALUES('schema_version', ?)",
                            (str(_SCHEMA_VERSION),),
                        )
                    elif current < _SCHEMA_VERSION:
                        for v in range(current + 1, _SCHEMA_VERSION + 1):
                            sql = _MIGRATIONS.get(v)
                            if sql is None:
                                raise BackendError(
                                    f"missing migration for schema version {v}"
                                )
                            cur.executescript(sql)
                        cur.execute(
                            "UPDATE _meta SET value = ? WHERE key = 'schema_version'",
                            (str(_SCHEMA_VERSION),),
                        )
                    cur.execute("COMMIT")
                except Exception:
                    cur.execute("ROLLBACK")
                    raise

        _with_lock_retry(_do)

    def schema_version(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM _meta WHERE key = 'schema_version'"
            ).fetchone()
            return int(row["value"]) if row is not None else 0

    # ------------------------------------------------------------------ history

    def record_command(self, cmd: str, target: str | None) -> None:
        def _do() -> None:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO cli_history(cmd, target, ts) VALUES(?, ?, ?)",
                    (cmd, target, _now_ts()),
                )

        _with_lock_retry(_do)

    async def record_command_async(self, cmd: str, target: str | None) -> None:
        await asyncio.to_thread(self.record_command, cmd, target)

    def recent_targets(self, n: int = 5) -> list[str]:
        """Return the most recent N distinct non-null targets, newest first."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT target FROM cli_history
                WHERE target IS NOT NULL AND target != ''
                ORDER BY ts DESC
                """
            ).fetchall()
        seen: set[str] = set()
        out: list[str] = []
        for row in rows:
            t = row["target"]
            if t in seen:
                continue
            seen.add(t)
            out.append(t)
            if len(out) >= n:
                break
        return out

    async def recent_targets_async(self, n: int = 5) -> list[str]:
        return await asyncio.to_thread(self.recent_targets, n)

    # ---------------------------------------------------------------- snapshots

    def add_snapshot(self, snapshot: Snapshot) -> None:
        def _do() -> None:
            with self._lock:
                self._conn.execute(
                    """
                    INSERT INTO snapshots(
                        target_pk, captured_at, profile_fields_json,
                        last_post_pks_json, avatar_url_hash, banner_url_hash
                    ) VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot.target_pk,
                        snapshot.captured_at,
                        json.dumps(snapshot.profile_fields, ensure_ascii=False),
                        json.dumps(snapshot.last_post_pks),
                        snapshot.avatar_url_hash,
                        snapshot.banner_url_hash,
                    ),
                )

        _with_lock_retry(_do)

    async def add_snapshot_async(self, snapshot: Snapshot) -> None:
        await asyncio.to_thread(self.add_snapshot, snapshot)

    def last_snapshot(self, target_pk: str) -> Snapshot | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT target_pk, captured_at, profile_fields_json,
                       last_post_pks_json, avatar_url_hash, banner_url_hash
                FROM snapshots
                WHERE target_pk = ?
                ORDER BY captured_at DESC
                LIMIT 1
                """,
                (target_pk,),
            ).fetchone()
        if row is None:
            return None
        return Snapshot(
            target_pk=row["target_pk"],
            captured_at=row["captured_at"],
            profile_fields=json.loads(row["profile_fields_json"]),
            last_post_pks=list(json.loads(row["last_post_pks_json"])),
            avatar_url_hash=row["avatar_url_hash"],
            banner_url_hash=row["banner_url_hash"],
        )

    def _all_snapshot_usernames(self, target_pk: str) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT profile_fields_json FROM snapshots
                WHERE target_pk = ?
                ORDER BY captured_at ASC
                """,
                (target_pk,),
            ).fetchall()
        names: list[str] = []
        seen: set[str] = set()
        for row in rows:
            try:
                fields = json.loads(row["profile_fields_json"])
            except (TypeError, ValueError):
                continue
            name = fields.get("username") if isinstance(fields, dict) else None
            if isinstance(name, str) and name and name not in seen:
                seen.add(name)
                names.append(name)
        return names

    def diff(self, target_pk: str, current: Profile) -> dict[str, Any]:
        """Compare `current` against the most recent snapshot.

        Returns a dict with `first_seen` (bool), `changes` (field -> {old,new}),
        and `previous_usernames` (every distinct historical username for
        `target_pk` that does not equal `current.username`, oldest first).
        """
        last = self.last_snapshot(target_pk)
        prior_names = [
            n for n in self._all_snapshot_usernames(target_pk) if n != current.username
        ]
        if last is None:
            return {
                "first_seen": True,
                "changes": {},
                "previous_usernames": prior_names,
            }
        changes: dict[str, dict[str, Any]] = {}
        for f in _PROFILE_TRACKED_FIELDS:
            old = last.profile_fields.get(f)
            new = getattr(current, f)
            if old != new:
                changes[f] = {"old": old, "new": new}
        cur_avatar = current.avatar_url_hash or hash_url(current.avatar_url)
        cur_banner = current.banner_url_hash or hash_url(current.banner_url)
        if last.avatar_url_hash and cur_avatar and last.avatar_url_hash != cur_avatar:
            changes["avatar"] = {"old": last.avatar_url_hash, "new": cur_avatar}
        if last.banner_url_hash and cur_banner and last.banner_url_hash != cur_banner:
            changes["banner"] = {"old": last.banner_url_hash, "new": cur_banner}
        return {
            "first_seen": False,
            "changes": changes,
            "previous_usernames": prior_names,
        }

    def snapshot_from_profile(self, profile: Profile, post_pks: list[str]) -> Snapshot:
        """Build a `Snapshot` from `profile` ready for `add_snapshot`."""
        fields: dict[str, Any] = {}
        for f in _PROFILE_TRACKED_FIELDS:
            value = getattr(profile, f)
            fields[f] = value
        return Snapshot(
            target_pk=profile.pk,
            captured_at=_now_ts(),
            profile_fields=fields,
            last_post_pks=list(post_pks),
            avatar_url_hash=profile.avatar_url_hash or hash_url(profile.avatar_url),
            banner_url_hash=profile.banner_url_hash or hash_url(profile.banner_url),
        )

    # ------------------------------------------------------------------ watches

    def add_watch(self, spec: WatchSpec) -> None:
        def _do() -> None:
            with self._lock:
                self._conn.execute(
                    """
                    INSERT INTO watches(user, interval_seconds, last_ok, last_error, status)
                    VALUES(?, ?, ?, ?, ?)
                    ON CONFLICT(user) DO UPDATE SET
                        interval_seconds = excluded.interval_seconds,
                        last_ok = excluded.last_ok,
                        last_error = excluded.last_error,
                        status = excluded.status
                    """,
                    (
                        spec.user,
                        spec.interval_seconds,
                        spec.last_ok,
                        spec.last_error,
                        spec.status,
                    ),
                )

        _with_lock_retry(_do)

    def get_watch(self, user: str) -> WatchSpec | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT user, interval_seconds, last_ok, last_error, status "
                "FROM watches WHERE user = ?",
                (user,),
            ).fetchone()
        if row is None:
            return None
        return _row_to_watchspec(row)

    def list_watches(self) -> list[WatchSpec]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT user, interval_seconds, last_ok, last_error, status "
                "FROM watches ORDER BY user ASC"
            ).fetchall()
        return [_row_to_watchspec(r) for r in rows]

    def update_watch_state(
        self,
        user: str,
        *,
        last_ok: int | None = None,
        last_error: str | None = None,
        status: str | None = None,
    ) -> None:
        sets: list[str] = []
        args: list[Any] = []
        if last_ok is not None:
            sets.append("last_ok = ?")
            args.append(last_ok)
        if last_error is not None:
            sets.append("last_error = ?")
            args.append(last_error)
        if status is not None:
            sets.append("status = ?")
            args.append(status)
        if not sets:
            return
        args.append(user)
        sql = f"UPDATE watches SET {', '.join(sets)} WHERE user = ?"

        def _do() -> None:
            with self._lock:
                self._conn.execute(sql, args)

        _with_lock_retry(_do)

    def delete_watch(self, user: str) -> bool:
        def _do() -> int:
            with self._lock:
                cur = self._conn.execute(
                    "DELETE FROM watches WHERE user = ?", (user,)
                )
                return cur.rowcount

        return _with_lock_retry(_do) > 0

    # ----------------------------------------------------------------- retention

    def prune(self) -> dict[str, int]:
        """Apply retention rules. Return `{cli_history_deleted, snapshots_deleted}`."""
        cutoff_history = _now_ts() - CLI_HISTORY_RETENTION_DAYS * 86400
        cutoff_snapshots = _now_ts() - SNAPSHOT_RETENTION_DAYS * 86400

        def _do() -> dict[str, int]:
            with self._lock:
                cur = self._conn.cursor()
                cur.execute("BEGIN IMMEDIATE")
                try:
                    cur.execute(
                        "DELETE FROM cli_history WHERE ts < ?", (cutoff_history,)
                    )
                    history_deleted = cur.rowcount
                    cur.execute(
                        "DELETE FROM snapshots WHERE captured_at < ?",
                        (cutoff_snapshots,),
                    )
                    snapshots_deleted = cur.rowcount
                    cur.execute(
                        """
                        DELETE FROM snapshots
                        WHERE id IN (
                            SELECT id FROM (
                                SELECT id, ROW_NUMBER() OVER (
                                    PARTITION BY target_pk ORDER BY captured_at DESC
                                ) AS rn
                                FROM snapshots
                            )
                            WHERE rn > ?
                        )
                        """,
                        (SNAPSHOT_MAX_PER_TARGET,),
                    )
                    snapshots_deleted += cur.rowcount
                    cur.execute("COMMIT")
                except Exception:
                    cur.execute("ROLLBACK")
                    raise
            return {
                "cli_history_deleted": history_deleted,
                "snapshots_deleted": snapshots_deleted,
            }

        return _with_lock_retry(_do)

    async def prune_async(self) -> dict[str, int]:
        return await asyncio.to_thread(self.prune)

    # -------------------------------------------------------------------- purges

    def purge_history(self) -> int:
        def _do() -> int:
            with self._lock:
                cur = self._conn.execute("DELETE FROM cli_history")
                return cur.rowcount

        return _with_lock_retry(_do)

    def purge_snapshots(self, user: str | None = None) -> int:
        def _do() -> int:
            with self._lock:
                if user is None:
                    cur = self._conn.execute("DELETE FROM snapshots")
                else:
                    cur = self._conn.execute(
                        "DELETE FROM snapshots WHERE target_pk = ?", (user,)
                    )
                return cur.rowcount

        return _with_lock_retry(_do)

    def purge_cache(self) -> dict[str, int]:
        """Drop history + snapshots; keep watches (they are user intent, not cache)."""
        return {
            "cli_history_deleted": self.purge_history(),
            "snapshots_deleted": self.purge_snapshots(),
        }


def _row_to_watchspec(row: sqlite3.Row) -> WatchSpec:
    status = row["status"] if row["status"] in ("active", "paused") else "active"
    return WatchSpec(
        user=row["user"],
        interval_seconds=row["interval_seconds"],
        last_ok=row["last_ok"],
        last_error=row["last_error"],
        status=status,  # type: ignore[arg-type]
    )


def _profile_to_fields(profile: Profile) -> dict[str, Any]:
    """Public accessor mirroring the diff field set, useful for tests."""
    full = dataclasses.asdict(profile)
    return {f: full.get(f) for f in _PROFILE_TRACKED_FIELDS}
