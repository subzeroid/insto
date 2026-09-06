import contextlib
import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

from insto.config import Config
from insto.desktop.errors import DesktopError
from insto.desktop.profile import Profile
from insto.exceptions import BackendError
from insto.service import watch_service

TOKEN = "offline-home-token"
RUNNING = {
    "registration": "owned",
    "installation": "installed",
    "interpreter": "other",
    "interpreter_exists": True,
    "loaded": True,
    "process": "running",
    "settings": None,
}
ABSENT = {
    "registration": "none",
    "installation": None,
    "interpreter": None,
    "interpreter_exists": None,
    "loaded": False,
    "process": "stopped",
    "settings": None,
}


def write_private(path: Path, payload: bytes) -> None:
    path.write_bytes(payload)
    path.chmod(0o600)


def cli_home(
    parent: Path,
    name: str = "cli-home",
    *,
    backend: str = "hikerapi",
    token: str = TOKEN,
    config: bool = True,
    database: str = "ok",
    extra: str = "",
) -> Path:
    from insto.desktop.configuration import initialize_database

    home = parent / name
    home.mkdir(mode=0o700)
    if config:
        if backend == "hikerapi":
            payload = f'backend = "hikerapi"\n{extra}[hikerapi]\ntoken = "{token}"\n'
        elif backend == "aiograpi":
            payload = (
                'backend = "aiograpi"\n[aiograpi]\nusername = "alice"\npassword = "offline-pw"\n'
            )
        elif backend == "aiograpi_no_credentials":
            payload = 'backend = "aiograpi"\n'
        else:
            payload = 'backend = "fake"\n'
        write_private(home / "config.toml", payload.encode())
    if database in {"ok", "schema_mismatch"}:
        initialize_database(home / "store.db")
    if database == "schema_mismatch":
        with sqlite3.connect(home / "store.db") as connection:
            connection.execute("UPDATE _meta SET value='999' WHERE key='schema_version'")
    return home


def register(home: Path, *, python: str | None = None) -> tuple[bytes, bytes]:
    """Write an owned registration for `home` (manifest + plist), like the CLI's install."""
    paths = watch_service.service_paths(home)
    for directory in (home / "services", paths.directory, paths.log_dir):
        directory.mkdir(mode=0o700, exist_ok=True)
    config = Config(backend="hikerapi", hiker_token=TOKEN, db_path=home / "store.db")
    manifest, plist = watch_service._desired(paths, config, None)
    if python is not None:
        import plistlib

        value = dict(json.loads(manifest), python=python)
        manifest = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
        plist = plistlib.dumps(
            watch_service._plist_document(paths, value, dont_write_bytecode=False),
            fmt=plistlib.FMT_XML,
            sort_keys=True,
        )
    watch_service._atomic_write(paths.manifest, manifest)
    watch_service._atomic_write(paths.plist, plist)
    return manifest, plist


@pytest.fixture
def launch_agents(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / "Library" / "LaunchAgents").mkdir(parents=True)


@pytest.fixture
def launchd(monkeypatch):
    """Per-home fake launchd: facts, leases and events keyed by the resolved home path."""
    from insto.desktop import home

    class Launchd:
        def __init__(self):
            self.facts: dict[Path, dict] = {}
            self.events: list[tuple[str, Path]] = []

        def state(self, home_path):
            return dict(self.facts.get(home_path, ABSENT))

    launchd = Launchd()

    async def registration_facts(paths, *, deadline, expected=None):
        return launchd.state(paths.home)

    class Lease:
        def __init__(self, paths, config, deadline, *, artifacts=None):
            self._paths = paths
            self._config = config
            self.deadline = deadline
            self._manifest, self._plist = artifacts or watch_service._desired(paths, config, None)

        def _installation(self):
            for path, desired in (
                (self._paths.manifest, self._manifest),
                (self._paths.plist, self._plist),
            ):
                if os.path.lexists(path) and not watch_service._existing_matches(path, desired):
                    raise BackendError("watch service artifacts do not match the owned runtime")

        async def inspect_owned(self):
            self._installation()
            running = launchd.state(self._paths.home)["process"] == "running"
            return {
                "installation": "installed",
                "registration": "loaded" if running else "unloaded",
                "process": {"state": "running" if running else None, "pid": 3 if running else None},
                "executor": {"state": "busy" if running else "idle", "pid": 3 if running else None},
            }

        async def ensure_stopped(self):
            self._installation()
            launchd.events.append(("stop", self._paths.home))
            launchd.facts[self._paths.home] = dict(
                launchd.state(self._paths.home), loaded=False, process="stopped"
            )
            return await self.inspect_owned()

        async def ensure_running(self):
            launchd.events.append(("start", self._paths.home))
            raise AssertionError("selection never starts a service")

    @contextlib.contextmanager
    def managed(**kwargs):
        yield Lease(
            watch_service.service_paths(kwargs["home"]), kwargs["config"], kwargs["deadline"]
        )

    monkeypatch.setattr(home, "registration_facts", registration_facts)
    monkeypatch.setattr(home, "ManagedService", Lease)
    monkeypatch.setattr(home, "managed_service", managed)
    return launchd


@pytest.fixture
def own(tmp_path, launch_agents, launchd):
    """The desktop's own profile: configured, registered for this interpreter, running."""
    from insto.desktop import operations
    from insto.desktop.configuration import initialize_database

    profile = Profile(tmp_path / "desktop")
    with profile.locked(initialize=True):
        profile.write_config(operations.config_bytes(profile, "offline-own-secret"))
        profile.write_state(profile.new_state(remaining=8, desired="running"))
        initialize_database(profile.home / "store.db")
    register(profile.home)
    launchd.facts[profile.home] = dict(RUNNING, interpreter="current")
    return profile


async def test_inspect_reports_an_adoptable_cli_home_without_touching_it(
    tmp_path, launch_agents, launchd
):
    from insto.desktop import home

    path = cli_home(tmp_path)
    launchd.facts[path] = RUNNING
    before = {p.name: p.stat().st_mtime_ns for p in path.iterdir()}
    report = await home.inspect(path, deadline=time.monotonic() + 10)
    assert report == {
        "path": str(path),
        "exists": True,
        "private": True,
        "config": "ok",
        "backend": "hikerapi",
        "database": "ok",
        "registration": "owned",
        "interpreter": "other",
        "loaded": True,
        "process": "running",
        "adoptable": True,
        "reason": None,
    }
    assert TOKEN not in json.dumps(report)
    assert {p.name: p.stat().st_mtime_ns for p in path.iterdir()} == before


@pytest.mark.parametrize(
    "case, expected",
    [
        ("missing", {"exists": False, "adoptable": False, "reason": "home_invalid"}),
        (
            "world_readable",
            {
                "private": False,
                "config": "invalid",
                "database": "unreadable",
                "registration": "unknown",
                "adoptable": False,
                "reason": "home_invalid",
            },
        ),
        ("untrusted_parent", {"private": False, "adoptable": False, "reason": "home_invalid"}),
        ("symlink", {"exists": True, "private": False, "adoptable": False}),
        ("no_config", {"config": "missing", "backend": None, "database": "ok", "adoptable": False}),
        ("insecure_config", {"config": "invalid", "adoptable": False, "reason": "home_invalid"}),
        ("readonly_config", {"config": "invalid", "adoptable": False, "reason": "home_invalid"}),
        ("hardlinked_config", {"config": "invalid", "adoptable": False, "reason": "home_invalid"}),
        ("numeric_path", {"config": "invalid", "adoptable": False, "reason": "home_invalid"}),
        ("short_token", {"config": "invalid", "backend": "hikerapi", "adoptable": False}),
        ("aiograpi", {"config": "ok", "backend": "aiograpi", "reason": "home_backend_unsupported"}),
        (
            "aiograpi_no_credentials",
            {"config": "ok", "backend": "aiograpi", "reason": "home_backend_unsupported"},
        ),
        ("fake", {"config": "ok", "backend": "fake", "reason": "home_backend_unsupported"}),
        ("schema_mismatch", {"database": "schema_mismatch", "reason": "schema_mismatch"}),
        ("no_database", {"database": "missing", "adoptable": True, "reason": None}),
        ("wal", {"database": "ok", "adoptable": True, "reason": None}),
    ],
)
async def test_inspection_matrix(tmp_path, launch_agents, launchd, case, expected):
    from insto.desktop import home

    with contextlib.ExitStack() as keep:
        if case == "missing":
            path = tmp_path / "absent"
        elif case == "symlink":
            path = tmp_path / "link"
            path.symlink_to(cli_home(tmp_path, "real"))
        elif case == "untrusted_parent":
            parent = tmp_path / "shared"
            parent.mkdir()
            parent.chmod(0o777)  # mkdir(mode=) is umask-masked; chmod makes it world-writable
            path = cli_home(parent)
        else:
            kwargs = {}
            if case == "no_config":
                kwargs["config"] = False
            if case == "short_token":
                kwargs["token"] = "abc"
            if case in {"aiograpi", "aiograpi_no_credentials", "fake"}:
                kwargs["backend"] = case
            if case == "schema_mismatch":
                kwargs["database"] = "schema_mismatch"
            if case == "no_database":
                kwargs["database"] = "missing"
            if case == "numeric_path":
                kwargs["extra"] = "db_path = 5\n"
            path = cli_home(tmp_path, **kwargs)
            if case == "world_readable":
                path.chmod(0o755)
            if case == "insecure_config":
                (path / "config.toml").chmod(0o644)
            if case == "readonly_config":
                (path / "config.toml").chmod(0o400)
            if case == "hardlinked_config":
                os.link(path / "config.toml", tmp_path / "config-link.toml")
            if case == "wal":
                connection = keep.enter_context(
                    contextlib.closing(sqlite3.connect(path / "store.db"))
                )
                connection.execute("PRAGMA wal_autocheckpoint=0")
                connection.execute(
                    "INSERT INTO cli_history (cmd, target, ts) VALUES ('watch', 'alice', 1)"
                )
                connection.commit()
                assert (path / "store.db-wal").exists()
        report = await home.inspect(path, deadline=time.monotonic() + 10)
    assert {key: report[key] for key in expected} == expected
    assert report["adoptable"] is (report["reason"] is None)


@pytest.mark.parametrize("code", ["storage_error", "profile_busy"])
async def test_unreadable_database_is_reported_and_blocks_adoption(
    tmp_path, launch_agents, launchd, monkeypatch, code
):
    from insto.desktop import home

    path = cli_home(tmp_path)

    def failing(db_path, *, deadline=None):
        raise DesktopError(code)

    monkeypatch.setattr(home, "check_database", failing)
    report = await home.inspect(path, deadline=time.monotonic() + 10)
    assert report["database"] == "unreadable" and report["reason"] == "storage_error"
    assert report["adoptable"] is False


async def test_inspect_propagates_timeouts(tmp_path, launch_agents, launchd, monkeypatch):
    from insto.desktop import home

    path = cli_home(tmp_path)

    def expired(db_path, *, deadline=None):
        raise DesktopError("operation_timeout")

    monkeypatch.setattr(home, "check_database", expired)
    with pytest.raises(DesktopError, match="operation_timeout"):
        await home.inspect(path, deadline=time.monotonic() + 10)
    monkeypatch.undo()
    with pytest.raises(DesktopError, match="operation_timeout"):
        await home.inspect(path, deadline=time.monotonic() - 1)


async def test_select_stops_the_own_service_and_adopts(tmp_path, own, launchd):
    from insto.desktop import home

    path = cli_home(tmp_path)
    launchd.facts[path] = RUNNING
    register(path, python=str(tmp_path / "old" / "python3"))
    result = await home.select(own, path)
    assert launchd.events == [("stop", own.home)]
    assert launchd.state(own.home)["process"] == "stopped"
    assert own.read_state()["desired_service"] == "stopped"
    assert json.loads(own.binding.read_bytes())["home"] == str(path)
    state = json.loads((path / "desktop-state.json").read_bytes())
    assert state["desired_service"] == "running"
    assert state["quota_remaining"] is None and state["quota_checked_at"] is None
    assert result["configured"] and result["status"] == "running"
    assert result["service_running"] is True and result["quota_remaining"] is None
    assert result["desired_service"] == "running"
    assert (path / "services" / "watch" / "logs").is_dir()
    assert sorted(p.name for p in path.iterdir()) == [
        ".desktop.lock",
        "config.toml",
        "desktop-state.json",
        "services",
        "store.db",
    ]


async def test_select_reports_a_foreign_registration_as_service_error(tmp_path, own, launchd):
    """Facts say running, but the on-disk plist is not the owned form: the DTO is honest."""
    from insto.desktop import home

    path = cli_home(tmp_path)
    launchd.facts[path] = RUNNING
    register(path)
    paths = watch_service.service_paths(path)
    watch_service.replace_registration(
        paths, watch_service.read_registration(paths), (b'{"foreign":1}\n', b"<plist/>")
    )
    result = await home.select(own, path)
    assert result["configured"] and result["status"] == "service_error"
    assert result["service_running"] is False


async def test_select_same_home_is_a_noop(tmp_path, own, launchd, monkeypatch):
    from insto.desktop import home

    path = cli_home(tmp_path)
    await home.select(own, path)
    launchd.events.clear()
    binding = own.binding.read_bytes()
    state = (path / "desktop-state.json").read_bytes()

    async def no_facts(paths, *, deadline, expected=None):
        raise AssertionError("a no-op selection reads no registration facts")

    monkeypatch.setattr(home, "registration_facts", no_facts)
    result = await home.select(own, path)
    assert launchd.events == []
    assert own.binding.read_bytes() == binding
    assert (path / "desktop-state.json").read_bytes() == state
    assert result["configured"] and result["desired_service"] == "stopped"


async def test_select_keeps_an_existing_adopted_state_and_creates_a_missing_database(
    tmp_path, own, launchd
):
    from insto.desktop import home
    from insto.desktop.configuration import check_database

    path = cli_home(tmp_path, database="missing")
    adopted = Profile(own.root, home=path)
    with own.locked(), adopted.shared_lease(own):
        adopted.write_state(adopted.new_state(remaining=None, desired="stopped"))
    kept = (path / "desktop-state.json").read_bytes()
    await home.select(own, path)
    assert (path / "desktop-state.json").read_bytes() == kept
    assert check_database(path / "store.db") is True
    assert not any(p.name.startswith(".desktop-db-") for p in tmp_path.iterdir())
    assert not any(p.name.startswith(".desktop-db-") for p in path.iterdir())


async def test_select_back_to_the_own_profile_never_starts_it(tmp_path, own, launchd):
    from insto.desktop import home

    path = cli_home(tmp_path)
    launchd.facts[path] = RUNNING
    await home.select(own, path)
    launchd.events.clear()
    result = await home.select(own, None)
    assert launchd.events == []
    assert not own.binding.exists()
    assert own.read_state()["desired_service"] == "stopped"
    assert result["configured"] and result["status"] == "stopped"
    assert (path / "desktop-state.json").exists()


async def test_switching_between_adopted_homes_never_touches_their_services(tmp_path, own, launchd):
    from insto.desktop import home

    first = cli_home(tmp_path, "first")
    second = cli_home(tmp_path, "second")
    launchd.facts[first] = RUNNING
    await home.select(own, first)
    launchd.events.clear()
    await home.select(own, second)
    assert launchd.events == []
    assert json.loads(own.binding.read_bytes())["home"] == str(second)
    assert json.loads((first / "desktop-state.json").read_bytes())["desired_service"] == "running"


@pytest.mark.parametrize(
    "case, code",
    [
        ("missing", "home_invalid"),
        ("fake", "home_backend_unsupported"),
        ("schema_mismatch", "schema_mismatch"),
        ("own_profile", "home_invalid"),
        ("target_pending_recovery", "recovery_required"),
        ("target_corrupt_state", "profile_ownership"),
    ],
)
async def test_select_refuses_before_any_change(tmp_path, own, launchd, case, code):
    from insto.desktop import home

    if case == "missing":
        path = tmp_path / "absent"
    elif case == "own_profile":
        path = own.home
    else:
        path = cli_home(
            tmp_path,
            backend="fake" if case == "fake" else "hikerapi",
            database="schema_mismatch" if case == "schema_mismatch" else "ok",
        )
        if case == "target_pending_recovery":
            target = Profile(own.root, home=path)
            with own.locked(), target.shared_lease(own):
                target.write_state(target.new_state(remaining=None, desired="stopped"))
                target.write_journal(
                    target.new_journal(
                        kind="migrate",
                        previous_state=target.read_state(),
                        previous_running=False,
                        remaining=None,
                    )
                )
        if case == "target_corrupt_state":
            write_private(path / "desktop-state.json", b"{not json")
    with pytest.raises(DesktopError) as info:
        await home.select(own, path)
    assert info.value.code == code
    assert launchd.events == [] and launchd.state(own.home)["process"] == "running"
    assert not own.binding.exists()


async def test_select_finishes_terminal_journals(tmp_path, own, launchd):
    """A death after committed/rolled_back but before cleanup leaves a terminal journal;
    selection completes it for the current profile and for the target."""
    from insto.desktop import home

    with own.locked():
        journal = own.new_journal(
            kind="migrate", previous_state=own.read_state(), previous_running=False, remaining=None
        )
        journal["phase"] = "committed"
        own.write_journal(journal)
    path = cli_home(tmp_path)
    target = Profile(own.root, home=path)
    with own.locked(), target.shared_lease(own):
        target.write_state(target.new_state(remaining=None, desired="stopped"))
        journal = target.new_journal(
            kind="migrate",
            previous_state=target.read_state(),
            previous_running=False,
            remaining=None,
        )
        journal["phase"] = "rolled_back"
        target.write_journal(journal)
    result = await home.select(own, path)
    assert result["configured"] and own.read_journal() is None
    assert target.read_journal() is None


async def test_select_refuses_pending_recovery_of_the_current_profile(tmp_path, own):
    from insto.desktop import home

    with own.locked():
        own.write_backup(own.read_config())
    with pytest.raises(DesktopError) as info:
        await home.select(own, cli_home(tmp_path))
    assert info.value.code == "recovery_required"


async def test_unknown_own_registration_refuses_adoption(tmp_path, own, launchd):
    from insto.desktop import home

    launchd.facts[own.home] = dict(RUNNING, registration="unknown", interpreter=None)
    with pytest.raises(DesktopError) as info:
        await home.select(own, cli_home(tmp_path))
    assert info.value.code == "service_ownership_unknown"
    assert not own.binding.exists()


async def test_broken_binding_counts_as_the_own_profile(tmp_path, own, launchd):
    from insto.desktop import home

    write_private(own.binding, b"{not json")
    result = await home.select(own, None)
    assert not own.binding.exists() and result["configured"]
    write_private(own.binding, b"{not json")
    path = cli_home(tmp_path)
    await home.select(own, path)
    assert launchd.events == [("stop", own.home)]  # the own service is still stopped first
    assert json.loads(own.binding.read_bytes())["home"] == str(path)


async def test_deleted_adopted_home_can_return_to_the_own_profile(
    tmp_path, own, launchd, monkeypatch
):
    import shutil

    from insto.desktop import home

    path = cli_home(tmp_path)
    await home.select(own, path)
    shutil.rmtree(path)
    monkeypatch.setenv("INSTO_DESKTOP_ROOT", str(own.root))
    with pytest.raises(DesktopError, match="home_invalid"):
        Profile.from_environment().read_state()  # the binding still names the missing home
    result = await home.select(own, None)
    assert not own.binding.exists() and result["configured"]


async def test_unconfigured_own_profile_adopts_without_service_calls(
    tmp_path, launch_agents, launchd, monkeypatch
):
    from insto.desktop import home

    monkeypatch.setattr(home, "managed_service", None)
    root = tmp_path / "fresh-root"
    path = cli_home(tmp_path)
    result = await home.select(Profile(root), path)
    assert json.loads((root / "desktop-home.json").read_bytes())["home"] == str(path)
    assert result["configured"] and result["status"] == "stopped"


async def test_adopted_state_is_written_before_the_binding(tmp_path, own, launchd, monkeypatch):
    from insto.desktop import home

    path = cli_home(tmp_path)

    def refuse(self, home_path):
        raise OSError("disk full")

    monkeypatch.setattr(Profile, "write_binding", refuse)
    with pytest.raises(DesktopError):
        await home.select(own, path)
    assert (path / "desktop-state.json").exists() and not own.binding.exists()


@pytest.mark.parametrize("database", ["ok", "missing"])
async def test_select_is_refused_while_another_root_holds_the_home(
    tmp_path, own, launchd, database
):
    from insto.desktop import home

    path = cli_home(tmp_path, database=database)
    other_root = Profile(tmp_path / "other-root")
    with other_root.locked(initialize=True):
        other_root.write_binding(path)
    other = Profile(other_root.root, home=path)
    with other.locked(initialize=True), pytest.raises(DesktopError) as info:
        await home.select(own, path)
    assert info.value.code == "profile_busy"
    assert not own.binding.exists()
    # Refused before the own service was touched and before anything entered the home.
    assert launchd.events == [] and launchd.state(own.home)["process"] == "running"
    assert own.read_state()["desired_service"] == "running"
    assert not (path / "services").exists() and not (path / "desktop-state.json").exists()
    assert (path / "store.db").exists() is (database == "ok")


async def test_unsafe_services_directory_refuses_before_any_stop(tmp_path, own, launchd):
    from insto.desktop import home

    path = cli_home(tmp_path)
    (path / "services").mkdir(mode=0o755)
    (path / "services").chmod(0o755)  # mkdir(mode=) is umask-masked
    with pytest.raises(DesktopError) as info:
        await home.select(own, path)
    assert info.value.code == "storage_error"
    assert launchd.events == [] and launchd.state(own.home)["process"] == "running"
    assert own.read_state()["desired_service"] == "running"
    assert not own.binding.exists() and not (path / "desktop-state.json").exists()


async def test_broken_own_profile_refuses_before_any_stop(tmp_path, own, launchd):
    """The own profile's config must resolve before its service is stopped."""
    from insto.desktop import home

    (own.home / "store.db").unlink()  # _config raises schema_mismatch for the own profile
    path = cli_home(tmp_path)
    with pytest.raises(DesktopError) as info:
        await home.select(own, path)
    assert info.value.code == "schema_mismatch"
    assert launchd.events == [] and launchd.state(own.home)["process"] == "running"
    assert not own.binding.exists() and not (path / "desktop-state.json").exists()


def test_shared_lease_requires_the_holder_to_own_the_same_lock(tmp_path):
    root = tmp_path / "desktop"
    own = Profile(root)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    other = Profile(root, home=elsewhere)
    with pytest.raises(DesktopError), other.shared_lease(own):
        pass
    with own.locked(initialize=True):
        with other.shared_lease(own):
            assert other._leased and (elsewhere / ".desktop.lock").exists()
        assert not other._leased
        with pytest.raises(DesktopError), own.shared_lease(own):
            pass


@pytest.mark.parametrize(
    "value, allow_none, code",
    [
        (None, False, "invalid_params"),
        ("", True, "invalid_params"),
        (5, True, "invalid_params"),
        ("/" + "a" * 1024, True, "invalid_params"),
        ("/" + "я" * 512, True, "invalid_params"),
        ("/x\x00y", True, "invalid_params"),
        ("relative/home", True, "home_invalid"),
        ("~alice/.insto", True, "home_invalid"),
        ("/a/../b", True, "home_invalid"),
    ],
)
def test_validate_path_rejects_bad_values(value, allow_none, code):
    from insto.desktop.home_params import validate_path

    with pytest.raises(DesktopError) as info:
        validate_path(value, allow_none=allow_none)
    assert info.value.code == code


def test_validate_path_accepts_absolute_and_tilde_paths(tmp_path, monkeypatch):
    from insto.desktop.home_params import validate_path

    monkeypatch.setenv("HOME", str(tmp_path))
    assert validate_path(None, allow_none=True) is None
    assert validate_path(str(tmp_path / "home"), allow_none=False) == tmp_path / "home"
    assert validate_path(str(tmp_path) + "/home/", allow_none=False) == tmp_path / "home"
    assert validate_path("~/.insto", allow_none=False) == tmp_path / ".insto"
    assert validate_path("~", allow_none=False) == tmp_path
    assert validate_path("/" + "a" * 1023, allow_none=False) == Path("/" + "a" * 1023)
    link = tmp_path / "link"
    link.symlink_to(tmp_path)
    with pytest.raises(DesktopError, match="home_invalid"):
        validate_path(str(link / "home"), allow_none=False)


@pytest.mark.parametrize("value", ["~/.insto", "~"])
def test_validate_path_refuses_tilde_without_an_account_home(monkeypatch, value):
    """An empty HOME expands "~" to the filesystem root: no account home to resolve."""
    from insto.desktop.home_params import validate_path

    monkeypatch.setenv("HOME", "")
    with pytest.raises(DesktopError, match="home_invalid"):
        validate_path(value, allow_none=False)
