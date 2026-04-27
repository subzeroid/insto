"""Tests for the sqlite history / snapshot / watch store."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path

import pytest

from insto.exceptions import BackendError
from insto.models import Profile, WatchSpec
from insto.service.history import (
    CLI_HISTORY_RETENTION_DAYS,
    SNAPSHOT_MAX_PER_TARGET,
    SNAPSHOT_RETENTION_DAYS,
    HistoryStore,
    hash_url,
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
        assert s.schema_version() == 1
        # All three tables present.
        with sqlite3.connect(str(db)) as raw:
            tables = {
                row[0] for row in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        assert {"cli_history", "watches", "snapshots", "_meta"} <= tables
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


def test_watches_crud(store: HistoryStore) -> None:
    spec = WatchSpec(user="@alice", interval_seconds=600)
    store.add_watch(spec)

    got = store.get_watch("@alice")
    assert got is not None
    assert got.interval_seconds == 600
    assert got.status == "active"

    store.update_watch_state("@alice", last_ok=1234, status="paused")
    got = store.get_watch("@alice")
    assert got is not None
    assert got.last_ok == 1234
    assert got.status == "paused"

    store.add_watch(WatchSpec(user="@bob", interval_seconds=900))
    assert {w.user for w in store.list_watches()} == {"@alice", "@bob"}

    assert store.delete_watch("@alice") is True
    assert store.delete_watch("@alice") is False
    assert {w.user for w in store.list_watches()} == {"@bob"}


def test_add_watch_upserts(store: HistoryStore) -> None:
    store.add_watch(WatchSpec(user="@alice", interval_seconds=600))
    store.add_watch(WatchSpec(user="@alice", interval_seconds=900))
    got = store.get_watch("@alice")
    assert got is not None
    assert got.interval_seconds == 900


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
        assert s2.schema_version() == v1 == 1
    finally:
        s2.close()
