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


@pytest.mark.parametrize("unsafe", ["symlink", "hardlink", "mode"])
def test_unsafe_database_refused_before_sql(monitoring_profile, unsafe):
    from insto.desktop.database import read_database

    path = monitoring_profile.home / "store.db"
    if unsafe == "symlink":
        saved = path.with_name("saved.db")
        path.rename(saved)
        path.symlink_to(saved)
    elif unsafe == "hardlink":
        os.link(path, path.with_name("second-name.db"))
    else:
        path.chmod(420)
    before = path.lstat()
    with (
        pytest.raises(DesktopError, match="profile_ownership"),
        read_database(monitoring_profile, deadline=time.monotonic() + 10),
    ):
        pytest.fail("unsafe database read")
    assert path.lstat().st_mode == before.st_mode


def test_symlink_sidecar_never_followed(monitoring_profile, tmp_path):
    from insto.desktop.database import read_database

    target = tmp_path / "outside-sentinel"
    target.write_bytes(b"unchanged")
    sidecar = monitoring_profile.home / "store.db-wal"
    assert not sidecar.exists()
    sidecar.symlink_to(target)
    with (
        pytest.raises(DesktopError, match="profile_ownership"),
        read_database(monitoring_profile, deadline=time.monotonic() + 10),
    ):
        pytest.fail("unsafe sidecar read")
    assert target.read_bytes() == b"unchanged"


def test_busy_has_static_code_and_one_second_budget(monitoring_profile):
    from insto.desktop.database import read_database

    path = monitoring_profile.home / "store.db"
    writer = sqlite3.connect(path, isolation_level=None)
    try:
        writer.execute("PRAGMA journal_mode=DELETE")
        writer.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        with (
            pytest.raises(DesktopError, match="profile_busy"),
            read_database(monitoring_profile, deadline=started + 10),
        ):
            pytest.fail("exclusive database accepted")
        assert time.monotonic() - started < 1.6
    finally:
        writer.rollback()
        writer.close()


def test_busy_profile_lease_and_expired_mutation_write_nothing(monitoring_profile):
    from insto.desktop.database import write_database

    with (
        monitoring_profile.locked(),
        pytest.raises(DesktopError, match="profile_busy"),
        write_database(monitoring_profile, deadline=time.monotonic() + 10),
    ):
        pytest.fail("second lease acquired")
    with (
        pytest.raises(DesktopError, match="operation_timeout"),
        write_database(monitoring_profile, deadline=time.monotonic() - 1),
    ):
        pytest.fail("expired mutation entered")
    with contextlib.closing(sqlite3.connect(monitoring_profile.home / "store.db")) as connection:
        assert connection.execute("SELECT COUNT(*) FROM watches").fetchone()[0] == 0


# Review gate G10: every refusal branch of the shared accessor is pinned to its
# exact public code below; these are the codes the GUI turns into recovery hints.
@pytest.mark.parametrize(
    "arrange,code",
    [
        ("backup_only", "recovery_required"),
        ("missing_config", "recovery_required"),
        ("orphan_wal", "schema_mismatch"),
        ("rollback_journal", "schema_mismatch"),
        ("not_a_database", "storage_error"),
    ],
)
def test_profile_file_states_map_to_exact_codes(monitoring_profile, arrange, code):
    from insto.desktop.database import read_database

    profile = monitoring_profile
    path = profile.home / "store.db"
    if arrange == "backup_only":
        with profile.locked():
            profile.write_backup(b"previous = true\n")
    elif arrange == "missing_config":
        profile.config.unlink()
    elif arrange == "orphan_wal":
        path.rename(path.with_name("saved.db"))
        os.close(os.open(path.with_name("store.db-wal"), os.O_WRONLY | os.O_CREAT, 0o600))
    elif arrange == "rollback_journal":
        os.close(os.open(path.with_name("store.db-journal"), os.O_WRONLY | os.O_CREAT, 0o600))
    else:
        path.write_bytes(b"not a database\n" * 64)
    with (
        pytest.raises(DesktopError, match=code),
        read_database(profile, deadline=time.monotonic() + 10),
    ):
        pytest.fail(f"{arrange} accepted")


@pytest.mark.parametrize(
    "statement", ["ALTER TABLE cli_history DROP COLUMN target", "DROP TABLE watches"]
)
def test_missing_schema_pieces_are_schema_mismatch(monitoring_profile, statement):
    from insto.desktop.database import read_database

    with contextlib.closing(sqlite3.connect(monitoring_profile.home / "store.db")) as connection:
        connection.execute(statement)
        connection.commit()
    with (
        pytest.raises(DesktopError, match="schema_mismatch"),
        read_database(monitoring_profile, deadline=time.monotonic() + 10),
    ):
        pytest.fail("incomplete schema accepted")


def test_profile_files_changing_during_inspection_are_busy(monitoring_profile, monkeypatch):
    from insto.desktop import database

    original = database.parse_profile_config

    def parse_then_change_state(profile, payload):
        config = original(profile, payload)
        with profile.locked():
            profile.write_state(profile.new_state(remaining=1, desired="stopped"))
        return config

    monkeypatch.setattr(database, "parse_profile_config", parse_then_change_state)
    with (
        pytest.raises(DesktopError, match="profile_busy"),
        database.read_database(monitoring_profile, deadline=time.monotonic() + 10),
    ):
        pytest.fail("state changed during inspection was accepted")


def test_database_replaced_after_open_is_refused(monitoring_profile, monkeypatch):
    import shutil

    from insto.desktop import database

    path = monitoring_profile.home / "store.db"
    original = database._files
    identities = []

    def swap_after_first_look(target):
        identity = original(target)
        identities.append(identity)
        if len(identities) == 1:
            replacement = path.with_name("replacement.db")
            shutil.copy2(path, replacement)
            os.replace(replacement, path)
        return identity

    monkeypatch.setattr(database, "_files", swap_after_first_look)
    with (
        pytest.raises(DesktopError, match="profile_ownership"),
        database.read_database(monitoring_profile, deadline=time.monotonic() + 10),
    ):
        pytest.fail("replaced database accepted")
    assert len(identities) == 2 and identities[0] != identities[1]


def _write_with_expiry(profile, monkeypatch, *, expire_call):
    """Run one mutation, expiring the deadline on the N-th check (0 = never)."""
    from insto.desktop import database

    calls = 0
    original = database.check_deadline

    def counting(deadline):
        nonlocal calls
        calls += 1
        if calls == expire_call:
            raise DesktopError("operation_timeout")
        original(deadline)

    monkeypatch.setattr(database, "check_deadline", counting)
    with database.write_database(profile, deadline=time.monotonic() + 10) as writer:
        writer.execute("INSERT INTO cli_history(cmd, ts) VALUES ('deadline', 1)")
    return calls


def _history_rows(profile):
    with contextlib.closing(sqlite3.connect(profile.home / "store.db")) as connection:
        return connection.execute("SELECT COUNT(*) FROM cli_history").fetchone()[0]


def test_deadline_before_commit_rolls_back_and_after_commit_is_uncertain(
    monitoring_profile, monkeypatch
):
    # The last deadline check of a mutation runs after COMMIT: expiring there keeps
    # the committed row AND reports operation_timeout, which is the documented
    # "inspect state before retrying" contract. One check earlier is pre-COMMIT.
    total = _write_with_expiry(monitoring_profile, monkeypatch, expire_call=0)
    assert total >= 2 and _history_rows(monitoring_profile) == 1
    with pytest.raises(DesktopError, match="operation_timeout"):
        _write_with_expiry(monitoring_profile, monkeypatch, expire_call=total - 1)
    assert _history_rows(monitoring_profile) == 1
    with pytest.raises(DesktopError, match="operation_timeout"):
        _write_with_expiry(monitoring_profile, monkeypatch, expire_call=total)
    assert _history_rows(monitoring_profile) == 2
    with contextlib.closing(sqlite3.connect(monitoring_profile.home / "store.db")) as writer:
        assert writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone() == (0, 0, 0)
