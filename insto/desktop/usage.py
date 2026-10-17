"""Local usage settings and inspection; no provider or service changes."""

from __future__ import annotations

from typing import Any

from insto.desktop.configuration import parse_profile_config
from insto.desktop.errors import DesktopError
from insto.desktop.profile import Profile
from insto.exceptions import UsageStorageError
from insto.service.usage import UsageLedger, usage_path


async def run(profile: Profile, operation: str, params: dict[str, Any]) -> dict[str, Any]:
    payload = profile.read_config()
    if payload is None and operation == "usage.configure":
        raise DesktopError("not_configured")
    database = (
        parse_profile_config(profile, payload).db_path
        if payload is not None
        else profile.home / "store.db"
    )
    ledger = UsageLedger(usage_path(database))
    try:
        if operation == "usage.inspect":
            return await ledger.inspect()
        return await ledger.configure(params["daily_limit"])
    except UsageStorageError:
        raise DesktopError("storage_error") from None
