"""Read-only launchd registration facts; never mutates files or jobs."""

from __future__ import annotations

import os
import plistlib
import sys
import time
from pathlib import Path
from typing import Any

from insto.desktop.errors import DesktopError
from insto.exceptions import BackendError
from insto.service import watch_service
from insto.service.watch_service import ServicePaths
from insto.service.watch_service_lifecycle import _outer_fields

_SETTINGS = ("backend", "db_path", "output_dir", "aiograpi_session_path", "env_file")
_PROCESS_STATES = {
    "running": "running",
    "xpcproxy": "running",  # starting
    None: "stopped",
    "waiting": "stopped",
    "not running": "stopped",
    "exited": "stopped",
    "SIGTERMed": "stopped",
}


def manifest_settings(manifest: dict[str, Any]) -> dict[str, Any]:
    """The operational pins a migration must keep byte-for-byte."""
    return {key: manifest[key] for key in _SETTINGS}


def _owned_files(paths: ServicePaths) -> tuple[dict[str, Any] | None, bytes | None]:
    """(manifest, plist bytes) when the files prove insto ownership; (None, None) otherwise."""
    if not os.path.lexists(paths.manifest):
        return None, None
    try:
        manifest = watch_service.read_manifest(paths)
    except BackendError:
        return None, None
    if not os.path.lexists(paths.plist):
        return manifest, None
    try:
        plist = watch_service.read_private_file(paths.plist)
        document = plistlib.loads(plist)
    except (BackendError, plistlib.InvalidFileException, ValueError, OSError):
        return None, None
    if not watch_service._matches_owned_plist(paths, manifest, document):
        return None, None
    return manifest, plist


def _job_matches(output: bytes, plist: bytes | None) -> bool:
    """The loaded job runs exactly the owned plist's program and arguments."""
    if plist is None:
        return False
    try:
        fields, arguments = _outer_fields(output)
        expected = plistlib.loads(plist)["ProgramArguments"]
    except (BackendError, KeyError, TypeError, ValueError, plistlib.InvalidFileException):
        return False
    return bool(fields.get("program") == expected[0] and arguments == expected)


def _process(state: str | None) -> str:
    return _PROCESS_STATES.get(state, "unknown")


async def registration_facts(
    paths: ServicePaths, *, deadline: float, expected: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Describe the registration for `paths` without changing anything.

    `expected` is the manifest this interpreter would write for the home's current
    configuration; when given, `settings` says whether the registered pins match it.
    """
    manifest, plist = _owned_files(paths)
    has_files = os.path.lexists(paths.manifest) or os.path.lexists(paths.plist)
    loaded: bool | None = None
    process = "unknown"
    job: bytes | None = None
    try:
        result = await watch_service._launchctl(
            ["print", f"gui/{os.getuid()}/{paths.label}"], deadline=deadline
        )
    except BackendError:
        # launchctl absent (non-macOS) or failed: ownership of files still counts,
        # but an expired budget is a timeout, never "degraded facts".
        result = None
    if time.monotonic() >= deadline:
        raise DesktopError("operation_timeout")
    if result is not None and result.returncode == 0:
        loaded = True
        job = result.stdout
        process = _process(watch_service._parse_launchctl_print(job)["state"])
    elif result is not None and watch_service._is_missing(result):
        loaded, process = False, "stopped"
    owned = manifest is not None and (not loaded or _job_matches(job or b"", plist))
    if manifest is None or not owned:
        return {
            "registration": "unknown" if has_files or loaded else "none",
            "installation": None,
            "interpreter": None,
            "interpreter_exists": None,
            "loaded": loaded,
            "process": process,
            "settings": None,
        }
    python = str(manifest["python"])
    settings = None
    if expected is not None:
        settings = (
            "matching"
            if manifest_settings(manifest) == manifest_settings(expected)
            else "different"
        )
    return {
        "registration": "owned",
        "installation": "installed" if plist is not None else "incomplete",
        "interpreter": "current" if python == os.path.abspath(sys.executable) else "other",
        "interpreter_exists": Path(python).is_file(),
        "loaded": loaded,
        "process": process,
        "settings": settings,
    }
