"""Durable journal reconciliation under the profile and service leases."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from insto.desktop.configuration import initialize_database, parse_profile_config
from insto.desktop.errors import DesktopError
from insto.desktop.profile import Profile
from insto.service.watch_service_lifecycle import ManagedService


def checkpoint(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise DesktopError("operation_timeout")


def phase(profile: Profile, journal: dict[str, Any], name: str, deadline: float) -> None:
    checkpoint(deadline)
    journal["phase"] = name
    profile.write_journal(journal)


def cleanup(profile: Profile, deadline: float) -> None:
    checkpoint(deadline)
    profile.remove_backup()
    checkpoint(deadline)
    profile.remove_journal()


async def reconcile(profile: Profile, service: ManagedService, deadline: float) -> bool:
    """Finish terminal cleanup or undo an uncommitted credential replacement.

    Caller retains both locks. Phase labels never prove that a candidate process
    is absent: stop and executor exclusion precede every old-config publication.
    """
    checkpoint(deadline)
    journal = profile.read_journal()
    backup = profile.read_backup()
    if journal is None:
        if backup is not None:
            if backup != profile.read_config():
                raise DesktopError("recovery_required")
            profile.remove_backup()
            return True
        return False
    if journal["phase"] in {"committed", "rolled_back"}:
        cleanup(profile, deadline)
        return True
    if journal["kind"] == "setup":
        payload = profile.read_config()
        if payload is None:
            raise DesktopError("recovery_required")
        config = parse_profile_config(profile, payload)
        if profile.read_state() is None:
            checkpoint(deadline)
            profile.write_state(
                profile.new_state(remaining=journal["new_remaining"], desired="running")
            )
        checkpoint(deadline)
        initialize_database(config.db_path, deadline=deadline)
        state = profile.read_state()
        assert state is not None
        if state["desired_service"] == "running":
            await service.ensure_running()
        else:
            await service.ensure_stopped()
        phase(profile, journal, "committed", deadline)
        cleanup(profile, deadline)
        return True
    if backup is None:
        raise DesktopError("recovery_required")
    parse_profile_config(profile, backup)
    phase(profile, journal, "rollback", deadline)
    await service.ensure_stopped()
    with service.idle_executor():
        checkpoint(deadline)
        profile.write_config(backup)
        checkpoint(deadline)
        profile.write_state(journal["previous_state"])
    if journal["previous_running"]:
        await service.ensure_running()
    phase(profile, journal, "rolled_back", deadline)
    cleanup(profile, deadline)
    return True


async def rollback_drained(profile: Profile, service: ManagedService, deadline: float) -> None:
    """Repeated cancellation cannot release the locks while rollback is active."""
    service.deadline = deadline
    worker = asyncio.create_task(reconcile(profile, service, deadline))
    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            continue
    worker.result()
