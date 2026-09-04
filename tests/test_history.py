"""Tests for the sqlite history / snapshot / watch store."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path

import pytest

from insto.exceptions import BackendError
from insto.models import Profile
from insto.service.history import (
    CLI_HISTORY_RETENTION_DAYS,
    SNAPSHOT_MAX_PER_TARGET,
    SNAPSHOT_RETENTION_DAYS,
    HistoryStore,
    hash_url,
)


def _create_schema_v1(db: Path, *, user: str = "alice") -> None:
    with sqlite3.connect(db) as raw:
        raw.executescript(
            """
            CREATE TABLE _meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO _meta(key, value) VALUES('schema_version', '1');
            CREATE TABLE watches (
                user TEXT PRIMARY KEY,
                interval_seconds INTEGER NOT NULL,
                last_ok INTEGER,
                last_error TEXT,
                status TEXT NOT NULL DEFAULT 'active'
            );
            """
        )
        raw.execute(
            "INSERT INTO watches VALUES(?, 600, 123, 'old error', 'paused')",
            (user,),
        )


def _make_profile(
    *,
    pk: str = "u1",
    username: str = "alice",
    full_name: str = "Alice",
    biography: str = "",
    follower_count: int = 10,
    avatar_url: str | None = None,
    banner_url: str | None = None,
    avatar_url_hash: str | None = None,
    banner_url_hash: str | None = None,
) -> Profile:
    return Profile(
        pk=pk,
        username=username,
        access="public",
        full_name=full_name,
        biography=biography,
        follower_count=follower_count,
        avatar_url=avatar_url,
        avatar_url_hash=avatar_url_hash,
        banner_url=banner_url,
        banner_url_hash=banner_url_hash,
    )


@pytest.fixture
def store(tmp_path: Path) -> HistoryStore:
    s = HistoryStore(tmp_path / "store.db")
    yield s
    s.close()


def test_creates_db_and_schema(tmp_path: Path) -> None:
    db = tmp_path / "store.db"
    s = HistoryStore(db)
    try:
        assert db.exists()
        assert s.schema_version() == 2
        # All three tables present.
        with sqlite3.connect(str(db)) as raw:
            tables = {
                row[0] for row in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        assert {"cli_history", "watches", "snapshots", "_meta"} <= tables
        with sqlite3.connect(str(db)) as raw:
            columns = {row[1] for row in raw.execute("PRAGMA table_info(watches)")}
        assert {"registration_id", "consecutive_errors"} <= columns
    finally:
        s.close()


def test_db_file_mode_0600(tmp_path: Path) -> None:
    s = HistoryStore(tmp_path / "store.db")
    try:
        mode = (tmp_path / "store.db").stat().st_mode & 0o777
        assert mode == 0o600
    finally:
        s.close()


def test_record_and_recent_targets(store: HistoryStore) -> None:
    store.record_command("/info", "@alice")
    store.record_command("/info", "@bob")
    store.record_command("/posts", "@alice")
    store.record_command("/quota", None)

    recents = store.recent_targets(5)
    # Newest first, deduped, None excluded.
    assert recents == ["@alice", "@bob"]


def test_recent_targets_respects_n(store: HistoryStore) -> None:
    for i in range(10):
        store.record_command("/info", f"@u{i}")
    assert len(store.recent_targets(3)) == 3
    assert store.recent_targets(3)[0] == "@u9"


def test_add_and_last_snapshot(store: HistoryStore) -> None:
    p = _make_profile(pk="42", username="alice", biography="hello")
    snap = store.snapshot_from_profile(p, post_pks=["m1", "m2"])
    store.add_snapshot(snap)

    last = store.last_snapshot("42")
    assert last is not None
    assert last.target_pk == "42"
    assert last.profile_fields["username"] == "alice"
    assert last.profile_fields["biography"] == "hello"
    assert last.last_post_pks == ["m1", "m2"]


def test_last_snapshot_returns_most_recent(store: HistoryStore) -> None:
    p = _make_profile(pk="42", biography="v1")
    s1 = store.snapshot_from_profile(p, post_pks=[])
    s1.captured_at = 100
    store.add_snapshot(s1)

    p.biography = "v2"
    s2 = store.snapshot_from_profile(p, post_pks=[])
    s2.captured_at = 200
    store.add_snapshot(s2)

    last = store.last_snapshot("42")
    assert last is not None
    assert last.profile_fields["biography"] == "v2"


def test_diff_first_seen(store: HistoryStore) -> None:
    p = _make_profile(pk="42")
    d = store.diff("42", p)
    assert d["first_seen"] is True
    assert d["changes"] == {}
    assert d["previous_usernames"] == []


def test_diff_detects_field_changes(store: HistoryStore) -> None:
    p = _make_profile(pk="42", biography="old", follower_count=10)
    store.add_snapshot(store.snapshot_from_profile(p, post_pks=[]))

    p.biography = "new"
    p.follower_count = 11
    d = store.diff("42", p)
    assert d["first_seen"] is False
    assert d["changes"]["biography"] == {"old": "old", "new": "new"}
    assert d["changes"]["follower_count"] == {"old": 10, "new": 11}


def test_diff_username_rename_into_previous(store: HistoryStore) -> None:
    p = _make_profile(pk="42", username="old_handle")
    store.add_snapshot(store.snapshot_from_profile(p, post_pks=[]))

    p.username = "new_handle"
    d = store.diff("42", p)
    assert d["changes"]["username"] == {"old": "old_handle", "new": "new_handle"}
    assert "old_handle" in d["previous_usernames"]
    assert "new_handle" not in d["previous_usernames"]


def test_diff_avatar_banner_hash_change(store: HistoryStore) -> None:
    p = _make_profile(
        pk="42",
        avatar_url_hash="hashA",
        banner_url_hash="hashB",
    )
    store.add_snapshot(store.snapshot_from_profile(p, post_pks=[]))

    p.avatar_url_hash = "hashA2"
    p.banner_url_hash = "hashB2"
    d = store.diff("42", p)
    assert d["changes"]["avatar"] == {"old": "hashA", "new": "hashA2"}
    assert d["changes"]["banner"] == {"old": "hashB", "new": "hashB2"}


def test_diff_emits_avatar_banner_set_to_unset(store: HistoryStore) -> None:
    p = _make_profile(pk="42", avatar_url_hash="hashA", banner_url_hash="hashB")
    store.add_snapshot(store.snapshot_from_profile(p, post_pks=[]))

    p.avatar_url_hash = None
    p.avatar_url = None
    p.banner_url_hash = None
    p.banner_url = None
    d = store.diff("42", p)
    assert d["changes"]["avatar"] == {"old": "hashA", "new": None}
    assert d["changes"]["banner"] == {"old": "hashB", "new": None}


def test_diff_emits_avatar_banner_unset_to_set(store: HistoryStore) -> None:
    p = _make_profile(pk="42")
    store.add_snapshot(store.snapshot_from_profile(p, post_pks=[]))

    p.avatar_url_hash = "hashA"
    p.banner_url_hash = "hashB"
    d = store.diff("42", p)
    assert d["changes"]["avatar"] == {"old": None, "new": "hashA"}
    assert d["changes"]["banner"] == {"old": None, "new": "hashB"}


def test_url_hashing_helper() -> None:
    assert hash_url(None) is None
    assert hash_url("") is None
    h1 = hash_url("https://cdn.example/a.jpg")
    h2 = hash_url("https://cdn.example/a.jpg")
    h3 = hash_url("https://cdn.example/b.jpg")
    assert h1 == h2
    assert h1 != h3
    assert len(h1) == 64  # sha256 hex


def test_snapshot_from_profile_hashes_urls(store: HistoryStore) -> None:
    p = _make_profile(pk="42", avatar_url="https://x/a.jpg", banner_url="https://x/b.jpg")
    snap = store.snapshot_from_profile(p, post_pks=[])
    assert snap.avatar_url_hash == hash_url("https://x/a.jpg")
    assert snap.banner_url_hash == hash_url("https://x/b.jpg")


def test_migrates_schema_v1_atomically_and_preserves_watch(tmp_path: Path) -> None:
    db = tmp_path / "v1.db"
    _create_schema_v1(db, user="@Alice")

    migrated = HistoryStore(db)
    try:
        assert migrated.schema_version() == 2
        watch = migrated.get_watch("alice")
        assert watch is not None
        assert watch.user == "alice"
        assert watch.registration_id
        assert watch.interval_seconds == 600
        assert watch.last_ok == 123
        assert watch.last_error == "old error"
        assert watch.consecutive_errors == 0
        assert watch.status == "paused"
    finally:
        migrated.close()

    reopened = HistoryStore(db)
    try:
        assert reopened.schema_version() == 2
        assert len(reopened.list_watches()) == 1
    finally:
        reopened.close()


def test_schema_v1_migration_deduplicates_canonical_usernames(tmp_path: Path) -> None:
    db = tmp_path / "duplicate-v1.db"
    _create_schema_v1(db, user="@Alice")
    with sqlite3.connect(db) as raw:
        raw.execute("INSERT INTO watches VALUES('alice', 900, 456, NULL, 'active')")

    migrated = HistoryStore(db)
    try:
        assert migrated.schema_version() == 2
        watches = migrated.list_watches()
        assert len(watches) == 1
        assert watches[0].user == "alice"
        assert watches[0].status == "active"
        assert watches[0].interval_seconds == 900
        assert watches[0].last_ok == 456
    finally:
        migrated.close()


def test_failed_schema_v2_migration_rolls_back_all_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import insto.service.history as history_module

    db = tmp_path / "broken-v1.db"
    _create_schema_v1(db)
    monkeypatch.setattr(
        history_module,
        "_MIGRATIONS",
        {2: ("ALTER TABLE watches ADD COLUMN scratch TEXT", "INVALID SQL")},
    )

    with pytest.raises(sqlite3.OperationalError):
        HistoryStore(db)

    with sqlite3.connect(db) as raw:
        version = raw.execute("SELECT value FROM _meta WHERE key = 'schema_version'").fetchone()
        columns = {row[1] for row in raw.execute("PRAGMA table_info(watches)")}
        assert version == ("1",)
        assert "scratch" not in columns


def test_register_watch_enforces_canonical_duplicate_and_global_limit(tmp_path: Path) -> None:
    db = tmp_path / "shared.db"
    first = HistoryStore(db)
    second = HistoryStore(db)
    try:
        for user in ("Alice", "bob", "carol"):
            assert first.register_watch(user, 300).kind == "created"
        assert second.register_watch("@ALICE", 600).kind == "already_active"
        assert second.register_watch("dave", 300).kind == "full"
        assert [watch.user for watch in second.list_watches()] == ["alice", "bob", "carol"]
    finally:
        second.close()
        first.close()


def test_reactivate_uses_new_id_and_stale_state_update_is_ignored(store: HistoryStore) -> None:
    created = store.register_watch("@Alice", 300)
    assert created.spec is not None
    old = created.spec
    assert store.update_watch_state(
        old,
        last_ok=1234,
        last_error="temporary",
        consecutive_errors=2,
        status="paused",
    )

    reactivated = store.register_watch("ALICE", 900)
    assert reactivated.kind == "reactivated"
    assert reactivated.spec is not None
    new = reactivated.spec
    assert new.registration_id != old.registration_id
    assert new.last_ok == 1234
    assert new.last_error is None
    assert new.consecutive_errors == 0
    assert new.status == "active"

    assert store.update_watch_state(old, last_error="stale") is False
    assert store.update_watch_state(new, last_error="fresh") is True
    assert store.update_watch_state(new, last_ok=5678, last_error=None) is True
    current = store.get_watch("@alice")
    assert current is not None
    assert current.last_ok == 5678
    assert current.last_error is None


def test_reactivation_respects_global_active_limit(store: HistoryStore) -> None:
    alice = store.register_watch("alice", 300).spec
    assert alice is not None
    assert store.update_watch_state(alice, status="paused")
    for user in ("bob", "carol", "dave"):
        assert store.register_watch(user, 300).kind == "created"

    result = store.register_watch("alice", 600)

    assert result.kind == "full"
    assert result.spec is None
    current = store.get_watch("alice")
    assert current is not None
    assert current.status == "paused"
    assert len([watch for watch in store.list_watches() if watch.status == "active"]) == 3


def test_watch_registry_validates_inputs_and_deletes_canonically(store: HistoryStore) -> None:
    with pytest.raises(ValueError, match="username"):
        store.register_watch("../alice", 300)
    with pytest.raises(ValueError, match="at least 300"):
        store.register_watch("alice", 299)

    result = store.register_watch("Alice", 300)
    assert result.spec is not None
    assert store.delete_watch("@ALICE") is True
    assert store.delete_watch("alice") is False


async def test_watch_registry_async_wrappers(store: HistoryStore) -> None:
    result = await store.register_watch_async("Alice", 300)
    assert result.spec is not None
    spec = result.spec
    assert await store.get_watch_async("@ALICE") == spec
    assert await store.list_watches_async() == [spec]
    assert await store.update_watch_state_async(spec, consecutive_errors=1) is True
    assert (await store.get_watch_async("alice")).consecutive_errors == 1  # type: ignore[union-attr]
    assert await store.delete_watch_async("ALICE") is True


def test_prune_drops_old_history(store: HistoryStore) -> None:
    # Insert one fresh and one ancient row directly.
    store.record_command("/info", "@fresh")
    cutoff = int(time.time()) - (CLI_HISTORY_RETENTION_DAYS + 1) * 86400
    with store._lock:  # type: ignore[attr-defined]
        store._conn.execute(  # type: ignore[attr-defined]
            "INSERT INTO cli_history(cmd, target, ts) VALUES(?, ?, ?)",
            ("/info", "@old", cutoff),
        )
    result = store.prune()
    assert result["cli_history_deleted"] == 1
    targets = store.recent_targets(10)
    assert "@old" not in targets
    assert "@fresh" in targets


def test_prune_drops_old_snapshots(store: HistoryStore) -> None:
    p = _make_profile(pk="42")
    fresh = store.snapshot_from_profile(p, post_pks=[])
    store.add_snapshot(fresh)
    cutoff = int(time.time()) - (SNAPSHOT_RETENTION_DAYS + 1) * 86400
    with store._lock:  # type: ignore[attr-defined]
        store._conn.execute(  # type: ignore[attr-defined]
            """
            INSERT INTO snapshots(target_pk, captured_at, profile_fields_json,
                last_post_pks_json, avatar_url_hash, banner_url_hash)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            ("42", cutoff, "{}", "[]", None, None),
        )
    result = store.prune()
    assert result["snapshots_deleted"] >= 1


def test_prune_caps_per_target(store: HistoryStore) -> None:
    # Insert SNAPSHOT_MAX_PER_TARGET + 5 rows for the same target.
    with store._lock:  # type: ignore[attr-defined]
        for i in range(SNAPSHOT_MAX_PER_TARGET + 5):
            store._conn.execute(  # type: ignore[attr-defined]
                """
                INSERT INTO snapshots(target_pk, captured_at, profile_fields_json,
                    last_post_pks_json, avatar_url_hash, banner_url_hash)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                ("42", int(time.time()) - i, "{}", "[]", None, None),
            )
    result = store.prune()
    assert result["snapshots_deleted"] >= 5
    with store._lock:  # type: ignore[attr-defined]
        count = store._conn.execute(  # type: ignore[attr-defined]
            "SELECT COUNT(*) FROM snapshots WHERE target_pk = ?", ("42",)
        ).fetchone()[0]
    assert count == SNAPSHOT_MAX_PER_TARGET


def test_purge_history(store: HistoryStore) -> None:
    store.record_command("/info", "@alice")
    store.record_command("/info", "@bob")
    deleted = store.purge_history()
    assert deleted == 2
    assert store.recent_targets(5) == []


def test_purge_snapshots_specific_user(store: HistoryStore) -> None:
    a = _make_profile(pk="A")
    b = _make_profile(pk="B")
    store.add_snapshot(store.snapshot_from_profile(a, post_pks=[]))
    store.add_snapshot(store.snapshot_from_profile(b, post_pks=[]))
    deleted = store.purge_snapshots(user="A")
    assert deleted == 1
    assert store.last_snapshot("A") is None
    assert store.last_snapshot("B") is not None


@pytest.mark.asyncio
async def test_async_record_does_not_block_loop(store: HistoryStore) -> None:
    """An async wrapper that runs a slow sync op via to_thread must not block.

    We monkey-patch the sync `record_command` to sleep 300 ms; meanwhile
    `asyncio.sleep(0.05)` runs concurrently. If the wrapper truly delegates
    to a worker thread, the short asyncio.sleep finishes well before the
    300 ms thread sleep, and the order of completion proves it.
    """
    completed: list[str] = []

    real_record = store.record_command

    def slow_record(cmd: str, target: str | None) -> None:
        time.sleep(0.3)
        real_record(cmd, target)

    store.record_command = slow_record  # type: ignore[method-assign]

    async def long_op() -> None:
        await store.record_command_async("/info", "@alice")
        completed.append("long")

    async def short_tick() -> None:
        await asyncio.sleep(0.05)
        completed.append("short")

    await asyncio.gather(long_op(), short_tick())
    assert completed == ["short", "long"], completed


@pytest.mark.asyncio
async def test_async_wrappers_round_trip(store: HistoryStore) -> None:
    await store.record_command_async("/info", "@alice")
    targets = await store.recent_targets_async(5)
    assert targets == ["@alice"]

    await store.add_snapshot_async(store.snapshot_from_profile(_make_profile(pk="42"), post_pks=[]))
    assert store.last_snapshot("42") is not None

    summary = await store.prune_async()
    assert "cli_history_deleted" in summary
    assert "snapshots_deleted" in summary


def test_lock_retry_raises_on_persistent_lock(
    store: HistoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If sqlite is locked across all retries, surface a friendly BackendError."""
    from insto.service import history as hist

    def always_locked(*_a: object, **_kw: object) -> None:
        raise sqlite3.OperationalError("database is locked")

    # Patch out the actual sleep so the test stays fast.
    monkeypatch.setattr(hist.time, "sleep", lambda _s: None)

    def boom() -> None:
        always_locked()

    with pytest.raises(BackendError) as ei:
        hist._with_lock_retry(boom)
    assert "sqlite is locked" in str(ei.value)


def test_lock_retry_succeeds_after_transient_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from insto.service import history as hist

    monkeypatch.setattr(hist.time, "sleep", lambda _s: None)

    calls = {"n": 0}

    def flaky() -> int:
        calls["n"] += 1
        if calls["n"] < 2:
            raise sqlite3.OperationalError("database is locked")
        return 42

    assert hist._with_lock_retry(flaky) == 42
    assert calls["n"] == 2


def test_migration_idempotent_across_processes(tmp_path: Path) -> None:
    """Opening the same db twice in succession leaves schema_version stable."""
    db = tmp_path / "store.db"
    s1 = HistoryStore(db)
    try:
        v1 = s1.schema_version()
    finally:
        s1.close()
    s2 = HistoryStore(db)
    try:
        assert s2.schema_version() == v1 == 2
    finally:
        s2.close()
