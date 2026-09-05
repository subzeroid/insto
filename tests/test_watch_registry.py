import dataclasses
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from insto.desktop.database import read_database, write_database

from insto.service.history import HistoryStore


def create(profile, user="alice"):
    from insto.service.watch_registry import add, public_row

    with write_database(profile, deadline=time.monotonic() + 10) as connection:
        return public_row(add(connection, user, 300))


def test_mutation_revision_fences_status_and_interval(monitoring_profile):
    from insto.service.watch_registry import RegistryError, mutate, public_row

    original = create(monitoring_profile)
    with write_database(monitoring_profile, deadline=time.monotonic() + 10) as connection:
        updated = public_row(
            mutate(connection, "update", "alice", original["revision"], interval=600)
        )
    assert updated["interval_seconds"] == 600
    assert updated["revision"] != original["revision"]
    with (
        pytest.raises(RegistryError, match="conflict"),
        write_database(monitoring_profile, deadline=time.monotonic() + 10) as connection,
    ):
        mutate(connection, "pause", "alice", original["revision"])


def test_tick_only_update_preserves_revision(monitoring_profile):
    from insto.service.watch_registry import lookup, mutate, public_row

    before = create(monitoring_profile)
    store = HistoryStore(monitoring_profile.home / "store.db")
    try:
        spec = store.get_watch("alice")
        assert spec is not None
        assert store.update_watch_state(spec, last_ok=123)
        with write_database(monitoring_profile, deadline=time.monotonic() + 10) as connection:
            assert public_row(lookup(connection, "alice"))["revision"] == before["revision"]
            paused = public_row(mutate(connection, "pause", "alice", before["revision"]))
        assert paused["status"] == "paused"
        assert not store.update_watch_state(dataclasses.replace(spec, last_ok=124), status="active")
        assert store.get_watch("alice").status == "paused"
    finally:
        store.close()


def test_duplicate_add_does_not_resume_and_remove_keeps_history(monitoring_profile):
    from insto.service.watch_registry import RegistryError, add, mutate, public_row

    before = create(monitoring_profile)
    with write_database(monitoring_profile, deadline=time.monotonic() + 10) as connection:
        paused = public_row(mutate(connection, "pause", "alice", before["revision"]))
        connection.execute(
            "INSERT INTO snapshots(target_pk,captured_at,profile_fields_json,last_post_pks_json) "
            "VALUES ('42',1,'{}','[]')"
        )
    with (
        pytest.raises(RegistryError, match="exists"),
        write_database(monitoring_profile, deadline=time.monotonic() + 10) as connection,
    ):
        add(connection, "alice", 300)
    with write_database(monitoring_profile, deadline=time.monotonic() + 10) as connection:
        assert mutate(connection, "remove", "alice", paused["revision"]) is None
    with read_database(monitoring_profile, deadline=time.monotonic() + 10) as connection:
        assert connection.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 1


def test_legacy_registration_concurrent_last_slot(monitoring_profile):
    from insto.service.watch_registry import register_in_transaction

    path = monitoring_profile.home / "store.db"
    create(monitoring_profile, "a")
    create(monitoring_profile, "b")
    barrier = threading.Barrier(2)

    def attempt(user):
        connection = sqlite3.connect(path, isolation_level=None, timeout=1)
        connection.row_factory = sqlite3.Row
        try:
            barrier.wait(timeout=3)
            connection.execute("BEGIN IMMEDIATE")
            kind, _ = register_in_transaction(connection, user, 300)
            connection.commit()
            return kind
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(attempt, user) for user in ("c", "d")]
        assert sorted(f.result(timeout=5) for f in futures) == ["created", "full"]


def test_existing_cli_semantics_preserved(monitoring_profile):
    store = HistoryStore(monitoring_profile.home / "store.db")
    try:
        created = store.register_watch("@Alice", 300)
        assert created.kind == "created"
        assert store.register_watch("alice", 900).spec.interval_seconds == 300
        assert store.update_watch_state(created.spec, status="paused", last_ok=123)
        resumed = store.register_watch("alice", 900)
        assert resumed.kind == "reactivated"
        assert resumed.spec.last_ok == 123
        assert resumed.spec.registration_id != created.spec.registration_id
    finally:
        store.close()
