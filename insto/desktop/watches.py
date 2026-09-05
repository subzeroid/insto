"""C2 watch DTOs and local operations; no provider construction or scheduler."""

from __future__ import annotations

import sqlite3
from typing import Any

from insto.desktop.database import (
    check_deadline,
    inspect_profile_files,
    read_database,
    write_database,
)
from insto.desktop.errors import DesktopError
from insto.desktop.profile import Profile
from insto.desktop.watch_params import cursor_for
from insto.exceptions import BackendError
from insto.service import watch_registry as registry

_ERRORS = {
    "conflict": "watch_conflict",
    "not_found": "watch_not_found",
    "exists": "watch_exists",
    "limit": "watch_limit",
    "invalid_data": "storage_error",
}


def run(
    profile: Profile,
    operation: str,
    params: dict[str, Any],
    *,
    deadline: float,
) -> dict[str, Any]:
    check_deadline(deadline)
    try:
        if operation == "watches.list":
            with read_database(profile, deadline=deadline) as connection:
                items, after = registry.list_page(
                    connection,
                    after=params["after"],
                    limit=params["limit"],
                )
                check_deadline(deadline)
                return {"items": items, "next_cursor": cursor_for(after)}
        with write_database(profile, deadline=deadline) as connection:
            row: sqlite3.Row | None
            if operation == "watches.add":
                row = registry.add(connection, params["user"], params["interval_seconds"])
            else:
                row = registry.mutate(
                    connection,
                    operation.removeprefix("watches."),
                    params["user"],
                    params["revision"],
                    interval=params.get("interval_seconds"),
                )
            result = (
                {"watch": registry.public_row(row)}
                if row is not None
                else {"removed_user": params["user"]}
            )
            check_deadline(deadline)
        return result
    except registry.RegistryError as error:
        raise DesktopError(_ERRORS.get(str(error), "storage_error")) from None


async def overview(profile: Profile, *, deadline: float) -> dict[str, Any]:
    try:
        state, config = inspect_profile_files(profile, deadline=deadline)
    except DesktopError as error:
        if error.code != "not_configured":
            raise
        return {
            "configured": False,
            "desired_service": None,
            "service_state": "unknown",
            "quota_remaining": None,
            "quota_checked_at": None,
            "watches": [],
            "next_cursor": None,
        }
    page = run(profile, "watches.list", {"limit": 50, "after": ""}, deadline=deadline)
    # Import and inspect only after the short SQLite read transaction has closed.
    from insto.desktop.operations import _running, read_service

    service = None
    service_state = "unknown"
    try:
        check_deadline(deadline)
        service = read_service(profile, config, deadline)
        report = await service.inspect_owned()
        if _running(report):
            service_state = "running"
        elif (
            report["registration"] == "unloaded"
            and report["process"]["state"] is None
            and report["executor"]["state"] == "idle"
        ):
            service_state = "stopped"
    except (BackendError, KeyError, TypeError):
        service_state = "unknown"
    finally:
        if service is not None:
            service._active = False
    check_deadline(deadline)
    return {
        "configured": True,
        "desired_service": state["desired_service"],
        "service_state": service_state,
        "quota_remaining": state["quota_remaining"],
        "quota_checked_at": state["quota_checked_at"],
        "watches": page["items"],
        "next_cursor": page["next_cursor"],
    }
