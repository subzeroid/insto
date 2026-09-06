"""Read-only inspection of an external CLI home and explicit adoption."""

from __future__ import annotations

import os
import time
import tomllib
from pathlib import Path
from typing import Any

from insto.config import BACKEND_HIKERAPI, Config, normalize_backend
from insto.desktop.access import validate_token
from insto.desktop.configuration import check_database, initialize_database, parse_profile_config
from insto.desktop.errors import DesktopError
from insto.desktop.operations import OPERATION_SECONDS, _config, _dto, _error, _running
from insto.desktop.profile import Profile, _directory, _file, _trusted_ancestors
from insto.desktop.recovery import _lease_artifacts, checkpoint, finish_terminal, require_settled
from insto.desktop.service_facts import _owned_files, registration_facts
from insto.exceptions import BackendError
from insto.service import watch_service
from insto.service.watch_service_lifecycle import ManagedService, managed_service
from insto.service.watch_service_runner import load_home_config

_BACKENDS = {"hikerapi", "aiograpi", "fake"}
_FACT_KEYS = ("registration", "interpreter", "loaded", "process")


def _private_home(path: Path) -> bool:
    """A real, owned, 0700 directory under trusted ancestors: what Profile requires."""
    try:
        _trusted_ancestors(path)
        _directory(path)
    except DesktopError:
        return False
    return True


def _config_report(path: Path) -> tuple[str, str | None, Config | None]:
    config_path = path / "config.toml"
    if not os.path.lexists(config_path):
        return "missing", None, None
    try:
        _file(config_path.lstat())  # exactly what Profile.read_config will accept later
        data = tomllib.loads(watch_service.read_private_file(config_path).decode("utf-8"))
        named = data.get("backend")
        if isinstance(named, str) and normalize_backend(named) != BACKEND_HIKERAPI:
            # Named before the runner's credential checks, like parse_profile_config:
            # an aiograpi home without credentials is unsupported here, not invalid.
            backend = normalize_backend(named)
            return "ok", backend if backend in _BACKENDS else None, None
        config = load_home_config(path, data)
    except (DesktopError, BackendError, TypeError, ValueError, OSError):
        return "invalid", None, None
    token = config.hiker_token
    if not isinstance(token, str):
        return "invalid", config.backend, None
    try:
        validate_token(token)
    except DesktopError:
        return "invalid", config.backend, None
    return "ok", config.backend, config


def _database_report(db_path: Path, deadline: float) -> str:
    try:
        return "ok" if check_database(db_path, deadline=deadline) else "missing"
    except DesktopError as error:
        if error.code == "operation_timeout":
            raise
        return "schema_mismatch" if error.code == "schema_mismatch" else "unreadable"


def _reason(report: dict[str, Any]) -> str | None:
    if not report["private"] or report["config"] != "ok":
        return "home_invalid"
    if report["backend"] != BACKEND_HIKERAPI:
        return "home_backend_unsupported"
    if report["database"] == "schema_mismatch":
        return "schema_mismatch"
    if report["database"] == "unreadable":
        return "storage_error"
    return None


async def _inspect(path: Path, *, deadline: float) -> tuple[dict[str, Any], Config | None]:
    checkpoint(deadline)
    report: dict[str, Any] = {
        "path": str(path),
        "exists": False,
        "private": False,
        "config": "missing",
        "backend": None,
        "database": "missing",
        "registration": "none",
        "interpreter": None,
        "loaded": None,
        "process": "unknown",
        "adoptable": False,
        "reason": "home_invalid",
    }
    if not os.path.lexists(path):
        return report, None
    report["exists"] = True
    if not _private_home(path):
        # Nothing inside a non-private path (or one under untrusted parents) is read.
        report.update(config="invalid", database="unreadable", registration="unknown")
        return report, None
    report["private"] = True
    report["config"], report["backend"], config = _config_report(path)
    db_path = config.db_path if config is not None else path / "store.db"
    report["database"] = _database_report(db_path, deadline)
    facts = await registration_facts(watch_service.service_paths(path), deadline=deadline)
    report.update({key: facts[key] for key in _FACT_KEYS})
    report["reason"] = _reason(report)
    report["adoptable"] = report["reason"] is None
    checkpoint(deadline)
    return report, config


async def inspect(path: Path, *, deadline: float) -> dict[str, Any]:
    """Report on an external home without creating, changing or locking anything."""
    report, _ = await _inspect(path, deadline=deadline)
    return report


def _current(own: Profile) -> Profile | None:
    """The bound profile, or None when the binding or its home is unusable."""
    try:
        binding = own.read_binding()
        if binding is None:
            return own
        current = Profile(own.root, home=Path(binding["home"]))
        current.read_state()  # proves the adopted home still exists and is private
        return current
    except DesktopError as error:
        if error.code in {"home_invalid", "profile_ownership"}:
            return None
        raise


async def _profile_dto(profile: Profile, config: Config | None, deadline: float) -> dict[str, Any]:
    """The C1 DTO: `service_running` only from the lifecycle's validated report."""
    state = profile.read_state()
    if state is None:
        return _dto(None)
    if config is None:
        return _dto(state)
    paths = watch_service.service_paths(profile.home)
    lease: ManagedService | None = None
    try:
        on_disk = watch_service.read_registration(paths)
        if on_disk == (None, None):
            return _dto(state)
        if _owned_files(paths)[0] is None:
            # Files that do not prove insto ownership of this home's registration are
            # nothing the desktop may lease, whatever launchd says about the label.
            raise BackendError("watch service registration ownership is unknown")
        lease = ManagedService(
            paths, config, deadline, artifacts=_lease_artifacts(paths, on_disk, on_disk)
        )
        return _dto(state, running=_running(await lease.inspect_owned()))
    except (BackendError, DesktopError):
        result = _dto(state)
        result["status"] = "service_error"
        return result
    finally:
        if lease is not None:
            lease._active = False


async def _stop_own(own: Profile, deadline: float) -> None:
    """Stop the desktop-owned service of the own profile before switching away."""
    state = own.read_state()
    if state is None:
        return
    config = _config(own, deadline=deadline)
    paths = watch_service.service_paths(own.home)
    with managed_service(home=own.home, config=config, deadline=deadline):
        facts = await registration_facts(paths, deadline=deadline)
        if facts["registration"] == "unknown":
            raise DesktopError("service_ownership_unknown")
        if facts["registration"] == "owned":
            on_disk = watch_service.read_registration(paths)
            registered = ManagedService(
                paths, config, deadline, artifacts=_lease_artifacts(paths, on_disk, on_disk)
            )
            try:
                await registered.ensure_stopped()
            finally:
                registered._active = False
    if state["desired_service"] == "running":
        checkpoint(deadline)
        own.write_state(dict(state, desired_service="stopped"))


def _prepare_directories(target: Profile) -> None:
    """The service controller needs these for every read and mutation; the CLI's
    install creates the same ones."""
    paths = watch_service.service_paths(target.home)
    try:
        for directory in (paths.home / "services", paths.directory, paths.log_dir):
            watch_service._private_directory(directory)
    except BackendError:
        # An unsafe existing directory inside the home: storage, not the service.
        raise DesktopError("storage_error") from None


def _finish_terminal_journal(own: Profile, current: Profile, deadline: float) -> None:
    """Complete a committed/rolled_back journal left by a death before cleanup.

    The journal is read under the lease it is finished under: for the own profile
    the root lock already held, for an adopted current profile its `shared_lease`.
    A pending journal that appeared since the unlocked `require_settled` is refused.
    """
    if current.adopted:
        with current.shared_lease(own):
            _settle(current, deadline)
    else:
        _settle(own, deadline)


def _settle(profile: Profile, deadline: float) -> None:
    """Under `profile`'s lease: refuse pending recovery, finish a terminal journal."""
    require_settled(profile)
    journal = profile.read_journal()
    if journal is not None:
        finish_terminal(profile, journal, deadline)


def _own_config(own: Profile, deadline: float) -> Config | None:
    try:
        return _config(own, deadline=deadline)
    except DesktopError:
        return None


def _adopted_config(current: Profile) -> Config | None:
    """The bound home's configuration for a DTO; nothing is read from launchd for it."""
    payload = current.read_config()
    if payload is None:
        return None
    try:
        return parse_profile_config(current, payload)
    except DesktopError:
        return None


async def select(profile: Profile, path: Path | None) -> dict[str, Any]:
    """Bind the desktop to an adopted home, or back to its own profile, under the root lock."""
    deadline = time.monotonic() + OPERATION_SECONDS
    own = Profile(profile.root)
    try:
        with own.locked(initialize=True, verify_binding=False):
            checkpoint(deadline)
            current = _current(own)
            if current is not None:
                require_settled(current)
                _finish_terminal_journal(own, current, deadline)
                same_own = path is None and not current.adopted
                same_adopted = path is not None and current.adopted and current.home == path
                if same_own or same_adopted:
                    config = (
                        _adopted_config(current)
                        if current.adopted
                        else _own_config(current, deadline)
                    )
                    return await _profile_dto(current, config, deadline)
            target = own if path is None else Profile(own.root, home=path)
            adoption: tuple[dict[str, Any], Config] | None = None
            if path is not None:
                report, config = await _inspect(path, deadline=deadline)
                if not report["adoptable"] or config is None:
                    raise DesktopError(report["reason"] or "home_invalid")
                require_settled(target)
                target.read_state()
                adoption = (report, config)
            checkpoint(deadline)
            if adoption is None:
                own.remove_binding()
                return await _profile_dto(own, _own_config(own, deadline), deadline)
            report, config = adoption
            # Everything that can refuse happens before the own service is stopped: the
            # target's home lock, service directories and database come first. Lock
            # order is root lock → target home lock → own management lock (acyclic).
            with target.shared_lease(own):
                # The unlocked check above admitted the target; a journal written by
                # another root since is re-checked under the home's own lock.
                _settle(target, deadline)
                _prepare_directories(target)
                if report["database"] == "missing":
                    checkpoint(deadline)
                    initialize_database(
                        config.db_path, deadline=deadline, stage_dir=config.db_path.parent
                    )
                if current is None or not current.adopted:
                    # Leaving the own profile (a broken binding counts as it): at most one
                    # desktop-managed service may run after selection. Returning to the
                    # own profile never touches a service, like leaving an adopted one.
                    await _stop_own(own, deadline)
                # The new home's state is durable before the binding points at it: a
                # crash in between leaves a stale state file, never a bound home without
                # intent; a refused stop leaves no state file behind at all.
                if target.read_state() is None:
                    desired = (
                        "running"
                        if report["loaded"] and report["process"] == "running"
                        else "stopped"
                    )
                    checkpoint(deadline)
                    target.write_state(target.new_state(remaining=None, desired=desired))
            checkpoint(deadline)
            own.write_binding(target.home)
            return await _profile_dto(target, config, deadline)
    except Exception as exc:
        raise _error(exc, deadline) from None
