"""Durable journal reconciliation under the profile and service leases."""

from __future__ import annotations

import asyncio
import json
import plistlib
import sys
import time
from typing import Any

from insto.desktop.configuration import initialize_database, parse_profile_config
from insto.desktop.errors import DesktopError
from insto.desktop.profile import Profile
from insto.service import watch_service
from insto.service.watch_service import Registration, ServicePaths
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


def require_settled(profile: Profile) -> None:
    """Refuse a new transition while a journal or stray backup awaits Repair."""
    journal = profile.read_journal()
    if journal is not None and journal["phase"] not in {"committed", "rolled_back"}:
        raise DesktopError("recovery_required")
    if journal is None and profile.read_backup() is not None:
        raise DesktopError("recovery_required")


async def reconcile(profile: Profile, service: ManagedService, deadline: float) -> bool:
    """Finish terminal cleanup or undo an uncommitted credential replacement or migration.

    Caller retains both locks. Phase labels never prove that a candidate process
    is absent: stop and executor exclusion precede every old-config publication.
    """
    checkpoint(deadline)
    journal = profile.read_journal()
    backup = profile.read_backup()
    # A retained registration sits beside the home's manifest; locating it needs
    # nothing from the lease, whose own paths derive from the same home.
    paths = watch_service.service_paths(profile.home)
    if journal is None:
        if backup is not None:
            if backup != profile.read_config():
                raise DesktopError("recovery_required")
            profile.remove_backup()
            return True
        if watch_service.read_retained_registration(paths) is not None:
            # Retained before its journal was written: nothing references it.
            watch_service.discard_retained_registration(paths)
            return True
        return False
    if journal["phase"] in {"committed", "rolled_back"}:
        if journal["kind"] == "migrate":
            watch_service.discard_retained_registration(paths)
        cleanup(profile, deadline)
        return True
    if journal["kind"] == "migrate":
        await _rollback_migration(profile, service, journal, deadline)
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


def _lease_artifacts(
    paths: ServicePaths, on_disk: Registration, fill: Registration
) -> tuple[bytes, bytes]:
    """Bytes for a lease over what is on disk.

    Absent components borrow `fill`, so the lease's artifact check (present files
    only) and its never-loaded plist stay consistent with each other.
    """
    manifest = on_disk[0] if on_disk[0] is not None else fill[0]
    plist = on_disk[1] if on_disk[1] is not None else fill[1]
    if manifest is None:
        raise DesktopError("service_ownership_unknown")
    if plist is None:
        document = watch_service._plist_document(
            paths, json.loads(manifest), dont_write_bytecode=sys.dont_write_bytecode
        )
        plist = plistlib.dumps(document, fmt=plistlib.FMT_XML, sort_keys=True)
    return manifest, plist


async def _rollback_migration(
    profile: Profile, service: ManagedService, journal: dict[str, Any], deadline: float
) -> None:
    """Restore the retained registration; never completes a migration forward.

    Only bytes recorded by this migration may be touched: each on-disk component must
    be the previous or the candidate version (a mixed pair is the legitimate state after
    a death between the two file replacements). Anything else is refused untouched.
    """
    paths = service._paths
    retained = watch_service.read_retained_registration(paths)
    if retained is None:
        raise DesktopError("recovery_required")
    previous, candidate = retained["previous"], retained["candidate"]
    on_disk = watch_service.read_registration(paths)
    for index in (0, 1):
        if on_disk[index] not in {previous[index], candidate[index]}:
            raise DesktopError("service_ownership_unknown")
    phase(profile, journal, "rollback", deadline)
    current = ManagedService(
        paths, service._config, deadline, artifacts=_lease_artifacts(paths, on_disk, previous)
    )
    await current.ensure_stopped()
    if on_disk != previous:
        with current.idle_executor():
            checkpoint(deadline)
            watch_service.replace_registration(paths, on_disk, previous)
    checkpoint(deadline)
    profile.write_state(journal["previous_state"])
    if journal["previous_running"]:
        old = ManagedService(
            paths, service._config, deadline, artifacts=_lease_artifacts(paths, previous, previous)
        )
        await old.ensure_running()
    phase(profile, journal, "rolled_back", deadline)
    watch_service.discard_retained_registration(paths)
    cleanup(profile, deadline)


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
