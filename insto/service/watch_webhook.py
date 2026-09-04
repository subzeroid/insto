"""Validation and versioned payload construction for watch webhooks."""

from __future__ import annotations

import copy
import ipaddress
from datetime import UTC, datetime
from typing import Any

import httpx

from insto.exceptions import BackendError


def validate_webhook_url(value: str) -> str:
    """Return an accepted webhook URL without exposing rejected URL contents."""
    try:
        endpoint = httpx.URL(value)
    except (httpx.InvalidURL, TypeError):
        raise BackendError("invalid watch webhook URL: malformed URL") from None

    if not endpoint.scheme:
        raise BackendError("invalid watch webhook URL: absolute URL required")
    if not endpoint.host:
        raise BackendError("invalid watch webhook URL: host required")
    if "#" in value:
        raise BackendError("invalid watch webhook URL: fragments are not allowed")
    if endpoint.scheme not in {"http", "https"}:
        raise BackendError("invalid watch webhook URL: unsupported scheme")
    if endpoint.scheme == "https":
        return value

    host = endpoint.host.lower()
    if host == "localhost":
        return value
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and address.is_loopback:
        return value
    raise BackendError("invalid watch webhook URL: HTTPS required for non-local endpoints")


def build_watch_event(
    username: str,
    diff: dict[str, Any],
    *,
    event_id: str,
    observed_at: datetime,
) -> dict[str, Any] | None:
    """Convert a current watch change into the stable version-1 event shape."""
    if diff.get("first_seen") or not diff.get("changes"):
        return None
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")

    observed_utc = observed_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return {
        "schema_version": 1,
        "event": "watch.changed",
        "event_id": event_id,
        "username": username,
        "observed_at": observed_utc,
        "changes": copy.deepcopy(diff["changes"]),
        "previous_usernames": copy.deepcopy(diff.get("previous_usernames") or []),
    }


__all__ = ["build_watch_event", "validate_webhook_url"]
