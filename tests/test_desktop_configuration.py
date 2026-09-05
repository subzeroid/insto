import os
import sqlite3

import pytest

from insto.desktop.errors import DesktopError
from insto.desktop.profile import Profile


def test_explicit_configuration_ignores_environment(tmp_path, monkeypatch):
    from insto.desktop.configuration import config_bytes, parse_config

    profile = Profile(tmp_path / "desktop")
    monkeypatch.setenv("INSTO_HOME", "/foreign")
    monkeypatch.setenv("HIKERAPI_TOKEN", "foreign-secret")
    monkeypatch.setenv("HIKERAPI_PROXY", "https://foreign")
    config = parse_config(profile, config_bytes(profile, "offline-token"))
    assert config.hiker_token == "offline-token"
    assert config.hiker_proxy is None
    assert config.db_path == profile.home / "store.db"
    assert config.output_dir == profile.home / "output"
    assert config.aiograpi_session_path == profile.home / "aiograpi.session.json"
    assert config.cli_history_path == profile.home / "cli_history"


def test_configuration_rejects_redirected_paths(tmp_path):
    from insto.desktop.configuration import config_bytes, parse_config

    profile = Profile(tmp_path / "desktop")
    payload = config_bytes(profile, "offline-token").replace(b"store.db", b"foreign.db")
    with pytest.raises(DesktopError, match="profile_ownership"):
        parse_config(profile, payload)


def test_database_missing_read_has_no_effect(tmp_path):
    from insto.desktop.configuration import check_database

    path = tmp_path / "missing.db"
    assert check_database(path) is False
    assert not path.exists()


@pytest.mark.parametrize("kind", ["symlink", "directory", "public", "old", "forged"])
def test_database_refuses_unsafe_or_incompatible_files(tmp_path, kind):
    from insto.desktop.configuration import check_database

    path = tmp_path / "store.db"
    if kind == "directory":
        path.mkdir()
    elif kind == "symlink":
        path.symlink_to(tmp_path / "missing")
    else:
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE _meta(key TEXT, value TEXT)")
            connection.execute(
                "INSERT INTO _meta VALUES ('schema_version', ?)",
                ("2" if kind == "forged" else "1",),
            )
        path.chmod(0o644 if kind == "public" else 0o600)
    before = path.lstat()
    with pytest.raises(DesktopError):
        check_database(path)
    assert path.lstat().st_mode == before.st_mode


def test_valid_database_check_preserves_bytes_and_permissions(tmp_path):
    from insto.desktop.configuration import check_database, initialize_database

    path = tmp_path / "store.db"
    initialize_database(path)
    before = path.read_bytes()
    assert check_database(path)
    assert path.read_bytes() == before
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_future_schema_in_uncheckpointed_wal_is_rejected_without_source_changes(tmp_path):
    from insto.desktop.configuration import check_database, initialize_database

    path = tmp_path / "store.db"
    initialize_database(path)
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("UPDATE _meta SET value='999' WHERE key='schema_version'")
        connection.commit()
        before = {p.name: (p.read_bytes(), p.stat().st_mode) for p in tmp_path.iterdir()}
        with pytest.raises(DesktopError, match="schema_mismatch"):
            check_database(path)
        assert {p.name: (p.read_bytes(), p.stat().st_mode) for p in tmp_path.iterdir()} == before
    finally:
        connection.close()


def test_large_database_is_allowed(tmp_path):
    from insto.desktop.configuration import check_database, initialize_database

    path = tmp_path / "store.db"
    initialize_database(path)
    with sqlite3.connect(path) as connection:
        connection.execute("INSERT INTO cli_history(cmd, ts) VALUES (?, 1)", ("x" * 100000,))
    assert check_database(path)


def test_interrupted_database_initialization_leaves_final_database_missing(tmp_path, monkeypatch):
    from insto.desktop import configuration

    path = tmp_path / "store.db"

    def fail(path):
        path.touch(mode=0o600)
        raise RuntimeError("interrupted")

    monkeypatch.setattr(configuration, "HistoryStore", fail)
    with pytest.raises(RuntimeError):
        configuration.initialize_database(path)
    assert not path.exists()


@pytest.mark.parametrize("leaf", ["output", "aiograpi.session.json", "cli_history"])
def test_fixed_auxiliary_paths_cannot_redirect_outside_profile(tmp_path, leaf):
    from insto.desktop.configuration import config_bytes, parse_config

    profile = Profile(tmp_path / "desktop")
    with profile.locked(initialize=True):
        (profile.home / leaf).symlink_to(tmp_path / "elsewhere")
        with pytest.raises(DesktopError, match="profile_ownership"):
            parse_config(profile, config_bytes(profile, "offline-token"))


def test_missing_database_never_adopts_an_orphan_wal(tmp_path):
    from insto.desktop.configuration import check_database, initialize_database

    origin = tmp_path / "origin.db"
    target = tmp_path / "target.db"
    initialize_database(origin)
    connection = sqlite3.connect(origin)
    try:
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute(
            "INSERT INTO watches VALUES "
            "('orphan', 'foreign-generation', 900, 12, NULL, 0, 'paused')"
        )
        connection.commit()
        orphan = tmp_path / "target.db-wal"
        orphan.write_bytes((tmp_path / "origin.db-wal").read_bytes())
        orphan.chmod(0o600)
        before = orphan.read_bytes()
        for action in (check_database, initialize_database):
            with pytest.raises(DesktopError, match="schema_mismatch"):
                action(target)
            assert not target.exists()
            assert orphan.read_bytes() == before
            assert orphan.stat().st_mode & 0o777 == 0o600
    finally:
        connection.close()


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
@pytest.mark.parametrize("kind", ["ordinary", "symlink", "directory"])
def test_missing_database_refuses_every_orphan_sidecar(tmp_path, suffix, kind):
    from insto.desktop.configuration import check_database, initialize_database

    target = tmp_path / "target.db"
    sidecar = tmp_path / ("target.db" + suffix)
    if kind == "ordinary":
        sidecar.write_bytes(b"orphan sentinel")
        sidecar.chmod(0o600)
    elif kind == "symlink":
        sidecar.symlink_to(tmp_path / "missing")
    else:
        sidecar.mkdir(mode=0o700)
    before = sidecar.lstat()
    for action in (check_database, initialize_database):
        with pytest.raises(DesktopError, match="schema_mismatch"):
            action(target)
        assert not target.exists()
        after = sidecar.lstat()
        assert (after.st_ino, after.st_mode, after.st_size) == (
            before.st_ino,
            before.st_mode,
            before.st_size,
        )


def test_database_publication_rechecks_sidecars_after_staging(tmp_path, monkeypatch):
    from insto.desktop import configuration

    target = tmp_path / "target.db"
    sidecar = tmp_path / "target.db-wal"
    original = configuration.HistoryStore

    def initialize_and_publish_orphan(path):
        store = original(path)
        sidecar.write_bytes(b"appeared during staging")
        sidecar.chmod(0o600)
        return store

    monkeypatch.setattr(configuration, "HistoryStore", initialize_and_publish_orphan)
    with pytest.raises(DesktopError, match="schema_mismatch"):
        configuration.initialize_database(target)
    assert not target.exists()
    assert sidecar.read_bytes() == b"appeared during staging"
