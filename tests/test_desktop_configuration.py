import os
import sqlite3
from pathlib import Path

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


def cli_home(tmp_path, toml: bytes) -> Path:
    home = tmp_path / "cli-home"
    home.mkdir(mode=0o700)
    (home / "config.toml").write_bytes(toml)
    (home / "config.toml").chmod(0o600)
    return home


def test_adopted_config_honours_cli_keys_and_registers_the_secret(tmp_path):
    from insto._redact import redact_secrets
    from insto.desktop.configuration import parse_profile_config

    home = cli_home(
        tmp_path,
        b'backend = "hikerapi"\ndb_path = "history.db"\n[hikerapi]\n'
        b'token = "offline-cli-secret"\nproxy = "http://127.0.0.1:8118"\n',
    )
    profile = Profile(tmp_path / "desktop", home=home)
    config = parse_profile_config(profile, profile.config.read_bytes())
    assert config.backend == "hikerapi"
    assert config.hiker_token == "offline-cli-secret"
    assert config.hiker_proxy == "http://127.0.0.1:8118"
    # A relative CLI path resolves against the home (the service's WorkingDirectory),
    # never against the bridge's own working directory.
    assert config.db_path == home / "history.db" and config.db_path.is_absolute()
    assert config.output_dir == home / "output"
    assert "offline-cli-secret" not in redact_secrets("token offline-cli-secret leaked")


@pytest.mark.parametrize(
    ("toml", "code"),
    [
        (
            b'backend = "aiograpi"\n[aiograpi]\nusername = "u"\npassword = "p"\n',
            "home_backend_unsupported",
        ),
        (b'backend = "aiograpi"\n', "home_backend_unsupported"),
        (b'backend = "fake"\n', "home_backend_unsupported"),
        (b"backend = \n", "home_invalid"),
        (b'backend = "hikerapi"\n', "home_invalid"),
        (b'backend = "hikerapi"\n[hikerapi]\ntoken = "abc"\n', "home_invalid"),
        (b'db_path = 5\n[hikerapi]\ntoken = "offline-cli-secret"\n', "home_invalid"),
    ],
)
def test_adopted_config_rejections(tmp_path, toml, code):
    from insto.desktop.configuration import parse_profile_config

    home = cli_home(tmp_path, toml)
    profile = Profile(tmp_path / "desktop", home=home)
    with pytest.raises(DesktopError) as info:
        parse_profile_config(profile, profile.config.read_bytes())
    assert info.value.code == code


def test_adopted_config_expands_home_and_defaults_paths_to_the_home(tmp_path, monkeypatch):
    from insto.desktop.configuration import parse_profile_config

    monkeypatch.setenv("HOME", str(tmp_path))
    home = cli_home(
        tmp_path, b'db_path = "~/x/store.db"\n[hikerapi]\ntoken = "offline-cli-secret"\n'
    )
    (tmp_path / "x").mkdir(mode=0o700)  # an external database parent must exist (C4)
    profile = Profile(tmp_path / "desktop", home=home)
    config = parse_profile_config(profile, profile.config.read_bytes())
    assert config.db_path == tmp_path / "x" / "store.db"
    default = parse_profile_config(profile, b'[hikerapi]\ntoken = "offline-cli-secret"\n')
    assert default.backend == "hikerapi"
    assert default.db_path == home / "store.db"
    assert default.output_dir == home / "output"
    assert default.cli_history_path == home / "cli_history"
    assert default.aiograpi_session_path == home / "aiograpi.session.json"


def test_adopted_config_accepts_the_legacy_hiker_table(tmp_path):
    from insto.desktop.configuration import parse_profile_config

    home = cli_home(tmp_path, b'backend = "hiker"\n[hiker]\ntoken = "offline-legacy-secret"\n')
    profile = Profile(tmp_path / "desktop", home=home)
    config = parse_profile_config(profile, profile.config.read_bytes())
    assert config.backend == "hikerapi"
    assert config.hiker_token == "offline-legacy-secret"


def test_own_profile_config_stays_strict(tmp_path):
    from insto.desktop.configuration import config_bytes, parse_profile_config

    profile = Profile(tmp_path / "desktop")
    config = parse_profile_config(profile, config_bytes(profile, "offline-desktop-token"))
    assert config.hiker_token == "offline-desktop-token"
    with pytest.raises(DesktopError):
        parse_profile_config(
            profile, b'backend = "hikerapi"\n[hikerapi]\ntoken = "offline-desktop-token"\n'
        )


def test_adopted_config_bytes_replaces_only_the_token(tmp_path):
    import tomllib

    from insto.desktop.configuration import adopted_config_bytes

    original = (
        b'backend = "hikerapi"\ntheme = "dark"\n[hikerapi]\n'
        b'token = "offline-old-secret"\nproxy = "http://127.0.0.1:8118"\n'
    )
    updated = tomllib.loads(adopted_config_bytes(original, "offline-new-secret").decode())
    assert updated["hikerapi"] == {"token": "offline-new-secret", "proxy": "http://127.0.0.1:8118"}
    assert updated["theme"] == "dark" and updated["backend"] == "hikerapi"
    legacy_toml = b'[hiker]\ntoken = "offline-old-secret"\n'
    legacy = tomllib.loads(adopted_config_bytes(legacy_toml, "offline-new-secret").decode())
    assert legacy == {"hiker": {"token": "offline-new-secret"}}
    fresh_toml = b'backend = "hikerapi"\n'
    fresh = tomllib.loads(adopted_config_bytes(fresh_toml, "offline-new-secret").decode())
    assert fresh["hikerapi"]["token"] == "offline-new-secret"


@pytest.mark.parametrize(
    "toml",
    [b'hikerapi = "x"\n', b'hiker = 1\n[hikerapi]\ntoken = "offline-old-secret"\n'],
)
def test_adopted_config_bytes_refuses_a_non_table_credential_section(toml):
    from insto.desktop.configuration import adopted_config_bytes

    with pytest.raises(DesktopError) as info:
        adopted_config_bytes(toml, "offline-new-secret")
    assert info.value.code == "home_invalid"


@pytest.mark.parametrize("parent", ["private", "world_writable", "group_readable", "missing"])
def test_external_database_needs_an_owned_private_parent_under_trusted_ancestors(tmp_path, parent):
    """C4: an absolute db_path outside the home is honoured only from a directory that
    passes the same ownership rule as the home itself; every desktop open applies it."""
    from insto.desktop.configuration import parse_profile_config

    external = tmp_path / "elsewhere"
    if parent != "missing":
        external.mkdir(mode=0o700)
        external.chmod({"private": 0o700, "world_writable": 0o777, "group_readable": 0o750}[parent])
    home = cli_home(
        tmp_path,
        b'db_path = "%s"\n[hikerapi]\ntoken = "offline-cli-secret"\n'
        % str(external / "s.db").encode(),
    )
    profile = Profile(tmp_path / "desktop", home=home)
    if parent == "private":
        config = parse_profile_config(profile, profile.config.read_bytes())
        assert config.db_path == external / "s.db"
        return
    with pytest.raises(DesktopError) as info:
        parse_profile_config(profile, profile.config.read_bytes())
    assert info.value.code == "home_invalid"


def test_external_database_under_an_untrusted_ancestor_is_refused(tmp_path):
    from insto.desktop.configuration import parse_profile_config

    shared = tmp_path / "shared"
    shared.mkdir(mode=0o777)
    shared.chmod(0o777)
    external = shared / "private"
    external.mkdir(mode=0o700)
    home = cli_home(
        tmp_path,
        b'db_path = "%s"\n[hikerapi]\ntoken = "offline-cli-secret"\n'
        % str(external / "s.db").encode(),
    )
    profile = Profile(tmp_path / "desktop", home=home)
    with pytest.raises(DesktopError, match="home_invalid"):
        parse_profile_config(profile, profile.config.read_bytes())


def test_database_inside_the_home_needs_no_extra_ancestry(tmp_path):
    from insto.desktop.configuration import parse_profile_config

    home = cli_home(
        tmp_path, b'db_path = "data/store.db"\n[hikerapi]\ntoken = "offline-cli-secret"\n'
    )
    profile = Profile(tmp_path / "desktop", home=home)
    assert (
        parse_profile_config(profile, profile.config.read_bytes()).db_path
        == home / "data" / "store.db"
    )


def test_a_parent_escaping_relative_database_is_judged_where_it_lands(tmp_path):
    """C4: `..` must not buy the home's exemption. The untrusted ancestor that refuses
    the absolute path refuses the relative one that lands in the same directory."""
    from insto.desktop.configuration import parse_profile_config

    shared = tmp_path / "shared"
    shared.mkdir(mode=0o777)
    shared.chmod(0o777)
    (shared / "private").mkdir(mode=0o700)
    home = cli_home(
        tmp_path,
        b'db_path = "../shared/private/s.db"\n[hikerapi]\ntoken = "offline-cli-secret"\n',
    )
    profile = Profile(tmp_path / "desktop", home=home)
    with pytest.raises(DesktopError, match="home_invalid"):
        parse_profile_config(profile, profile.config.read_bytes())


def test_a_relative_database_landing_back_inside_the_home_is_accepted(tmp_path):
    from insto.desktop.configuration import parse_profile_config

    home = cli_home(
        tmp_path, b'db_path = "data/../store.db"\n[hikerapi]\ntoken = "offline-cli-secret"\n'
    )
    (home / "data").mkdir(mode=0o700)
    profile = Profile(tmp_path / "desktop", home=home)
    config = parse_profile_config(profile, profile.config.read_bytes())
    assert Path(os.path.normpath(config.db_path)) == home / "store.db"


@pytest.mark.parametrize("moment", ["before_recheck", "at_link"])
def test_database_publication_keeps_a_concurrently_created_database(tmp_path, monkeypatch, moment):
    """C3: a writer that publishes between the absence check and the link (the CLI) wins;
    its file is kept and validated, never replaced."""
    from insto.desktop import configuration

    target = tmp_path / "store.db"
    original_store = configuration.HistoryStore
    original_link = os.link

    def publish_winner():
        original_store(target).close()
        target.chmod(0o600)

    def store_then_publish(path):
        store = original_store(path)
        publish_winner()
        return store

    def link_after_winner(src, dst, *args, **kwargs):
        publish_winner()
        return original_link(src, dst, *args, **kwargs)

    if moment == "before_recheck":
        monkeypatch.setattr(configuration, "HistoryStore", store_then_publish)
    else:
        monkeypatch.setattr(os, "link", link_after_winner)
    configuration.initialize_database(target)
    winner = target.stat()
    assert winner.st_nlink == 1 and stat_mode(target) == 0o600
    assert configuration.check_database(target)
    assert not any(p.name.startswith(".desktop-db-") for p in tmp_path.iterdir())
    # Nothing of ours replaced it: the inode is the winner's, and a second run is a no-op.
    configuration.initialize_database(target)
    assert target.stat().st_ino == winner.st_ino


def test_database_publication_refuses_an_incompatible_concurrent_winner(tmp_path, monkeypatch):
    from insto.desktop import configuration

    target = tmp_path / "store.db"
    original_link = os.link

    def link_after_incompatible_winner(src, dst, *args, **kwargs):
        with sqlite3.connect(target) as connection:
            connection.execute("CREATE TABLE _meta(key TEXT, value TEXT)")
            connection.execute("INSERT INTO _meta VALUES ('schema_version', '999')")
        target.chmod(0o600)
        return original_link(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "link", link_after_incompatible_winner)
    with pytest.raises(DesktopError, match="schema_mismatch"):
        configuration.initialize_database(target)
    with sqlite3.connect(target) as connection:
        assert connection.execute("SELECT value FROM _meta").fetchall() == [("999",)]


def test_database_publication_repairs_its_own_stranded_stage_link(tmp_path):
    """A death between the link and the stage unlink strands a two-link database that
    every later open refuses; the next initialization drops only our own stage link."""
    from insto.desktop import configuration
    from insto.service.history import HistoryStore

    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    target = home / "store.db"
    stage = tmp_path / ".desktop-db-crashed"  # where initialize_database stages
    stage.mkdir(mode=0o700)
    staged = stage / "store.db"
    os.close(os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    HistoryStore(staged).close()
    os.link(staged, target)  # the process died here: the stage unlink never ran
    other = tmp_path / ".desktop-db-live"
    other.mkdir(mode=0o700)
    foreign = other / "store.db"
    foreign.write_bytes(b"another process is staging here")
    foreign.chmod(0o600)
    with pytest.raises(DesktopError, match="profile_ownership"):
        configuration.check_database(target)

    configuration.initialize_database(target)

    assert configuration.check_database(target) is True
    assert target.stat().st_nlink == 1 and stat_mode(target) == 0o600
    # Only the duplicate link goes; the stage directory and every unrelated stage
    # file (which may belong to a live process) are untouched.
    assert not staged.exists() and stage.is_dir()
    assert foreign.read_bytes() == b"another process is staging here"


def stat_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777
