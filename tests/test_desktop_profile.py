"""Private profile operations use real files and never adopt unknown data."""

import json
import os
import stat
import subprocess
import sys

import pytest

from insto.desktop.errors import DesktopError
from insto.desktop.profile import Profile


def test_missing_inspection_has_no_side_effect(tmp_path, monkeypatch):
    root = tmp_path / "new-root"
    monkeypatch.setenv("INSTO_DESKTOP_ROOT", str(root))
    monkeypatch.setenv("INSTO_HOME", str(tmp_path / "foreign"))
    profile = Profile.from_environment()
    assert profile.root == root
    assert profile.home == root / "profile"
    assert not profile.adopted and profile.home_lock_path is None
    assert profile.read_state() is None
    assert profile.read_config() is None
    assert profile.read_journal() is None
    assert not root.exists()


def test_private_state_and_config_roundtrip(tmp_path):
    profile = Profile(tmp_path / "app")
    with profile.locked(initialize=True):
        state = profile.new_state(remaining=0, desired="stopped")
        profile.write_state(state)
        profile.write_config(b'backend = "hikerapi"\n[hikerapi]\ntoken = "private-sentinel"\n')
    assert profile.read_state() == state
    assert b"private-sentinel" in profile.read_config()
    assert "private-sentinel" not in profile.state.read_text()
    assert state["quota_remaining"] == 0
    for path in (profile.root, profile.home):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    for path in (profile.state, profile.config, profile.lock_path):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_writes_without_lease_are_rejected(tmp_path):
    profile = Profile(tmp_path / "app")
    with pytest.raises(DesktopError):
        profile.write_config(b"data")
    assert not profile.root.exists()


def test_unknown_populated_profile_is_not_adopted(tmp_path):
    profile = Profile(tmp_path / "app")
    profile.home.mkdir(parents=True, mode=0o700)
    profile.root.chmod(0o700)
    config = profile.home / "config.toml"
    config.write_text("external = true\n")
    config.chmod(0o600)
    before = config.read_bytes()
    with pytest.raises(DesktopError, match="profile_ownership"), profile.locked(initialize=True):
        pass
    assert config.read_bytes() == before
    assert not profile.state.exists()


@pytest.mark.parametrize(
    "kind",
    [
        "root_mode",
        "root_link",
        "parent_link",
        "relative",
        "home_link",
        "state_link",
        "state_mode",
        "state_owner",
        "hardlink",
        "fifo",
        "oversized",
    ],
)
def test_unsafe_profiles_are_rejected_without_chmod(tmp_path, monkeypatch, kind):
    root = tmp_path / "app"
    if kind == "relative":
        with pytest.raises(DesktopError):
            Profile(type(root)("relative"))
        return
    if kind in {"root_link", "parent_link"}:
        target = tmp_path / "target"
        target.mkdir(mode=0o700)
        root.symlink_to(target, target_is_directory=True)
        with pytest.raises(DesktopError):
            Profile(root if kind == "root_link" else root / "nested")
        return
    profile = Profile(root)
    with profile.locked(initialize=True):
        profile.write_state(profile.new_state(remaining=10, desired="stopped"))
    if kind == "root_mode":
        root.chmod(0o755)
    elif kind == "home_link":
        profile.home.rmdir()
        profile.home.symlink_to(tmp_path, target_is_directory=True)
    elif kind in {"state_link", "hardlink", "fifo"}:
        profile.state.unlink()
        if kind == "fifo":
            os.mkfifo(profile.state, 0o600)
        else:
            other = tmp_path / "other"
            other.write_text("{}")
            other.chmod(0o600)
            if kind == "hardlink":
                os.link(other, profile.state)
            else:
                profile.state.symlink_to(other)
    elif kind == "state_mode":
        profile.state.chmod(0o644)
    elif kind == "state_owner":
        monkeypatch.setattr(os, "getuid", lambda: 123456)
    elif kind == "oversized":
        profile.state.write_bytes(b"x" * 65537)
    with pytest.raises(DesktopError):
        profile.read_state()
    if kind == "state_mode":
        assert stat.S_IMODE(profile.state.stat().st_mode) == 0o644


@pytest.mark.parametrize(
    "change",
    [
        {"uid": -1},
        {"profile": "/foreign"},
        {"schema_version": True},
        {"quota_remaining": True},
        {"desired_service": "sometimes"},
        {"token": "forbidden"},
    ],
)
def test_state_schema_and_binding_are_exact(tmp_path, change):
    profile = Profile(tmp_path / "app")
    with profile.locked(initialize=True):
        state = profile.new_state(remaining=3, desired="running")
        with pytest.raises(DesktopError):
            profile.write_state({**state, **change})


def test_atomic_replacement_fsyncs_and_keeps_private_mode(tmp_path, monkeypatch):
    profile = Profile(tmp_path / "app")
    sync_modes = []
    original_fsync = os.fsync

    def fsync(fd):
        sync_modes.append(os.fstat(fd).st_mode)
        original_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    with profile.locked(initialize=True):
        profile.write_state(profile.new_state(remaining=1, desired="stopped"))
        profile.write_config(b"old")
        old_inode = profile.config.stat().st_ino
        profile.write_config(b"new")
    assert profile.config.read_bytes() == b"new"
    assert profile.config.stat().st_ino != old_inode
    assert any(stat.S_ISDIR(mode) for mode in sync_modes)
    assert any(stat.S_ISREG(mode) for mode in sync_modes)
    assert not list(profile.home.glob(".config.toml.*"))


def test_real_cross_process_lock_is_nonblocking_and_stable(tmp_path):
    profile = Profile(tmp_path / "app")
    script = """
import sys
from pathlib import Path
from insto.desktop.profile import Profile
from insto.desktop.errors import DesktopError
try:
    with Profile(Path(sys.argv[1])).locked():
        raise AssertionError('acquired a busy profile')
except DesktopError as error:
    assert error.code == 'profile_busy'
"""
    with profile.locked(initialize=True):
        profile.write_state(profile.new_state(remaining=1, desired="stopped"))
        inode = profile.lock_path.stat().st_ino
        completed = subprocess.run(
            [sys.executable, "-I", "-B", "-c", script, str(profile.root)],
            capture_output=True,
            timeout=5,
        )
        assert completed.returncode == 0, completed.stderr
    with profile.locked():
        assert profile.lock_path.stat().st_ino == inode


def test_fixed_journal_binding_and_backup_roundtrip(tmp_path):
    profile = Profile(tmp_path / "app")
    with profile.locked(initialize=True):
        state = profile.new_state(remaining=4, desired="running")
        profile.write_state(state)
        profile.write_config(b"original-private-config")
        journal = profile.new_journal(
            kind="replace", previous_state=state, previous_running=True, remaining=7
        )
        profile.write_backup(profile.read_config())
        profile.write_journal(journal)
        assert profile.read_journal() == journal
        assert profile.read_backup() == b"original-private-config"
        assert "original-private-config" not in profile.recovery.read_text()
        with pytest.raises(DesktopError):
            profile.write_journal({**journal, "backup": "/outside"})
        profile.remove_backup()
        profile.remove_journal()
    assert not profile.backup.exists()
    assert not profile.recovery.exists()


def test_duplicate_json_state_is_not_accepted(tmp_path):
    profile = Profile(tmp_path / "app")
    with profile.locked(initialize=True):
        state = profile.new_state(remaining=4, desired="running")
        profile.write_state(state)
    payload = json.dumps(state).replace(
        '"schema_version": 1', '"schema_version": 2, "schema_version": 1'
    )
    profile.state.write_text(payload)
    with pytest.raises(DesktopError):
        profile.read_state()


@pytest.mark.parametrize("payload", [b"null", b"[]", b"{}"])
def test_existing_invalid_state_is_not_missing(tmp_path, payload):
    profile = Profile(tmp_path / "app")
    with profile.locked(initialize=True):
        profile.write_state(profile.new_state(remaining=4, desired="running"))
    profile.state.write_bytes(payload)
    with pytest.raises(DesktopError):
        profile.read_state()


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("level", ["parent", "ancestor"])
def test_writable_ancestors_are_refused(tmp_path, existing, level):
    parent = tmp_path / "parent"
    parent.mkdir(mode=0o700)
    profile = Profile(parent / "app")
    if existing:
        with profile.locked(initialize=True):
            profile.write_state(profile.new_state(remaining=4, desired="running"))
    unsafe = parent if level == "parent" else tmp_path
    unsafe.chmod(0o777)
    try:
        with pytest.raises(DesktopError, match="profile_ownership"):
            profile.read_state()
    finally:
        unsafe.chmod(0o700)


def test_new_directory_entries_fsync_their_parents(tmp_path, monkeypatch):
    from insto.desktop import profile as module

    synced = []
    original = module._sync_directory

    def sync(path):
        synced.append(path)
        original(path)

    monkeypatch.setattr(module, "_sync_directory", sync)
    profile = Profile(tmp_path / "app")
    with profile.locked(initialize=True):
        assert tmp_path in synced
        assert profile.root in synced


def test_oversized_lock_is_refused(tmp_path):
    profile = Profile(tmp_path / "app")
    with profile.locked(initialize=True):
        profile.write_state(profile.new_state(remaining=4, desired="running"))
    profile.lock_path.write_bytes(b"x" * 65537)
    with pytest.raises(DesktopError, match="profile_ownership"), profile.locked():
        pass


def test_crash_before_first_journal_publication_does_not_block_setup(tmp_path):
    profile = Profile(tmp_path / "app")
    script = """
import os, sys
from pathlib import Path
from insto.desktop.profile import Profile
profile = Profile(Path(sys.argv[1]))
replace = os.replace
def crash(source, destination):
    if destination == profile.recovery:
        os._exit(77)
    return replace(source, destination)
os.replace = crash
with profile.locked(initialize=True):
    profile.write_journal(profile.new_journal(kind='setup', previous_state=None,
        previous_running=False, remaining=10))
"""
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script, str(profile.root)],
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 77, result.stderr
    assert profile.read_state() is None and profile.read_journal() is None
    with profile.locked(initialize=True):
        profile.write_journal(
            profile.new_journal(
                kind="setup", previous_state=None, previous_running=False, remaining=10
            )
        )
    assert profile.read_journal()["kind"] == "setup"


def test_crash_after_backup_publication_keeps_reconcilable_single_link(tmp_path):
    profile = Profile(tmp_path / "app")
    with profile.locked(initialize=True):
        profile.write_state(profile.new_state(remaining=4, desired="running"))
        profile.write_config(b"old-private-config")
    script = """
import os, sys
from pathlib import Path
from insto.desktop.profile import Profile
profile = Profile(Path(sys.argv[1]))
link, replace = os.link, os.replace
def publish(function):
    def crash(source, destination):
        function(source, destination)
        if destination == profile.backup:
            os._exit(78)
    return crash
os.link, os.replace = publish(link), publish(replace)
with profile.locked():
    profile.write_backup(profile.read_config())
"""
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script, str(profile.root)],
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 78, result.stderr
    with profile.locked():
        assert profile.read_journal() is None
        assert profile.read_backup() == profile.read_config() == b"old-private-config"
        profile.remove_backup()


def test_backup_publication_refuses_existing_file(tmp_path):
    profile = Profile(tmp_path / "app")
    with profile.locked(initialize=True):
        profile.write_backup(b"original")
        with pytest.raises(DesktopError):
            profile.write_backup(b"replacement")
        assert profile.read_backup() == b"original"


def test_binding_selects_an_adopted_home_and_keeps_state_inside_it(tmp_path):
    from insto.desktop.profile import Profile

    root = tmp_path / "desktop"
    home = tmp_path / "cli-home"
    home.mkdir(mode=0o700)
    own = Profile(root)
    with own.locked(initialize=True):
        own.write_binding(home)
    assert own.read_binding() == {
        "schema_version": 1,
        "managed_by": "insto-gui",
        "uid": os.getuid(),
        "home": str(home),
    }
    adopted = Profile(root, home=home)
    assert adopted.adopted and adopted.home == home
    assert adopted.state == home / "desktop-state.json"
    assert adopted.config == home / "config.toml"
    assert adopted.recovery == home / "desktop-recovery.json"
    with adopted.locked(initialize=True):
        adopted.write_state(adopted.new_state(remaining=None, desired="stopped"))
    assert (home / ".desktop.lock").exists()
    state = adopted.read_state()
    assert state["profile"] == str(home)
    assert state["quota_remaining"] is None and state["quota_checked_at"] is None
    # The own profile is not the bound one while a binding exists, and it has no
    # state of its own: binding management leases it without verification.
    with own.locked(initialize=True, verify_binding=False):
        own.remove_binding()
    assert own.read_binding() is None


def test_binding_is_read_by_from_environment(tmp_path, monkeypatch):
    from insto.desktop.profile import Profile

    root = tmp_path / "desktop"
    home = tmp_path / "cli-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("INSTO_DESKTOP_ROOT", str(root))
    assert not Profile.from_environment().adopted
    own = Profile(root)
    with own.locked(initialize=True):
        own.write_binding(home)
    profile = Profile.from_environment()
    assert profile.adopted and profile.home == home
    assert Profile.own_from_environment().home == root / "profile"


def test_stale_binding_is_refused_under_the_lock(tmp_path):
    """A profile resolved before another process re-bound the root must not act."""
    from insto.desktop.errors import DesktopError
    from insto.desktop.profile import Profile

    root = tmp_path / "desktop"
    first = tmp_path / "first"
    second = tmp_path / "second"
    for home in (first, second):
        home.mkdir(mode=0o700)
    own = Profile(root)
    with own.locked(initialize=True):
        own.write_binding(first)
    stale = Profile(root, home=first)  # resolved now ...
    with own.locked(initialize=True, verify_binding=False):
        own.write_binding(second)  # ... re-bound by "another process"
    with pytest.raises(DesktopError, match="profile_busy"), stale.locked(initialize=True):
        pass
    # The own profile is not the bound one either.
    with pytest.raises(DesktopError, match="profile_busy"), Profile(root).locked():
        pass
    with Profile(root, home=second).locked(initialize=True):
        pass
    with own.locked(initialize=True, verify_binding=False):
        own.remove_binding()
    with Profile(root).locked(initialize=True):
        pass


def test_two_roots_cannot_lease_the_same_adopted_home(tmp_path):
    from insto.desktop.errors import DesktopError
    from insto.desktop.profile import Profile

    home = tmp_path / "cli-home"
    home.mkdir(mode=0o700)
    roots = []
    for name in ("root-a", "root-b"):
        own = Profile(tmp_path / name)
        with own.locked(initialize=True):
            own.write_binding(home)
        roots.append(Profile(own.root, home=home))
    with (
        roots[0].locked(initialize=True),
        pytest.raises(DesktopError, match="profile_busy"),
        roots[1].locked(initialize=True),
    ):
        pass
    with roots[1].locked(initialize=True):
        pass


def test_adopted_home_under_an_untrusted_parent_is_refused(tmp_path):
    from insto.desktop.errors import DesktopError
    from insto.desktop.profile import Profile

    shared = tmp_path / "shared"
    shared.mkdir(mode=0o777)
    shared.chmod(0o777)
    home = shared / "cli-home"
    home.mkdir(mode=0o700)
    root = tmp_path / "desktop"
    own = Profile(root)
    with own.locked(initialize=True):
        own.write_binding(home)
    with pytest.raises(DesktopError, match="home_invalid"):
        Profile(root, home=home).read_state()


@pytest.mark.parametrize(
    "payload",
    [
        b"[]",
        b'{"schema_version":1,"managed_by":"insto-gui","uid":0,"home":"/x"}',
        b'{"schema_version":1,"managed_by":"insto-gui","uid":%d,"home":"relative"}' % os.getuid(),
        b'{"schema_version":1,"managed_by":"insto-gui","uid":%d,"home":"/x/../y"}' % os.getuid(),
        b'{"schema_version":2,"managed_by":"insto-gui","uid":%d,"home":"/x"}' % os.getuid(),
    ],
)
def test_invalid_binding_is_home_invalid(tmp_path, payload):
    from insto.desktop.errors import DesktopError
    from insto.desktop.profile import Profile

    root = tmp_path / "desktop"
    root.mkdir(mode=0o700)
    binding = root / "desktop-home.json"
    binding.write_bytes(payload)
    binding.chmod(0o600)
    with pytest.raises(DesktopError) as info:
        Profile(root).read_binding()
    assert info.value.code in {"home_invalid", "profile_ownership"}


def test_adopted_home_must_be_a_private_owned_directory(tmp_path):
    from insto.desktop.errors import DesktopError
    from insto.desktop.profile import Profile

    root = tmp_path / "desktop"
    root.mkdir(mode=0o700)
    missing = Profile(root, home=tmp_path / "missing")
    with pytest.raises(DesktopError) as info:
        missing.read_state()
    assert info.value.code == "home_invalid"
    loose = tmp_path / "loose"
    loose.mkdir(mode=0o755)
    with pytest.raises(DesktopError):
        Profile(root, home=loose).read_state()
    with pytest.raises(DesktopError) as info:
        Profile(root, home=root / "profile")
    assert info.value.code == "home_invalid"


def test_quota_fields_are_null_together_or_ints_together(tmp_path):
    from insto.desktop.errors import DesktopError
    from insto.desktop.profile import Profile

    profile = Profile(tmp_path / "desktop")
    with profile.locked(initialize=True):
        good = profile.new_state(remaining=None, desired="running")
        profile.write_state(good)
        for bad in (
            dict(good, quota_remaining=1),
            dict(good, quota_checked_at=1),
            dict(good, quota_remaining=-1, quota_checked_at=1),
        ):
            with pytest.raises(DesktopError):
                profile.write_state(bad)
    assert profile.read_state()["quota_remaining"] is None


def test_migrate_journal_shape(tmp_path):
    from insto.desktop.errors import DesktopError
    from insto.desktop.profile import RETAINED_REGISTRATION, Profile

    profile = Profile(tmp_path / "desktop")
    with profile.locked(initialize=True):
        state = profile.new_state(remaining=5, desired="running")
        journal = profile.new_journal(
            kind="migrate", previous_state=state, previous_running=True, remaining=None
        )
        assert journal["backup"] == RETAINED_REGISTRATION and journal["new_remaining"] is None
        profile.write_journal(journal)
        for phase in ("stopped", "published", "started", "rollback", "rolled_back", "committed"):
            profile.write_journal(dict(journal, phase=phase))
        with pytest.raises(DesktopError):
            profile.write_journal(dict(journal, backup="config.previous.toml"))
        with pytest.raises(DesktopError):
            profile.write_journal(dict(journal, previous_state=None))
        with pytest.raises(DesktopError):
            profile.write_journal(
                profile.new_journal(
                    kind="replace", previous_state=state, previous_running=False, remaining=None
                )
            )
    assert profile.read_journal()["kind"] == "migrate"


def test_adopted_home_is_validated_before_a_fresh_root_is_created(tmp_path):
    from insto.desktop.errors import DesktopError
    from insto.desktop.profile import Profile

    root = tmp_path / "desktop"
    home = tmp_path / "cli-home"
    home.mkdir(mode=0o755)
    profile = Profile(root, home=home)
    with (
        pytest.raises(DesktopError, match="home_invalid"),
        profile.locked(initialize=True, verify_binding=False),
    ):
        pass
    assert not (home / ".desktop.lock").exists()
    assert not root.exists()


@pytest.mark.parametrize(
    "select",
    [lambda root: root, lambda root: root / "inside", lambda root: root.parent],
    ids=["root", "inside_root", "parent_of_root"],
)
def test_home_may_not_overlap_the_root(tmp_path, select):
    from insto.desktop.errors import DesktopError
    from insto.desktop.profile import Profile

    root = tmp_path / "desktop"
    root.mkdir(mode=0o700)
    home = select(root)
    with pytest.raises(DesktopError) as info:
        Profile(root, home=home)
    assert info.value.code == "home_invalid"
    binding = root / "desktop-home.json"
    binding.write_text(
        json.dumps(
            {"schema_version": 1, "managed_by": "insto-gui", "uid": os.getuid(), "home": str(home)}
        )
    )
    binding.chmod(0o600)
    with pytest.raises(DesktopError) as info:
        Profile(root).read_binding()
    assert info.value.code == "home_invalid"


def test_symlinked_adopted_home_lock_is_refused(tmp_path):
    from insto.desktop.errors import DesktopError
    from insto.desktop.profile import Profile

    root = tmp_path / "desktop"
    home = tmp_path / "cli-home"
    home.mkdir(mode=0o700)
    own = Profile(root)
    with own.locked(initialize=True):
        own.write_binding(home)
    other = tmp_path / "other"
    other.write_text("")
    other.chmod(0o600)
    (home / ".desktop.lock").symlink_to(other)
    profile = Profile(root, home=home)
    with (
        pytest.raises(DesktopError, match="storage_error"),
        profile.locked(initialize=True, verify_binding=False),
    ):
        pass
    assert (home / ".desktop.lock").is_symlink()
