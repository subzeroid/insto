"""Parameter validation for home operations; imported before any operation code."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from insto.desktop.errors import DesktopError

CAPABILITIES = ("home.inspect", "home.select")
PATH_LIMIT_BYTES = 1024


def validate_path(value: Any, *, allow_none: bool) -> Path | None:
    """An absolute or `~`/`~/…` path (this account's home), canonical, symlink-free."""
    if value is None:
        if allow_none:
            return None
        raise DesktopError("invalid_params")
    if not isinstance(value, str) or not value or "\x00" in value:
        raise DesktopError("invalid_params")
    try:
        encoded = value.encode("utf-8")  # a lone surrogate is not a path
    except UnicodeEncodeError:
        raise DesktopError("invalid_params") from None
    if len(encoded) > PATH_LIMIT_BYTES:
        raise DesktopError("invalid_params")
    expanded = value
    if value == "~" or value.startswith("~/"):
        # An unset or empty HOME expands "~" to the filesystem root: no account home.
        account = os.path.expanduser("~")
        if not os.path.isabs(account) or account == "/":
            raise DesktopError("home_invalid")
        expanded = os.path.expanduser(value)
    path = Path(expanded)
    if not path.is_absolute() or os.path.normpath(expanded) != str(path):
        raise DesktopError("home_invalid")
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        # RuntimeError: a symlink loop on Python 3.11/3.12 (ELOOP as OSError later).
        raise DesktopError("home_invalid") from None
    if resolved != path:
        raise DesktopError("home_invalid")
    return path
