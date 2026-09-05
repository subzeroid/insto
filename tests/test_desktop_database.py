import contextlib
import os
import sqlite3
import time

import pytest

from insto.desktop.errors import DesktopError
from insto.desktop.profile import Profile


def test_missing_reader_never_creates_root(tmp_path):
    from insto.desktop.database import read_database

    profile = Profile(tmp_path / "missing")
    with (
        pytest.raises(DesktopError, match="not_configured"),
        read_database(profile, deadline=time.monotonic() + 10),
    ):
        pytest.fail("missing profile accepted")
    assert not profile.root.exists()


def test_read_is_query_only_without_c1_copy(monitoring_profile, monkeypatch):
    from insto.desktop import configuration, database

    profile = monitoring_profile
    original_check = configuration.check_database
    monkeypatch.setattr(
        configuration,
        "check_database",
        lambda *a, **k: pytest.fail("C2 invoked full-copy C1 preflight"),
    )
    path = profile.home / "store.db"
    before = path.read_bytes()
    with database.read_database(profile, deadline=time.monotonic() + 10) as connection:
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM watches").fetchone()[0] == 0
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM watches")
    assert path.read_bytes() == before
    assert {p.name for p in profile.home.iterdir()} <= {
        "config.toml",
        "store.db",
        "store.db-wal",
        "store.db-shm",
    }
    for suffix in ("", "-wal", "-shm"):
        side = profile.home / ("store.db" + suffix)
        if side.exists():
            info = side.lstat()
            assert info.st_uid == os.getuid()
            assert info.st_nlink == 1
            assert info.st_mode & 511 == 384
    assert original_check(path)


def test_reader_does_not_acquire_existing_profile_lease(monitoring_profile):
    from insto.desktop.database import read_database

    with (
        monitoring_profile.locked(),
        read_database(monitoring_profile, deadline=time.monotonic() + 10) as connection,
    ):
        assert connection.execute("SELECT COUNT(*) FROM watches").fetchone()[0] == 0


def test_expired_reader_does_not_touch_missing_root(tmp_path):
    from insto.desktop.database import read_database

    profile = Profile(tmp_path / "missing")
    with (
        pytest.raises(DesktopError, match="operation_timeout"),
        read_database(profile, deadline=time.monotonic() - 1),
    ):
        pytest.fail("expired budget accepted")
    assert not profile.root.exists()


def test_live_wal_read_snapshot_and_release(monitoring_profile):
    from insto.desktop.database import read_database

    path = monitoring_profile.home / "store.db"
    with contextlib.closing(sqlite3.connect(path, isolation_level=None)) as writer:
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("INSERT INTO cli_history(cmd, ts) VALUES ('before', 1)")
        with read_database(monitoring_profile, deadline=time.monotonic() + 10) as reader:
            assert reader.execute("SELECT COUNT(*) FROM cli_history").fetchone()[0] == 1
            writer.execute("INSERT INTO cli_history(cmd, ts) VALUES ('after', 2)")
            assert reader.execute("SELECT COUNT(*) FROM cli_history").fetchone()[0] == 1
        with read_database(monitoring_profile, deadline=time.monotonic() + 10) as reader:
            assert reader.execute("SELECT COUNT(*) FROM cli_history").fetchone()[0] == 2
        assert tuple(writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()) == (0, 0, 0)


def test_future_schema_and_recovery_refuse_reads(monitoring_profile):
    from insto.desktop.database import read_database

    profile = monitoring_profile
    with contextlib.closing(sqlite3.connect(profile.home / "store.db")) as connection, connection:
        connection.execute("UPDATE _meta SET value='999' WHERE key='schema_version'")
    with (
        pytest.raises(DesktopError, match="schema_mismatch"),
        read_database(profile, deadline=time.monotonic() + 10),
    ):
        pytest.fail("future schema accepted")
    with profile.locked():
        profile.write_journal(
            profile.new_journal(
                kind="replace",
                previous_state=profile.read_state(),
                previous_running=False,
                remaining=8,
            )
        )
    with (
        pytest.raises(DesktopError, match="recovery_required"),
        read_database(profile, deadline=time.monotonic() + 10),
    ):
        pytest.fail("recovery profile accepted")


def test_sql_deadline_interrupts_and_closes(monitoring_profile):
    from insto.desktop.database import read_database

    with (
        pytest.raises(DesktopError, match="operation_timeout"),
        read_database(monitoring_profile, deadline=time.monotonic() + 0.2) as reader,
    ):
        reader.execute(
            "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL "
            "SELECT x+1 FROM n WHERE x<1000000000) SELECT sum(x) FROM n"
        ).fetchone()
    with read_database(monitoring_profile, deadline=time.monotonic() + 10) as reader:
        assert reader.execute("SELECT 1").fetchone()[0] == 1


def test_write_rollback_and_missing_database_no_creation(monitoring_profile):
    from insto.desktop.database import write_database

    with (
        pytest.raises(RuntimeError, match="injected"),
        write_database(monitoring_profile, deadline=time.monotonic() + 10) as writer,
    ):
        writer.execute("INSERT INTO cli_history(cmd, ts) VALUES ('rollback', 1)")
        raise RuntimeError("injected")
    path = monitoring_profile.home / "store.db"
    with contextlib.closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM cli_history").fetchone()[0] == 0
    for suffix in ("-wal", "-shm"):
        path.with_name(path.name + suffix).unlink(missing_ok=True)
    path.rename(path.with_name("saved.db"))
    with (
        pytest.raises(DesktopError, match="not_configured"),
        write_database(monitoring_profile, deadline=time.monotonic() + 10),
    ):
        pytest.fail("missing database accepted")
    assert not path.exists()
