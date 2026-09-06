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
from insto.exceptions import BackendError
from insto.service import watch_service
from insto.service.watch_service import Registration, ServicePaths
from insto.service.watch_service_lifecycle import ManagedService

TERMINAL_PHASES = frozenset({"committed", "rolled_back"})


class RestartFailedError(BackendError):
    """A rollback completed, but the restored registration could not be started.

    Bytes, intent and journal are settled; only the previous service is down. Callers
    report it as `service_error` instead of holding the profile in recovery.
    """


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
    if journal is not None and journal["phase"] not in TERMINAL_PHASES:
        raise DesktopError("recovery_required")
    if journal is None and profile.read_backup() is not None:
        raise DesktopError("recovery_required")


def finish_terminal(profile: Profile, journal: dict[str, Any], deadline: float) -> None:
    """Complete a journal that reached a terminal phase before its cleanup ran.

    The phase is checked here, under the lease the caller holds: a journal read
    before the lease may have been replaced by a pending one since.
    """
    if journal["phase"] not in TERMINAL_PHASES:
        raise DesktopError("recovery_required")
    checkpoint(deadline)
    if journal["kind"] == "migrate":
        try:
            watch_service.discard_retained_registration(watch_service.service_paths(profile.home))
        except BackendError:
            # A retained document the service layer refuses to touch keeps the
            # journal: Repair reports it, exactly like inspect_profile does.
            raise DesktopError("recovery_required") from None
    cleanup(profile, deadline)


def _discard_unreferenced(profile: Profile) -> bool:
    """Drop a retained registration that no journal references; True when one was dropped.

    The document sits beside the home's manifest, so locating it needs nothing from
    the lease, whose own paths derive from the same home. A private file that is not
    a retained registration can never serve a rollback; without a journal it is
    dropped too, so one Repair settles it (a file the service layer refuses to read
    at all stays, and Repair keeps reporting it).
    """
    paths = watch_service.service_paths(profile.home)
    try:
        if watch_service.read_retained_registration(paths) is None:
            return False
    except BackendError:
        pass
    watch_service.discard_retained_registration(paths)
    return True


async def reconcile(profile: Profile, service: ManagedService, deadline: float) -> bool:
    """Finish terminal cleanup or undo an uncommitted credential replacement or migration.

    Caller retains both locks. Phase labels never prove that a candidate process
    is absent: stop and executor exclusion precede every old-config publication.
    """
    checkpoint(deadline)
    journal = profile.read_journal()
    backup = profile.read_backup()
    if journal is None:
        changed = False
        if backup is not None:
            if backup != profile.read_config():
                raise DesktopError("recovery_required")
            profile.remove_backup()
            changed = True
        # Retained before its journal was written: nothing references it. One
        # Repair settles it together with a stray backup.
        if _discard_unreferenced(profile):
            changed = True
        return changed
    if journal["phase"] in TERMINAL_PHASES:
        finish_terminal(profile, journal, deadline)
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
    if journal["previous_running"] and previous[1] is None:
        # The lifecycle refuses a loaded job whose plist is missing, so a running
        # previous registration always retained its real plist: the restart below
        # never starts a derived one. Unreachable by construction; refused untouched.
        raise DesktopError("recovery_required")
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
    try:
        if journal["previous_running"]:
            old = ManagedService(
                paths,
                service._config,
                deadline,
                artifacts=_lease_artifacts(paths, previous, previous),
            )
            await old.ensure_running()
    except BackendError as exc:
        # Bytes and intent are already the previous ones. A previous registration
        # that will not start (its interpreter may be gone for good) must not hold
        # the journal at `rollback` forever: the rollback completes and the restart
        # failure is reported on its own.
        _finish_rollback(profile, paths, journal, deadline)
        raise RestartFailedError("the restored watch service registration did not start") from exc
    _finish_rollback(profile, paths, journal, deadline)


def _finish_rollback(
    profile: Profile, paths: ServicePaths, journal: dict[str, Any], deadline: float
) -> None:
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
