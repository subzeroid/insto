import json
import subprocess
import sys

import pytest

from insto.desktop.errors import DesktopError
from insto.desktop.profile import Profile
from tests.test_desktop_operations import configured as _configured
from tests.test_desktop_operations import environment as _environment

configured = _configured
environment = _environment


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["prepared", "stopped", "written", "rollback"])
async def test_crash_reconstructs_exact_old_config_and_observed_state(configured, phase):
    from insto.desktop import operations
    from insto.desktop.configuration import config_bytes

    profile, service = configured
    old = profile.read_config()
    state = profile.read_state()
    with profile.locked():
        profile.write_backup(old)
        journal = profile.new_journal(
            kind="replace", previous_state=state, previous_running=False, remaining=3
        )
        journal["phase"] = phase
        profile.write_journal(journal)
        if phase in ("written", "rollback"):
            profile.write_config(config_bytes(profile, "offline-new-secret"))
    before = profile.read_config()
    assert (await operations.inspect_profile(profile))["status"] == "recovery_required"
    assert profile.read_config() == before
    await operations.change_service(Profile(profile.root), "repair")
    assert profile.read_config() == old
    assert profile.read_state() == state
    assert not service.running
    assert profile.read_journal() is None
    assert profile.read_backup() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["committed", "rolled_back"])
async def test_terminal_recovery_only_cleans_up(configured, phase):
    from insto.desktop import operations

    profile, service = configured
    old = profile.read_config()
    with profile.locked():
        journal = profile.new_journal(
            kind="replace", previous_state=profile.read_state(), previous_running=False, remaining=3
        )
        journal["phase"] = phase
        profile.write_journal(journal)
    await operations.change_service(Profile(profile.root), "repair")
    assert not service.events
    assert profile.read_config() == old
    assert profile.read_journal() is None


@pytest.mark.asyncio
async def test_missing_nonterminal_backup_is_never_success(configured):
    from insto.desktop import operations

    profile, service = configured
    with profile.locked():
        journal = profile.new_journal(
            kind="replace", previous_state=profile.read_state(), previous_running=True, remaining=3
        )
        profile.write_journal(journal)
    with pytest.raises(DesktopError, match="recovery_required"):
        await operations.change_service(Profile(profile.root), "repair")
    assert profile.read_journal() == journal
    assert not service.events


@pytest.mark.asyncio
async def test_rollback_idle_failure_keeps_candidate_and_journal(configured):
    from insto.desktop import operations
    from insto.desktop.configuration import config_bytes

    profile, service = configured
    with profile.locked():
        profile.write_backup(profile.read_config())
        profile.write_journal(
            profile.new_journal(
                kind="replace",
                previous_state=profile.read_state(),
                previous_running=True,
                remaining=3,
            )
        )
        profile.write_config(config_bytes(profile, "offline-new-secret"))
    service.fail_idle = True
    with pytest.raises(DesktopError, match="recovery_required"):
        await operations.change_service(Profile(profile.root), "repair")
    assert b"offline-new-secret" in profile.read_config()
    assert profile.read_backup() is not None
    assert "offline-" not in json.dumps(profile.read_journal())


@pytest.mark.asyncio
@pytest.mark.parametrize("matches", [False, True])
async def test_orphan_backup_requires_exact_current_bytes(configured, matches):
    from insto.desktop import operations

    profile, service = configured
    service.running = False
    with profile.locked():
        profile.write_backup(profile.read_config() if matches else b"offline-foreign")
    if matches:
        await operations.change_service(profile, "repair")
        assert profile.read_backup() is None
        assert not service.events
        assert not service.running
    else:
        with pytest.raises(DesktopError, match="recovery_required"):
            await operations.change_service(profile, "repair")
        assert profile.read_backup() == b"offline-foreign"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "point",
    [
        "prepared",
        "stopped",
        "written",
        "committed",
        "rollback",
        "rolled_back",
        "write_backup",
        "write_config",
        "write_state",
        "remove_backup",
        "remove_journal",
    ],
)
async def test_real_process_death_at_durable_boundaries_is_recoverable(configured, point):
    from insto.desktop import operations

    profile, _ = configured
    old, state = profile.read_config(), profile.read_state()
    script = r"""
import asyncio, contextlib, os, sys
from pathlib import Path
from insto.desktop import operations
from insto.desktop.profile import Profile
from tests.test_desktop_operations import Service
profile = Profile(Path(sys.argv[1]))
point = sys.argv[2]
service = Service(profile)
service.running = True
service.fail_start = int(point in {"rollback", "rolled_back"})
@contextlib.contextmanager
def managed(**kwargs):
    service.deadline = kwargs["deadline"]
    yield service
async def validate(token):
    return 3
operations.managed_service = managed
operations.validate_candidate = validate
original = profile.write_journal
def write_journal(journal):
    original(journal)
    if journal["phase"] == point:
        os._exit(71)
profile.write_journal = write_journal
if point.startswith(("write_", "remove_")):
    original_method = getattr(profile, point)
    def stop_after(*args):
        original_method(*args)
        os._exit(71)
    setattr(profile, point, stop_after)
asyncio.run(operations.replace_credentials(profile, "offline-new-secret"))
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", script, str(profile.root), point],
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 71, result.stderr.decode()
    await operations.change_service(Profile(profile.root), "repair")
    if point in {"committed", "remove_backup", "remove_journal"}:
        assert b"offline-new-secret" in profile.read_config()
        assert profile.read_state()["quota_remaining"] == 3
    else:
        assert profile.read_config() == old
        assert profile.read_state() == state
    assert profile.read_journal() is None
    assert profile.read_backup() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("point", ["write_journal", "write_config", "write_state"])
async def test_first_setup_process_death_reconciles_without_token_prompt(environment, point):
    from insto.desktop import operations

    profile, _ = environment
    script = r"""
import asyncio, os, sys
from pathlib import Path
from insto.desktop import operations
from insto.desktop.profile import Profile
profile = Profile(Path(sys.argv[1]))
async def validate(token):
    return 8
operations.validate_candidate = validate
method = getattr(profile, sys.argv[2])
def stop_after(*args):
    method(*args)
    os._exit(72)
setattr(profile, sys.argv[2], stop_after)
asyncio.run(operations.configure(profile, "offline-new-secret"))
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", script, str(profile.root), point],
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 72, result.stderr.decode()
    result = await operations.change_service(Profile(profile.root), "repair")
    assert result["status"] == ("unconfigured" if point == "write_journal" else "running")
    assert profile.read_journal() is None
