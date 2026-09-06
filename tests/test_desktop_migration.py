import contextlib
import json
import os
import plistlib
import sys
from pathlib import Path

import pytest

from insto.desktop.errors import DesktopError
from insto.desktop.profile import Profile
from insto.exceptions import BackendError
from insto.service import watch_service


class Launchd:
    """One fake launchd job keyed by the registration bytes that started it.

    Every lease method first proves the files on disk are exactly this lease's
    artifacts, exactly like ``ManagedService._artifacts`` does.
    """

    def __init__(self):
        self.loaded_with = None  # manifest bytes of the loaded registration
        self.fail = set()  # {"stop_old", "start_new", "start_old"}
        self.events = []

    def service(self, paths, config, artifacts):
        launchd = self

        class Service:
            def __init__(self):
                self._paths = paths
                self._config = config
                self._manifest, self._plist = artifacts
                self.deadline = 1e12

            def _installation(self):
                exists = []
                for path, desired in (
                    (self._paths.manifest, self._manifest),
                    (self._paths.plist, self._plist),
                ):
                    present = os.path.lexists(path)
                    if present and not watch_service._existing_matches(path, desired):
                        raise BackendError("watch service artifacts do not match the owned runtime")
                    exists.append(present)
                return (
                    "installed" if all(exists) else "incomplete" if any(exists) else "not_installed"
                )

            async def inspect_owned(self):
                installation = self._installation()
                running = launchd.loaded_with == self._manifest
                if launchd.loaded_with is not None and installation != "installed":
                    raise BackendError("refusing a loaded service with incomplete ownership")
                return {
                    "installation": installation,
                    "registration": "loaded" if running else "unloaded",
                    "process": {
                        "state": "running" if running else None,
                        "pid": 7 if running else None,
                    },
                    "executor": {
                        "state": "busy" if running else "idle",
                        "pid": 7 if running else None,
                    },
                }

            async def ensure_stopped(self):
                self._installation()
                launchd.events.append(("stop", self._manifest))
                if "stop_old" in launchd.fail:
                    launchd.fail.discard("stop_old")
                    raise BackendError("stop failed")
                if launchd.loaded_with == self._manifest:
                    launchd.loaded_with = None
                return await self.inspect_owned()

            async def ensure_running(self):
                self._installation()
                launchd.events.append(("start", self._manifest))
                key = "start_new" if b"NEW" in self._manifest else "start_old"
                if key in launchd.fail:
                    launchd.fail.discard(key)
                    raise BackendError("start failed")
                launchd.loaded_with = self._manifest
                return await self.inspect_owned()

            async def remove_registration(self):
                report = await self.inspect_owned()
                if report["registration"] == "loaded":
                    raise BackendError("watch service is still registered or running")
                for path in (self._paths.plist, self._paths.manifest):
                    if os.path.lexists(path):
                        path.unlink()
                launchd.events.append(("remove", self._manifest))

            @contextlib.contextmanager
            def idle_executor(self):
                assert launchd.loaded_with is None
                yield

        return Service()


@pytest.fixture
def world(tmp_path, monkeypatch):
    from insto.desktop import migration, operations, recovery, service_facts
    from insto.desktop.configuration import initialize_database, parse_profile_config

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / "Library" / "LaunchAgents").mkdir(parents=True)
    profile = Profile(tmp_path / "desktop")
    launchd = Launchd()
    with profile.locked(initialize=True):
        profile.write_config(operations.config_bytes(profile, "offline-old-secret"))
        profile.write_state(profile.new_state(remaining=8, desired="running"))
        initialize_database(profile.home / "store.db")
    config = parse_profile_config(profile, profile.read_config())
    paths = watch_service.service_paths(profile.home)
    for directory in (profile.home / "services", paths.directory, paths.log_dir):
        directory.mkdir(mode=0o700, exist_ok=True)
    new_manifest, new_plist = watch_service._desired(paths, config, None)
    new_value = json.loads(new_manifest)
    new_manifest = new_manifest.replace(b"}\n", b',"marker":"NEW"}\n')
    old_value = dict(new_value, python=str(tmp_path / "old-runtime" / "python3"))
    old_manifest = (json.dumps(old_value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    old_plist = plistlib.dumps(
        watch_service._plist_document(paths, old_value, dont_write_bytecode=False),
        fmt=plistlib.FMT_XML,
        sort_keys=True,
    )
    watch_service._atomic_write(paths.manifest, old_manifest)
    watch_service._atomic_write(paths.plist, old_plist)
    launchd.loaded_with = old_manifest

    def desired(p, c, env):
        return new_manifest, new_plist

    monkeypatch.setattr(watch_service, "_desired", desired)

    def make(paths_, config_, deadline, *, artifacts=None):
        return launchd.service(paths_, config_, artifacts or (new_manifest, new_plist))

    monkeypatch.setattr(migration, "ManagedService", make)
    monkeypatch.setattr(recovery, "ManagedService", make)

    @contextlib.contextmanager
    def managed(**kwargs):
        yield make(paths, kwargs["config"], kwargs["deadline"], artifacts=kwargs.get("artifacts"))

    monkeypatch.setattr(migration, "managed_service", managed)
    monkeypatch.setattr(operations, "managed_service", managed)
    monkeypatch.setattr(operations, "read_service", lambda *a: make(paths, config, 1e12))

    async def facts(paths_, *, deadline, expected=None):
        manifest, plist = watch_service.read_registration(paths_)
        if manifest is None:
            return {
                "registration": "unknown" if plist is not None or launchd.loaded_with else "none",
                "installation": None,
                "interpreter": None,
                "interpreter_exists": None,
                "loaded": launchd.loaded_with is not None,
                "process": "running" if launchd.loaded_with is not None else "stopped",
                "settings": None,
            }
        value = json.loads(manifest)
        current = value["python"] == os.path.abspath(sys.executable)
        settings = None
        if expected is not None:
            settings = (
                "matching"
                if service_facts.manifest_settings(value)
                == service_facts.manifest_settings(expected)
                else "different"
            )
        return {
            "registration": "owned",
            "installation": "installed" if plist is not None else "incomplete",
            "interpreter": "current" if current else "other",
            "interpreter_exists": current,
            "loaded": launchd.loaded_with is not None,
            "process": "running" if launchd.loaded_with is not None else "stopped",
            "settings": settings,
        }

    monkeypatch.setattr(migration, "registration_facts", facts)
    monkeypatch.setattr(operations, "registration_facts", facts)
    return profile, launchd, paths, (old_manifest, old_plist), (new_manifest, new_plist)


async def test_migrate_moves_a_running_service_and_cleans_up(world):
    from insto.desktop import migration

    profile, launchd, paths, old, new = world
    result = await migration.migrate(profile)
    assert result["status"] == "running" and result["service_running"] is True
    assert watch_service.read_registration(paths) == new
    assert launchd.loaded_with == new[0]
    assert launchd.events == [("stop", old[0]), ("start", new[0])]
    assert profile.read_journal() is None
    assert watch_service.read_retained_registration(paths) is None
    assert profile.read_state()["desired_service"] == "running"


async def test_migrate_keeps_a_stopped_service_stopped(world):
    from insto.desktop import migration

    profile, launchd, paths, _old, new = world
    launchd.loaded_with = None
    with profile.locked():
        profile.write_state(dict(profile.read_state(), desired_service="stopped"))
    result = await migration.migrate(profile)
    assert result["status"] == "stopped" and result["service_running"] is False
    assert watch_service.read_registration(paths) == new
    assert launchd.loaded_with is None
    assert ("start", new[0]) not in launchd.events


async def test_migrate_is_a_noop_when_the_registration_already_matches(world):
    from insto.desktop import migration

    profile, launchd, _paths, _old, _new = world
    await migration.migrate(profile)
    launchd.events.clear()
    result = await migration.migrate(profile)
    assert result["service_running"] is True and launchd.events == []


async def test_migrate_normalizes_a_same_interpreter_legacy_form(world):
    """Same python, plist without -B: not a no-op, migrated like any other registration."""
    from insto.desktop import migration

    profile, launchd, paths, old, new = world
    legacy_value = dict(json.loads(old[0]), python=os.path.abspath(sys.executable))
    legacy = (
        (json.dumps(legacy_value, sort_keys=True, separators=(",", ":")) + "\n").encode(),
        plistlib.dumps(
            watch_service._plist_document(paths, legacy_value, dont_write_bytecode=False),
            fmt=plistlib.FMT_XML,
            sort_keys=True,
        ),
    )
    watch_service.replace_registration(paths, old, legacy)
    launchd.loaded_with = legacy[0]
    result = await migration.migrate(profile)
    assert result["status"] == "running"
    assert watch_service.read_registration(paths) == new and launchd.loaded_with == new[0]


async def test_migrate_refuses_different_settings_before_touching_anything(world):
    from insto.desktop import migration

    profile, launchd, paths, old, _new = world
    moved = dict(json.loads(old[0]), db_path=str(paths.home / "other.db"))
    changed = (
        (json.dumps(moved, sort_keys=True, separators=(",", ":")) + "\n").encode(),
        plistlib.dumps(
            watch_service._plist_document(paths, moved, dont_write_bytecode=False),
            fmt=plistlib.FMT_XML,
            sort_keys=True,
        ),
    )
    watch_service.replace_registration(paths, old, changed)
    launchd.loaded_with = changed[0]
    with pytest.raises(DesktopError) as info:
        await migration.migrate(profile)
    assert info.value.code == "service_config_mismatch"
    assert launchd.events == [] and watch_service.read_registration(paths) == changed
    assert (
        profile.read_journal() is None and watch_service.read_retained_registration(paths) is None
    )


async def test_migrate_completes_an_incomplete_registration(world):
    from insto.desktop import migration

    profile, launchd, paths, _old, new = world
    launchd.loaded_with = None
    paths.plist.unlink()
    result = await migration.migrate(profile)
    assert result["status"] == "running"
    assert watch_service.read_registration(paths) == new and launchd.loaded_with == new[0]


@pytest.mark.parametrize("point", ["stop_old", "publish", "start_new"])
async def test_failure_at_each_transition_restores_the_old_registration(world, point, monkeypatch):
    from insto.desktop import migration

    profile, launchd, paths, old, new = world
    if point == "publish":
        original = watch_service.replace_registration

        def failing(paths_, expected, desired):
            if desired == new:
                raise BackendError("disk full")
            return original(paths_, expected, desired)

        monkeypatch.setattr(watch_service, "replace_registration", failing)
    else:
        launchd.fail.add(point)
    with pytest.raises(DesktopError) as info:
        await migration.migrate(profile)
    assert info.value.code == "service_error"
    assert watch_service.read_registration(paths) == old
    assert launchd.loaded_with == old[0]
    assert profile.read_journal() is None
    assert watch_service.read_retained_registration(paths) is None


async def test_rollback_failure_leaves_recovery_required_then_repair_finishes(world):
    from insto.desktop import migration, operations

    profile, launchd, paths, old, _new = world
    launchd.fail.update({"start_new", "start_old"})
    with pytest.raises(DesktopError) as info:
        await migration.migrate(profile)
    assert info.value.code == "recovery_required"
    assert profile.read_journal()["kind"] == "migrate"
    assert (await operations.inspect_profile(profile))["status"] == "recovery_required"
    repaired = await operations.change_service(profile, "repair")
    # The rollback restored a registration that names the OLD interpreter, which
    # the calling lease cannot manage: the repair reports service_error, never
    # a fake "running" (the C1 inspect_profile does the same).
    assert repaired["status"] == "service_error" and repaired["configured"] is True
    assert watch_service.read_registration(paths) == old and launchd.loaded_with == old[0]
    assert profile.read_journal() is None
    assert watch_service.read_retained_registration(paths) is None
    assert (await operations.inspect_profile(profile))["status"] == "service_error"


class Death(BaseException):
    """Process death: nothing after the raise runs, not even in-process rollback."""


@pytest.mark.parametrize("phase", ["prepared", "stopped", "published", "started"])
async def test_crash_at_each_phase_is_rolled_back_by_repair(world, phase, monkeypatch):
    from insto.desktop import migration, operations

    profile, launchd, paths, old, _new = world
    original = profile.write_journal

    def write_journal(journal):
        original(journal)
        if journal["phase"] == phase:
            raise Death

    async def no_rollback(*args):
        raise Death  # the process is gone; Repair is the only path back

    profile.write_journal = write_journal
    monkeypatch.setattr(migration, "rollback_drained", no_rollback)
    with pytest.raises(Death):
        await migration.migrate(profile)
    profile.write_journal = original
    assert profile.read_journal()["phase"] == phase
    repaired = await operations.change_service(Profile(profile.root), "repair")
    assert repaired["status"] == "service_error"
    assert watch_service.read_registration(paths) == old
    assert launchd.loaded_with == old[0]
    assert (
        profile.read_journal() is None and watch_service.read_retained_registration(paths) is None
    )


async def test_crash_between_the_two_file_replacements_is_repaired(world, monkeypatch):
    """Manifest already candidate, plist still previous: the legitimate mixed pair."""
    from insto.desktop import migration, operations

    profile, launchd, paths, old, new = world

    original = watch_service.replace_registration

    def half_publish(paths_, expected, desired):
        if desired == new:
            watch_service._replace_file(paths_.manifest, new[0])
            raise Death
        return original(paths_, expected, desired)

    monkeypatch.setattr(watch_service, "replace_registration", half_publish)

    async def no_rollback(*args):
        raise Death

    monkeypatch.setattr(migration, "rollback_drained", no_rollback)
    with pytest.raises(Death):
        await migration.migrate(profile)
    monkeypatch.setattr(watch_service, "replace_registration", original)
    assert watch_service.read_registration(paths) == (new[0], old[1])
    repaired = await operations.change_service(Profile(profile.root), "repair")
    assert repaired["status"] == "service_error"
    assert watch_service.read_registration(paths) == old and launchd.loaded_with == old[0]
    assert profile.read_journal() is None


async def test_repair_refuses_bytes_that_belong_to_neither_side(world, monkeypatch):
    from insto.desktop import migration, operations

    profile, launchd, paths, old, _new = world

    async def no_rollback(*args):
        raise Death

    monkeypatch.setattr(migration, "rollback_drained", no_rollback)
    original = profile.write_journal

    def die_at_stopped(journal):
        original(journal)
        if journal["phase"] == "stopped":
            raise Death

    profile.write_journal = die_at_stopped
    with pytest.raises(Death):
        await migration.migrate(profile)
    profile.write_journal = original
    foreign = (b'{"foreign":1}\n', b"<plist>foreign</plist>\n")
    watch_service.replace_registration(paths, old, foreign)
    with pytest.raises(DesktopError) as info:
        await operations.change_service(Profile(profile.root), "repair")
    assert info.value.code == "service_ownership_unknown"
    assert watch_service.read_registration(paths) == foreign
    assert profile.read_journal()["phase"] == "stopped"
    assert ("stop", foreign[0]) not in launchd.events


async def test_unreferenced_retention_is_discarded_by_migrate_and_by_repair(world):
    from insto.desktop import migration, operations

    profile, _launchd, paths, _old, new = world
    watch_service.retain_registration(paths, previous=(b"stale", b"stale"), candidate=new)
    await operations.change_service(profile, "repair")
    assert watch_service.read_retained_registration(paths) is None
    watch_service.retain_registration(paths, previous=(b"stale", b"stale"), candidate=new)
    result = await migration.migrate(profile)
    assert result["status"] == "running"
    assert watch_service.read_registration(paths) == new
    assert watch_service.read_retained_registration(paths) is None


async def test_unknown_ownership_refuses_migration_and_uninstall(world, monkeypatch):
    from insto.desktop import migration

    profile, launchd, paths, old, _new = world

    async def unknown(paths_, *, deadline, expected=None):
        return {
            "registration": "unknown",
            "installation": None,
            "interpreter": None,
            "interpreter_exists": None,
            "loaded": True,
            "process": "running",
            "settings": None,
        }

    monkeypatch.setattr(migration, "registration_facts", unknown)
    for action in (migration.migrate, migration.uninstall):
        with pytest.raises(DesktopError) as info:
            await action(profile)
        assert info.value.code == "service_ownership_unknown"
    assert watch_service.read_registration(paths) == old and launchd.events == []


async def test_uninstall_removes_the_registration_and_keeps_data(world):
    from insto.desktop import migration

    profile, launchd, paths, old, _new = world
    (profile.home / "store.db").unlink()  # removal must not need a database or credentials
    result = await migration.uninstall(profile)
    assert launchd.events == [("stop", old[0]), ("remove", old[0])]
    assert result["status"] == "stopped" and result["desired_service"] == "stopped"
    assert (
        profile.read_config() is not None and profile.read_state()["desired_service"] == "stopped"
    )
    assert not paths.manifest.exists() and not paths.plist.exists()
    assert launchd.loaded_with is None


async def test_uninstall_completes_a_manifest_only_registration(world):
    from insto.desktop import migration

    profile, launchd, paths, _old, _new = world
    launchd.loaded_with = None
    paths.plist.unlink()
    result = await migration.uninstall(profile)
    assert result["status"] == "stopped" and not paths.manifest.exists()


async def test_uninstall_without_a_registration_only_persists_intent(world):
    from insto.desktop import migration

    profile, launchd, paths, _old, _new = world
    launchd.loaded_with = None
    paths.manifest.unlink()
    paths.plist.unlink()
    result = await migration.uninstall(profile)
    assert result["status"] == "stopped" and launchd.events == []
    assert profile.read_state()["desired_service"] == "stopped"


async def test_pending_journal_blocks_migration_and_uninstall(world):
    from insto.desktop import migration

    profile, _launchd, _paths, _old, _new = world
    with profile.locked():
        state = profile.read_state()
        profile.write_journal(
            profile.new_journal(
                kind="replace", previous_state=state, previous_running=True, remaining=1
            )
        )
        profile.write_backup(profile.read_config())
    for action in (migration.migrate, migration.uninstall):
        with pytest.raises(DesktopError) as info:
            await action(profile)
        assert info.value.code == "recovery_required"
