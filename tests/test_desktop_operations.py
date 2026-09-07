import asyncio
import contextlib
import json
import selectors
import sqlite3
import subprocess
import sys
import time
from unittest.mock import AsyncMock

import pytest

from insto.desktop.errors import DesktopError
from insto.desktop.profile import Profile
from insto.exceptions import BackendError


class Service:
    def __init__(self, profile, artifacts=None):
        self.profile = profile
        self.artifacts = artifacts
        self.running = False
        self.events = []
        self.fail_start = 0
        self.fail_stop = False
        self.fail_idle = False
        self.deadline = time.monotonic() + 120

    async def inspect_owned(self):
        return {
            "installation": "installed",
            "registration": "loaded" if self.running else "unloaded",
            "process": {
                "state": "running" if self.running else None,
                "pid": 12 if self.running else None,
            },
            "executor": {
                "state": "busy" if self.running else "idle",
                "pid": 12 if self.running else None,
            },
        }

    async def ensure_stopped(self):
        self.events.append(("stop", self.profile.read_config()))
        if self.fail_stop:
            raise BackendError("offline-old-secret offline-new-secret")
        self.running = False
        return await self.inspect_owned()

    async def ensure_running(self):
        self.events.append(("start", self.profile.read_config()))
        self.running = True
        if self.fail_start:
            self.fail_start -= 1
            raise BackendError("offline-new-secret")
        return await self.inspect_owned()

    @contextlib.contextmanager
    def idle_executor(self):
        assert not self.running
        if self.fail_idle:
            raise BackendError("busy")
        self.events.append(("idle", self.profile.read_config()))
        yield

    async def remove_registration(self):
        self.events.append(("remove", None))


@pytest.fixture
def environment(tmp_path, monkeypatch):
    from insto.desktop import operations

    profile = Profile(tmp_path / "desktop")
    service = Service(profile)

    @contextlib.contextmanager
    def managed(**kwargs):
        assert profile.read_state() is not None
        service.deadline = kwargs["deadline"]
        yield service

    monkeypatch.setattr(operations, "managed_service", managed)
    monkeypatch.setattr(operations, "read_service", lambda *args: service)
    monkeypatch.setattr(operations, "validate_candidate", AsyncMock(return_value=8))
    return profile, service


@pytest.fixture
async def configured(environment):
    from insto.desktop import operations

    profile, service = environment
    await operations.configure(profile, "offline-old-secret")
    service.events.clear()
    return profile, service


@pytest.mark.asyncio
async def test_missing_inspection_has_no_filesystem_effect(tmp_path):
    from insto.desktop import operations

    profile = Profile(tmp_path / "missing")
    assert (await operations.inspect_profile(profile))["status"] == "unconfigured"
    assert not profile.root.exists()


@pytest.mark.asyncio
async def test_invalid_validation_has_no_effect(environment, monkeypatch):
    from insto.desktop import operations

    profile, service = environment
    monkeypatch.setattr(
        operations, "validate_candidate", AsyncMock(side_effect=DesktopError("invalid_token"))
    )
    with pytest.raises(DesktopError, match="invalid_token"):
        await operations.configure(profile, "offline-new-secret")
    assert not profile.root.exists()
    assert not service.events


@pytest.mark.asyncio
async def test_setup_and_identical_resubmit_are_idempotent(configured):
    from insto.desktop import operations

    profile, service = configured
    result = await operations.configure(profile, "offline-old-secret")
    assert result["status"] == "running"
    assert not service.events
    assert profile.read_journal() is None
    with pytest.raises(DesktopError, match="already_configured"):
        await operations.configure(profile, "offline-new-secret")


@pytest.mark.asyncio
async def test_replacement_stops_before_write_and_preserves_watch_data(configured):
    from insto.desktop import operations

    profile, service = configured
    db = profile.home / "store.db"
    with sqlite3.connect(db) as connection:
        connection.execute(
            "INSERT INTO watches VALUES "
            "('alice', 'generation', 900, 12, 'private-error', 2, 'paused')"
        )
    before_db = db.read_bytes()
    old = profile.read_config()
    result = await operations.replace_credentials(profile, "offline-new-secret")
    assert service.events[0] == ("stop", old)
    assert service.events[1] == ("idle", old)
    assert b"offline-new-secret" in service.events[2][1]
    assert db.read_bytes() == before_db
    assert result["status"] == "running"
    for payload in (json.dumps(result), json.dumps(profile.read_state())):
        assert "offline-" not in payload
        assert "private-error" not in payload
    assert profile.read_journal() is None
    assert profile.read_backup() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("running,desired", [(False, "stopped"), (False, "running")])
async def test_replacement_preserves_observed_nonrunning(configured, running, desired):
    from insto.desktop import operations

    profile, service = configured
    service.running = running
    with profile.locked():
        state = profile.read_state()
        state["desired_service"] = desired
        profile.write_state(state)
    await operations.replace_credentials(profile, "offline-new-secret")
    assert not service.running
    assert all(event != "start" for event, _ in service.events)
    assert profile.read_state()["desired_service"] == desired


@pytest.mark.asyncio
async def test_failed_stop_never_publishes_candidate(configured):
    from insto.desktop import operations

    profile, service = configured
    old = profile.read_config()
    service.fail_stop = True
    with pytest.raises(DesktopError):
        await operations.replace_credentials(profile, "offline-new-secret")
    assert profile.read_config() == old


@pytest.mark.asyncio
async def test_failed_candidate_start_stops_candidate_before_restoring_exact_config(configured):
    from insto.desktop import operations

    profile, service = configured
    old = profile.read_config()
    state = profile.read_state()
    service.fail_start = 1
    with pytest.raises(DesktopError, match="service_error"):
        await operations.replace_credentials(profile, "offline-new-secret")
    assert [event for event, _ in service.events] == [
        "stop",
        "idle",
        "start",
        "stop",
        "idle",
        "start",
    ]
    assert b"offline-new-secret" in service.events[3][1]
    assert profile.read_config() == old
    assert profile.read_state() == state
    assert service.running
    assert profile.read_journal() is None


@pytest.mark.asyncio
async def test_setup_failure_keeps_token_and_repair_needs_no_network(environment, monkeypatch):
    from insto.desktop import operations

    profile, service = environment
    service.fail_start = 1
    with pytest.raises(DesktopError):
        await operations.configure(profile, "offline-new-secret")
    assert b"offline-new-secret" in profile.read_config()
    assert profile.read_journal()["kind"] == "setup"
    monkeypatch.setattr(
        operations, "validate_candidate", AsyncMock(side_effect=AssertionError("network"))
    )
    result = await operations.change_service(Profile(profile.root), "repair")
    assert result["status"] == "running"
    assert profile.read_journal() is None


@pytest.mark.asyncio
async def test_zero_quota_is_saved_but_not_green(environment, monkeypatch):
    from insto.desktop import operations

    profile, _ = environment
    monkeypatch.setattr(operations, "validate_candidate", AsyncMock(return_value=0))
    assert (await operations.configure(profile, "offline-new-secret"))[
        "status"
    ] == "quota_exhausted"


@pytest.mark.asyncio
async def test_stop_persists_intent_even_when_native_stop_fails(configured):
    from insto.desktop import operations

    profile, service = configured
    service.fail_stop = True
    with pytest.raises(DesktopError):
        await operations.change_service(profile, "stop")
    assert profile.read_state()["desired_service"] == "stopped"


@pytest.mark.asyncio
async def test_validation_consumes_forward_budget_before_any_mutation(environment, monkeypatch):
    from insto.desktop import operations

    profile, _ = environment
    monkeypatch.setattr(operations, "OPERATION_SECONDS", 0.4)
    monkeypatch.setattr(operations, "ROLLBACK_SECONDS", 0.2)

    async def validate(token):
        await asyncio.sleep(0.25)
        return 8

    monkeypatch.setattr(operations, "validate_candidate", validate)
    with pytest.raises(DesktopError, match="operation_timeout"):
        await operations.configure(profile, "offline-new-secret")
    assert not profile.root.exists()


@pytest.mark.asyncio
async def test_expired_forward_budget_uses_reserved_rollback(configured, monkeypatch):
    from insto.desktop import operations

    profile, service = configured
    old = profile.read_config()
    monkeypatch.setattr(operations, "OPERATION_SECONDS", 1.5)
    monkeypatch.setattr(operations, "ROLLBACK_SECONDS", 1.0)
    original_start = service.ensure_running
    calls = 0

    async def start():
        nonlocal calls
        calls += 1
        if calls == 1:
            service.running = True
            await asyncio.sleep(0.6)
        return await original_start()

    service.ensure_running = start
    with pytest.raises(DesktopError, match="operation_timeout"):
        await operations.replace_credentials(profile, "offline-new-secret")
    assert profile.read_config() == old
    assert profile.read_journal() is None
    assert service.running


@pytest.mark.asyncio
async def test_repeat_cancellation_keeps_profile_lock_until_rollback_drains(configured):
    from insto.desktop import operations

    profile, service = configured
    old = profile.read_config()
    started = asyncio.Event()
    rolling_back = asyncio.Event()
    finish = asyncio.Event()
    original_start, original_stop = service.ensure_running, service.ensure_stopped
    calls = 0

    async def start():
        nonlocal calls
        calls += 1
        if calls == 1:
            service.running = True
            started.set()
            await asyncio.Event().wait()
        return await original_start()

    async def stop():
        if started.is_set():
            rolling_back.set()
            await finish.wait()
        return await original_stop()

    service.ensure_running, service.ensure_stopped = start, stop
    task = asyncio.create_task(operations.replace_credentials(profile, "offline-new-secret"))
    await started.wait()
    task.cancel()
    await rolling_back.wait()
    task.cancel()
    await asyncio.sleep(0)
    with pytest.raises(DesktopError, match="profile_busy"), Profile(profile.root).locked():
        pass
    assert b"offline-new-secret" in profile.read_config()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert profile.read_config() == old
    assert profile.read_journal() is None


@pytest.mark.asyncio
async def test_expired_rollback_keeps_durable_recovery(configured, monkeypatch):
    from insto.desktop import operations

    profile, service = configured
    monkeypatch.setattr(operations, "OPERATION_SECONDS", 0.35)
    monkeypatch.setattr(operations, "ROLLBACK_SECONDS", 0.15)

    async def start():
        service.running = True
        await asyncio.sleep(0.4)
        raise BackendError("late readiness")

    service.ensure_running = start
    with pytest.raises(DesktopError, match="recovery_required"):
        await operations.replace_credentials(profile, "offline-new-secret")
    assert profile.read_journal() is not None
    assert profile.read_backup() is not None


@pytest.mark.asyncio
async def test_mismatched_busy_executor_is_not_reported_healthy(configured):
    from insto.desktop import operations

    profile, service = configured
    report = await service.inspect_owned()
    report["executor"]["pid"] = 99
    service.inspect_owned = AsyncMock(return_value=report)
    assert (await operations.inspect_profile(profile))["status"] != "running"


@pytest.mark.asyncio
async def test_stop_refuses_nonterminal_recovery_without_native_actions(configured):
    from insto.desktop import operations

    profile, service = configured
    state = profile.read_state()
    with profile.locked():
        profile.write_backup(profile.read_config())
        journal = profile.new_journal(
            kind="replace", previous_state=state, previous_running=True, remaining=3
        )
        profile.write_journal(journal)
    with pytest.raises(DesktopError, match="recovery_required"):
        await operations.change_service(profile, "stop")
    assert not service.events
    assert profile.read_state() == state
    assert profile.read_journal() == journal


@pytest.mark.asyncio
async def test_double_submit_from_independent_process_is_serialized(configured):
    from insto.desktop import operations

    profile, _ = configured
    script = r"""
import asyncio, contextlib, sys
from pathlib import Path
from insto.desktop import operations
from insto.desktop.profile import Profile
from tests.test_desktop_operations import Service
profile = Profile(Path(sys.argv[1]))
service = Service(profile)
service.running = True
@contextlib.contextmanager
def managed(**kwargs):
    service.deadline = kwargs["deadline"]
    print("locked", flush=True)
    sys.stdin.read(1)
    yield service
async def validate(token):
    return 8
operations.managed_service = managed
operations.validate_candidate = validate
asyncio.run(operations.replace_credentials(profile, "offline-new-secret"))
"""
    child = subprocess.Popen(
        [sys.executable, "-B", "-c", script, str(profile.root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=5)
        assert child.stdout.readline() == b"locked\n"
        with pytest.raises(DesktopError, match="profile_busy"):
            await operations.replace_credentials(Profile(profile.root), "offline-other-secret")
        _, errors = child.communicate(b"x", timeout=5)
        assert child.returncode == 0, errors.decode()
        assert b"offline-new-secret" in profile.read_config()
        assert profile.read_journal() is None
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


@pytest.mark.asyncio
async def test_resumed_setup_binds_quota_to_current_candidate_before_start_failure(
    environment, monkeypatch
):
    from insto.desktop import operations

    profile, service = environment
    with profile.locked(initialize=True):
        profile.write_journal(
            profile.new_journal(
                kind="setup", previous_state=None, previous_running=False, remaining=99
            )
        )
    monkeypatch.setattr(operations, "validate_candidate", AsyncMock(return_value=0))
    service.fail_start = 1
    with pytest.raises(DesktopError):
        await operations.configure(profile, "offline-new-secret")
    assert profile.read_state()["quota_remaining"] == 0
    assert profile.read_journal()["new_remaining"] == 0


@pytest.mark.asyncio
async def test_null_quota_is_not_exhausted_and_is_reported_null(environment):
    from insto.desktop import operations
    from insto.desktop.configuration import initialize_database

    profile, _ = environment
    with profile.locked(initialize=True):
        profile.write_config(operations.config_bytes(profile, "offline-old-secret"))
        profile.write_state(profile.new_state(remaining=None, desired="stopped"))
        initialize_database(profile.home / "store.db")
    result = await operations.inspect_profile(profile)
    assert result["status"] == "stopped"
    assert result["quota_remaining"] is None and result["quota_checked_at"] is None


def adopted_profile(own, tmp_path):
    """Bind the own profile to a sibling CLI home that holds a hikerapi config."""
    home = tmp_path / "cli-home"
    home.mkdir(mode=0o700)
    original = b'backend = "hikerapi"\n[hikerapi]\ntoken = "offline-cli-secret"\n'
    (home / "config.toml").write_bytes(original)
    (home / "config.toml").chmod(0o600)
    with own.locked(initialize=True):
        own.write_binding(home)
    profile = Profile(own.root, home=home)
    assert profile.adopted
    return profile, original


@pytest.mark.asyncio
@pytest.mark.parametrize("token", ["offline-cli-secret", "offline-new-secret"])
async def test_configure_refuses_an_adopted_home_before_validation(environment, tmp_path, token):
    from insto.desktop import operations

    own, service = environment
    profile, original = adopted_profile(own, tmp_path)
    with pytest.raises(DesktopError, match="already_configured"):
        await operations.configure(profile, token)
    operations.validate_candidate.assert_not_awaited()
    assert not service.events
    assert profile.config.read_bytes() == original
    assert profile.read_state() is None and profile.read_journal() is None


@pytest.mark.asyncio
async def test_inspect_adopted_home_without_state_is_unconfigured(environment, tmp_path):
    from insto.desktop import operations

    own, service = environment
    profile, original = adopted_profile(own, tmp_path)
    result = await operations.inspect_profile(profile)
    assert result["configured"] is False and result["status"] == "unconfigured"
    assert not service.events
    assert profile.config.read_bytes() == original
