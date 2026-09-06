"""Journaled service migration to the current interpreter, and registration removal."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from insto.config import Config
from insto.desktop.errors import DesktopError
from insto.desktop.operations import (
    OPERATION_SECONDS,
    ROLLBACK_SECONDS,
    _config,
    _dto,
    _error,
    _running,
)
from insto.desktop.profile import Profile
from insto.desktop.recovery import (
    RestartFailedError,
    _lease_artifacts,
    checkpoint,
    cleanup,
    finish_terminal,
    phase,
    require_settled,
    rollback_drained,
)
from insto.desktop.service_facts import registration_facts
from insto.exceptions import BackendError
from insto.service import watch_service
from insto.service.watch_service_lifecycle import ManagedService, managed_service


def _settled_state(profile: Profile, deadline: float) -> dict[str, Any]:
    """Refuse pending recovery, finish a terminal journal, and return the profile state."""
    require_settled(profile)
    journal = profile.read_journal()
    if journal is not None:
        # Terminal by require_settled: a death between its last phase and cleanup
        # must not outlive this transition's early returns.
        finish_terminal(profile, journal, deadline)
    state = profile.read_state()
    if state is None:
        raise DesktopError("not_configured")
    return state


async def migrate(profile: Profile) -> dict[str, Any]:
    """Move the owned registration to this interpreter; roll back on any failure."""
    deadline = time.monotonic() + OPERATION_SECONDS
    forward = deadline - ROLLBACK_SECONDS
    try:
        with profile.locked():
            checkpoint(forward)
            state = _settled_state(profile, forward)
            config = _config(profile, deadline=forward)
            with managed_service(home=profile.home, config=config, deadline=forward) as new:
                paths = new._paths
                desired = (new._manifest, new._plist)
                facts = await registration_facts(
                    paths, deadline=forward, expected=json.loads(new._manifest)
                )
                if facts["registration"] == "unknown":
                    raise DesktopError("service_ownership_unknown")
                if facts["registration"] == "none":
                    return _dto(state)
                previous = watch_service.read_registration(paths)
                if previous == desired:
                    return _dto(state, running=_running(await new.inspect_owned()))
                if facts["settings"] != "matching":
                    raise DesktopError("service_config_mismatch")
                old = ManagedService(
                    paths, config, forward, artifacts=_lease_artifacts(paths, previous, previous)
                )
                previous_running = _running(await old.inspect_owned())
                journal = profile.new_journal(
                    kind="migrate",
                    previous_state=state,
                    previous_running=previous_running,
                    remaining=None,
                )
                checkpoint(forward)
                try:
                    if watch_service.read_retained_registration(paths) is not None:
                        # Retained by a run that died before journaling: unreferenced
                        # (require_settled proved there is no journal).
                        watch_service.discard_retained_registration(paths)
                except BackendError:
                    # An unreadable retained document is what inspect_profile
                    # reports as pending; Repair settles it, a migration does not.
                    raise DesktopError("recovery_required") from None
                watch_service.retain_registration(paths, previous=previous, candidate=desired)
                checkpoint(forward)
                profile.write_journal(journal)
                running = False
                try:
                    await old.ensure_stopped()
                    phase(profile, journal, "stopped", forward)
                    with new.idle_executor():
                        checkpoint(forward)
                        watch_service.replace_registration(paths, previous, desired)
                    phase(profile, journal, "published", forward)
                    running = state["desired_service"] == "running"
                    if running:
                        await new.ensure_running()
                        phase(profile, journal, "started", forward)
                    phase(profile, journal, "committed", forward)
                except BaseException as exc:
                    try:
                        await rollback_drained(profile, new, deadline)
                    except BaseException as failure:
                        if not isinstance(exc, Exception):
                            # Cancellation or an interpreter interrupt, not an error:
                            # it propagates as itself; the journal awaits Repair.
                            raise exc from None
                        if isinstance(failure, RestartFailedError):
                            # Rolled back completely; only the previous registration
                            # did not restart. Reported as service_error, retryable.
                            raise failure from None
                        raise DesktopError("recovery_required") from None
                    raise
                watch_service.discard_retained_registration(paths)
                cleanup(profile, forward)
                return _dto(state, running=running)
    except Exception as exc:
        raise _error(exc, forward) from None


def _manifest_config(manifest: dict[str, Any]) -> Config:
    """A lease configuration from the registration's own pins: removal needs no credentials."""
    return Config(
        backend=str(manifest["backend"]),
        db_path=Path(manifest["db_path"]),
        output_dir=Path(manifest["output_dir"]),
        aiograpi_session_path=Path(manifest["aiograpi_session_path"]),
    )


async def uninstall(profile: Profile) -> dict[str, Any]:
    """Remove the exact owned registration; config, database and state stay."""
    deadline = time.monotonic() + OPERATION_SECONDS
    try:
        with profile.locked():
            checkpoint(deadline)
            state = dict(_settled_state(profile, deadline), desired_service="stopped")
            paths = watch_service.service_paths(profile.home)
            if not os.path.lexists(paths.manifest):
                # Without a manifest no lease can be built; a plist-only or loaded
                # job is still refused, read-only, exactly as the facts classify it.
                facts = await registration_facts(paths, deadline=deadline)
                if facts["registration"] != "none":
                    raise DesktopError("service_ownership_unknown")
                profile.write_state(state)
                return _dto(state)
            try:
                unlocked = watch_service.read_private_file(paths.manifest)
                config = _manifest_config(watch_service.parse_manifest(paths, unlocked))
            except BackendError:
                # A manifest that cannot prove ownership is what the facts call unknown.
                raise DesktopError("service_ownership_unknown") from None
            with managed_service(home=profile.home, config=config, deadline=deadline):
                # Verdict and removal share the management lock: the registration
                # cannot change hands between the ownership check and the unlink.
                on_disk = watch_service.read_registration(paths)
                if on_disk[0] != unlocked:
                    # Re-registered between the unlocked read and the lock: the lease
                    # was derived from bytes that are gone. Refused before any native
                    # action and before intent is persisted.
                    raise DesktopError("service_ownership_unknown")
                facts = await registration_facts(paths, deadline=deadline)
                if facts["registration"] != "owned":
                    raise DesktopError("service_ownership_unknown")
                checkpoint(deadline)
                profile.write_state(state)
                registered = ManagedService(
                    paths, config, deadline, artifacts=_lease_artifacts(paths, on_disk, on_disk)
                )
                await registered.ensure_stopped()
                await registered.remove_registration()
            return _dto(state)
    except Exception as exc:
        raise _error(exc, deadline) from None
