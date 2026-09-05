"""Pure C2 watch parameter validation before profile/domain imports."""

from __future__ import annotations

import base64
import binascii
import re
from typing import Any

from insto.desktop.errors import DesktopError

CAPABILITIES = (
    "overview",
    "watches.list",
    "watches.add",
    "watches.update",
    "watches.pause",
    "watches.resume",
    "watches.remove",
)


def _user(value: Any) -> str:
    if not isinstance(value, str):
        raise DesktopError("invalid_params")
    # Exactly the CLI order (_canonical_watch_user): leading "@" first, then
    # surrounding whitespace, then lowercase; " @alice" stays invalid everywhere.
    user = value.lstrip("@").strip().lower()
    if re.fullmatch(r"[a-z0-9._]{1,255}", user) is None or user in (".", ".."):
        raise DesktopError("invalid_params")
    return user


def cursor_for(user: str | None) -> str | None:
    if user is None:
        return None
    return "w1." + base64.urlsafe_b64encode(user.encode("ascii")).decode("ascii").rstrip("=")


def _after(value: Any) -> str:
    if not isinstance(value, str) or not 4 <= len(value) <= 512 or not value.startswith("w1."):
        raise DesktopError("invalid_params")
    try:
        encoded = value[3:]
        raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
        user = raw.decode("ascii")
        if _user(user) != user or cursor_for(user) != value:
            raise ValueError
        return user
    except (ValueError, UnicodeError, binascii.Error):
        raise DesktopError("invalid_params") from None


def validate_params(operation: str, params: dict[str, Any]) -> dict[str, Any]:
    if type(params) is not dict or operation not in CAPABILITIES:
        raise DesktopError("invalid_params")
    if operation == "overview":
        if params:
            raise DesktopError("invalid_params")
        return {}
    if operation == "watches.list":
        if params.keys() - {"limit", "cursor"}:
            raise DesktopError("invalid_params")
        limit = params.get("limit", 50)
        if type(limit) is not int or not 1 <= limit <= 50:
            raise DesktopError("invalid_params")
        return {"limit": limit, "after": _after(params["cursor"]) if "cursor" in params else ""}
    required = {"user"} if operation == "watches.add" else {"user", "revision"}
    if operation == "watches.update":
        required.add("interval_seconds")
    allowed = required | ({"interval_seconds"} if operation == "watches.add" else set())
    if not required <= params.keys() or params.keys() - allowed:
        raise DesktopError("invalid_params")
    result: dict[str, Any] = {"user": _user(params["user"])}
    if operation != "watches.add":
        revision = params["revision"]
        if not isinstance(revision, str) or re.fullmatch(r"[a-f0-9]{64}", revision) is None:
            raise DesktopError("invalid_params")
        result["revision"] = revision
    if operation in ("watches.add", "watches.update"):
        interval = params.get("interval_seconds", 300)
        if type(interval) is not int or not 300 <= interval <= 2**31 - 1:
            raise DesktopError("invalid_params")
        result["interval_seconds"] = interval
    return result
