"""Token-only desktop setup, credential transactions and safe inspection DTOs."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from insto.config import Config
from insto.desktop.access import validate_candidate
from insto.desktop.configuration import (
    check_database,
    config_bytes,
    config_payload,
    parse_profile_config,
)
from insto.desktop.errors import DesktopError
from insto.desktop.profile import Profile
from insto.desktop.recovery import checkpoint, cleanup, phase, reconcile, rollback_drained
from insto.desktop.service_facts import registration_facts
from insto.exceptions import BackendError
from insto.service import watch_service
from insto.service.watch_service_lifecycle import ManagedService, managed_service

OPERATION_SECONDS = 120.0
ROLLBACK_SECONDS = 35.0
INSPECT_SECONDS = 10.0


def read_service(profile: Profile, config: Config, deadline: float) -> ManagedService:
    watch_service._require_macos()
    return ManagedService(watch_service.service_paths(profile.home), config, deadline)


def _running(report: dict[str, Any]) -> bool:
    process, executor = report["process"], report["executor"]
    if executor["state"] == "busy" and (
        process["state"] != "running"
        or type(process["pid"]) is not int
        or process["pid"] <= 0
        or type(executor["pid"]) is not int
        or process["pid"] != executor["pid"]
    ):
        raise BackendError("watch executor ownership is not confirmed")
    return bool(process["state"] == "running" and executor["state"] == "busy")


def _dto(
    state: dict[str, Any] | None, *, running: bool = False, pending: bool = False
) -> dict[str, Any]:
    status = "unconfigured"
    if state is not None:
        status = "running" if running else "stopped"
        if state["quota_remaining"] == 0:
            status = "quota_exhausted"
    if pending:
        status = "recovery_required"
    return {
        "configured": state is not None,
        "status": status,
        "desired_service": state["desired_service"] if state else None,
        "service_running": running,
        "quota_remaining": state["quota_remaining"] if state else None,
        "quota_checked_at": state["quota_checked_at"] if state else None,
        "revision": state["revision"] if state else None,
    }


async def inspect_profile(profile: Profile) -> dict[str, Any]:
    deadline = time.monotonic() + OPERATION_SECONDS
    state = profile.read_state()
    journal = profile.read_journal()
    backup = profile.read_backup()
    payload = profile.read_config()
    if journal is not None or backup is not None:
        return _dto(state, pending=True)
    if state is None:
        if not profile.adopted and profile.home.exists() and any(profile.home.iterdir()):
            raise DesktopError("profile_ownership")
        return _dto(None)
    if payload is None:
        raise DesktopError("recovery_required")
    config = parse_profile_config(profile, payload)
    if not check_database(config.db_path, deadline=deadline):
        return _dto(state, pending=True)
    service = None
    try:
        service = read_service(profile, config, deadline)
        return _dto(state, running=_running(await service.inspect_owned()))
    except BackendError:
        result = _dto(state)
        result["status"] = "service_error"
        return result
    finally:
        if service is not None:
            service._active = False


async def inspect_service(profile: Profile) -> dict[str, Any]:
    """Registration facts for the current profile; a read that never touches launchd state."""
    deadline = time.monotonic() + INSPECT_SECONDS
    if profile.read_state() is None:
        return {
            "registration": "none",
            "interpreter": None,
            "interpreter_exists": None,
            "loaded": None,
            "settings": None,
        }
    paths = watch_service.service_paths(profile.home)
    expected: dict[str, Any] | None = None
    payload = profile.read_config()
    if payload is not None:
        try:
            config = parse_profile_config(profile, payload)
            expected = json.loads(watch_service._desired(paths, config, None)[0])
        except DesktopError:
            expected = None
    facts = await registration_facts(paths, deadline=deadline, expected=expected)
    return {
        key: facts[key]
        for key in ("registration", "interpreter", "interpreter_exists", "loaded", "settings")
    }


def _config(profile: Profile, token: str | None = None, *, deadline: float) -> Config:
    payload = profile.read_config()
    if payload is None:
        if token is None:
            raise DesktopError("not_configured")
        return parse_profile_config(profile, config_bytes(profile, token))
    config = parse_profile_config(profile, payload)
    exists = check_database(config.db_path, deadline=deadline)
    journal = profile.read_journal()
    if not exists and (journal is None or journal["kind"] != "setup"):
        raise DesktopError("schema_mismatch")
    return config


async def _recover(profile: Profile, service: ManagedService, deadline: float) -> bool:
    try:
        return await reconcile(profile, service, deadline)
    except asyncio.CancelledError:
        raise
    except DesktopError as exc:
        if exc.code == "service_ownership_unknown":
            # Registration bytes that no journaled migration wrote: Repair refuses
            # to touch them, and that verdict must reach the user as itself.
            raise
        raise DesktopError("recovery_required") from None
    except Exception:
        raise DesktopError("recovery_required") from None


def _error(exc: Exception, deadline: float) -> DesktopError:
    if isinstance(exc, DesktopError):
        return exc
    if time.monotonic() >= deadline:
        return DesktopError("operation_timeout")
    return DesktopError("service_error" if isinstance(exc, BackendError) else "storage_error")


async def configure(profile: Profile, token: str) -> dict[str, Any]:
    if profile.adopted:
        # Setup never runs on an adopted home: its credentials already exist and
        # are replaced through credentials.replace. Refused before validation.
        raise DesktopError("already_configured")
    return await _credentials(profile, token, replace=False)


async def replace_credentials(profile: Profile, token: str) -> dict[str, Any]:
    return await _credentials(profile, token, replace=True)


async def _credentials(profile: Profile, token: str, *, replace: bool) -> dict[str, Any]:
    deadline = time.monotonic() + OPERATION_SECONDS
    forward = deadline - ROLLBACK_SECONDS
    remaining = await validate_candidate(token)
    checkpoint(forward)
    try:
        with profile.locked(initialize=not replace):
            checkpoint(forward)
            if profile.read_state() is None:
                if replace and profile.read_journal() is None:
                    raise DesktopError("not_configured")
                journal = profile.read_journal()
                if journal is None:
                    journal = profile.new_journal(
                        kind="setup",
                        previous_state=None,
                        previous_running=False,
                        remaining=remaining,
                    )
                    profile.write_journal(journal)
                if journal["kind"] != "setup":
                    raise DesktopError("recovery_required")
                if profile.read_config() is None:
                    checkpoint(forward)
                    journal = dict(journal, new_remaining=remaining)
                    profile.write_journal(journal)
                    checkpoint(forward)
                    profile.write_config(config_bytes(profile, token))
                checkpoint(forward)
                profile.write_state(
                    profile.new_state(remaining=journal["new_remaining"], desired="running")
                )
            config = _config(profile, None if replace else token, deadline=forward)
            with managed_service(home=profile.home, config=config, deadline=forward) as service:
                await _recover(profile, service, forward)
                state = profile.read_state()
                payload = profile.read_config()
                report = await service.inspect_owned()
                checkpoint(forward)
                if state is not None:
                    assert payload is not None
                    current = parse_profile_config(profile, payload)
                    if current.hiker_token == token:
                        state = dict(
                            state, quota_remaining=remaining, quota_checked_at=int(time.time())
                        )
                        profile.write_state(state)
                        return _dto(state, running=_running(report))
                    if not replace:
                        raise DesktopError("already_configured")
                    return await _replace(
                        profile,
                        service,
                        payload,
                        state,
                        token,
                        remaining,
                        _running(report),
                        forward,
                        deadline,
                    )
                raise DesktopError("not_configured")
    except Exception as exc:
        raise _error(exc, forward) from None


async def _replace(
    profile: Profile,
    service: ManagedService,
    old: bytes,
    state: dict[str, Any],
    token: str,
    remaining: int,
    previous_running: bool,
    forward: float,
    deadline: float,
) -> dict[str, Any]:
    journal = profile.new_journal(
        kind="replace", previous_state=state, previous_running=previous_running, remaining=remaining
    )
    checkpoint(forward)
    profile.write_backup(old)
    checkpoint(forward)
    profile.write_journal(journal)
    try:
        await service.ensure_stopped()
        phase(profile, journal, "stopped", forward)
        with service.idle_executor():
            checkpoint(forward)
            profile.write_config(config_payload(profile, old, token))
            phase(profile, journal, "written", forward)
        if previous_running:
            await service.ensure_running()
        checkpoint(forward)
        new_state = profile.new_state(remaining=remaining, desired=state["desired_service"])
        profile.write_state(new_state)
        phase(profile, journal, "committed", forward)
    except BaseException as exc:
        try:
            await rollback_drained(profile, service, deadline)
        except BaseException:
            if isinstance(exc, asyncio.CancelledError):
                raise exc from None
            raise DesktopError("recovery_required") from None
        raise
    cleanup(profile, forward)
    return _dto(new_state, running=previous_running)


async def change_service(profile: Profile, action: str) -> dict[str, Any]:
    deadline = time.monotonic() + OPERATION_SECONDS
    if action not in {"start", "stop", "repair"}:
        raise DesktopError("invalid_params")
    try:
        with profile.locked():
            checkpoint(deadline)
            journal = profile.read_journal()
            if (
                action == "stop"
                and journal is not None
                and journal["phase"] not in {"committed", "rolled_back"}
            ):
                raise DesktopError("recovery_required")
            if journal is not None and journal["kind"] == "setup" and profile.read_config() is None:
                if any(path != profile.recovery for path in profile.home.iterdir()):
                    raise DesktopError("recovery_required")
                profile.remove_journal()
                profile.remove_state()
                return _dto(None)
            if journal is not None and journal["kind"] == "setup" and profile.read_state() is None:
                profile.write_state(
                    profile.new_state(remaining=journal["new_remaining"], desired="running")
                )
            config = _config(profile, deadline=deadline)
            with managed_service(home=profile.home, config=config, deadline=deadline) as service:
                recovered = await _recover(profile, service, deadline)
                state = profile.read_state()
                if state is None:
                    raise DesktopError("not_configured")
                if recovered and action == "repair":
                    try:
                        return _dto(state, running=_running(await service.inspect_owned()))
                    except BackendError:
                        # A rolled-back migration restored a registration that names
                        # another interpreter; the repair succeeded, the lease cannot
                        # manage that registration. Same shape as inspect_profile.
                        result = _dto(state)
                        result["status"] = "service_error"
                        return result
                checkpoint(deadline)
                if action in {"start", "stop"}:
                    state = dict(
                        state, desired_service="running" if action == "start" else "stopped"
                    )
                    profile.write_state(state)
                report = await (
                    service.ensure_running()
                    if state["desired_service"] == "running"
                    else service.ensure_stopped()
                )
                return _dto(state, running=_running(report))
    except Exception as exc:
        raise _error(exc, deadline) from None
