"""Read-only launchd registration facts; never mutates files or jobs."""

from __future__ import annotations

import os
import plistlib
import sys
import time
from pathlib import Path
from typing import Any
from xml.parsers.expat import ExpatError

from insto.desktop.errors import DesktopError
from insto.exceptions import BackendError
from insto.service import watch_service
from insto.service.watch_service import ServicePaths
from insto.service.watch_service_lifecycle import _outer_fields

# `output_dir` is deliberately not a pin: the CLI resolves its default `./output` against
# the install cwd, the watch service never writes exports, and a migration normalizes it
# to the home (ruling R21 after the native proof).
_SETTINGS = ("backend", "db_path", "aiograpi_session_path", "env_file")
_PROCESS_STATES = {
    "running": "running",
    "xpcproxy": "running",  # starting
    "waiting": "stopped",
    "not running": "stopped",
    "exited": "stopped",
    "SIGTERMed": "stopped",
}


def manifest_settings(manifest: dict[str, Any]) -> dict[str, Any]:
    """The data-affecting pins a migration must keep byte-for-byte.

    Backend, database, session and env-file paths decide what the service reads and
    writes; the output directory does not (see `_SETTINGS`).
    """
    return {key: manifest[key] for key in _SETTINGS}


def _owned_files(paths: ServicePaths) -> tuple[dict[str, Any] | None, bytes | None]:
    """(manifest, plist bytes) when both files prove insto ownership.

    (manifest, None) when the manifest is owned but the plist is absent; (None, None)
    for anything foreign, corrupt or unreadable.
    """
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
    except (BackendError, ExpatError, plistlib.InvalidFileException, ValueError, OSError):
        return None, None
    if not watch_service._matches_owned_plist(paths, manifest, document):
        return None, None
    return manifest, plist


def _job_matches(paths: ServicePaths, output: bytes, plist: bytes | None) -> bool:
    """The loaded job runs exactly the owned plist's program and arguments, from its path.

    The same provenance predicate as the lifecycle's `_process`: a job loaded from
    another plist file with identical arguments is not this registration.
    """
    if plist is None:
        return False
    try:
        fields, arguments = _outer_fields(output)
        expected = plistlib.loads(plist)["ProgramArguments"]
    except (
        BackendError,
        ExpatError,
        KeyError,
        TypeError,
        ValueError,
        plistlib.InvalidFileException,
    ):
        return False
    return bool(
        fields.get("program") == expected[0]
        and arguments == expected
        and ("path" not in fields or fields["path"] == str(paths.plist))
    )


def _process(state: str | None) -> str:
    # A loaded job whose output carries no state line is not provably stopped.
    if state is None:
        return "unknown"
    return _PROCESS_STATES.get(state, "unknown")


async def registration_facts(
    paths: ServicePaths, *, deadline: float, expected: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Describe the registration for `paths` without changing anything.

    `expected` is the manifest this interpreter would write for the home's current
    configuration; when given, `settings` says whether the registered pins match it.
    An `expected` missing any pinned key is an unknown expectation: `settings` stays None.
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
    owned = manifest is not None and (not loaded or _job_matches(paths, job or b"", plist))
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
    if expected is not None and all(key in expected for key in _SETTINGS):
        settings = (
            "matching"
            if manifest_settings(manifest) == manifest_settings(expected)
            else "different"
        )
    try:
        exists = Path(python).is_file()
    except OSError:
        # The manifest may point below a directory this user can no longer traverse.
        exists = False
    return {
        "registration": "owned",
        "installation": "installed" if plist is not None else "incomplete",
        "interpreter": "current" if python == os.path.abspath(sys.executable) else "other",
        "interpreter_exists": exists,
        "loaded": loaded,
        "process": process,
        "settings": settings,
    }
