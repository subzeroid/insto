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
