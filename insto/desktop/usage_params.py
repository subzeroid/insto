"""Exact usage-operation parameters, before accessing any profile."""

from __future__ import annotations

from typing import Any

from insto.desktop.errors import DesktopError

CAPABILITIES = ("usage.inspect", "usage.configure")


def validate_params(operation: str, params: dict[str, Any]) -> dict[str, Any]:
    expected = set() if operation == "usage.inspect" else {"daily_limit"}
    if not isinstance(params, dict) or params.keys() != expected:
        raise DesktopError("invalid_params")
    if operation == "usage.configure":
        limit = params["daily_limit"]
        if limit is not None and (type(limit) is not int or not 1 <= limit <= 1_000_000):
            raise DesktopError("invalid_params")
    return params
