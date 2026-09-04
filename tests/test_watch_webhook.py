"""Tests for the watch webhook event and endpoint contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from insto.exceptions import BackendError
from insto.service.watch_webhook import build_watch_event, validate_webhook_url


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://receiver.example/hooks/watch?token=secret",
        "https://receiver.example:8443/hooks/watch",
        "http://localhost/hooks/watch",
        "http://127.0.0.1/hooks/watch",
        "http://127.255.255.254/hooks/watch",
        "http://[::1]/hooks/watch",
    ],
)
def test_validate_webhook_url_accepts_secure_and_local_endpoints(endpoint: str) -> None:
    assert validate_webhook_url(endpoint) == endpoint


@pytest.mark.parametrize(
    ("endpoint", "reason"),
    [
        ("receiver.example/secret/path?token=shh", "absolute"),
        ("https:///secret/path?token=shh", "host"),
        ("https://receiver.example/secret/path?token=shh#private", "fragment"),
        ("ftp://receiver.example/secret/path?token=shh", "scheme"),
        ("http://receiver.example/secret/path?token=shh", "HTTPS"),
        ("http://192.0.2.1/secret/path?token=shh", "HTTPS"),
    ],
)
def test_validate_webhook_url_rejects_safely(endpoint: str, reason: str) -> None:
    with pytest.raises(BackendError) as caught:
        validate_webhook_url(endpoint)

    message = str(caught.value)
    assert message.startswith("invalid watch webhook URL:")
    assert reason in message
    assert endpoint not in message
    assert "secret" not in message
    assert "token" not in message
    assert "shh" not in message


@pytest.mark.parametrize(
    "diff",
    [
        {"first_seen": True, "changes": {"biography": {"old": "", "new": "hi"}}},
        {"first_seen": False, "changes": {}},
        {"first_seen": False, "changes": {}, "previous_usernames": ["old_alice"]},
        {"first_seen": False, "previous_usernames": ["old_alice"]},
    ],
)
def test_build_watch_event_suppresses_non_changes(diff: dict[str, Any]) -> None:
    event = build_watch_event(
        "alice",
        diff,
        event_id="evt-suppressed",
        observed_at=datetime(2026, 9, 4, 18, 30, tzinfo=UTC),
    )

    assert event is None


def test_build_watch_event_returns_exact_versioned_payload() -> None:
    observed_at = datetime(
        2026,
        9,
        4,
        21,
        30,
        45,
        123456,
        tzinfo=timezone(timedelta(hours=3)),
    )
    diff = {
        "first_seen": False,
        "changes": {
            "biography": {"old": "old bio", "new": "new bio"},
            "is_verified": {"old": False, "new": True},
        },
        "previous_usernames": ["old_alice"],
        "ignored": "not part of the public contract",
    }

    event = build_watch_event(
        "alice",
        diff,
        event_id="evt-123",
        observed_at=observed_at,
    )

    assert event == {
        "schema_version": 1,
        "event": "watch.changed",
        "event_id": "evt-123",
        "username": "alice",
        "observed_at": "2026-09-04T18:30:45.123456Z",
        "changes": {
            "biography": {"old": "old bio", "new": "new bio"},
            "is_verified": {"old": False, "new": True},
        },
        "previous_usernames": ["old_alice"],
    }
    assert event is not None
    assert set(event) == {
        "schema_version",
        "event",
        "event_id",
        "username",
        "observed_at",
        "changes",
        "previous_usernames",
    }


def test_build_watch_event_copies_nested_mutable_values() -> None:
    nested_new = ["one"]
    changes = {"labels": {"old": [], "new": nested_new}}
    aliases = ["old_alice"]
    diff = {
        "first_seen": False,
        "changes": changes,
        "previous_usernames": aliases,
    }

    event = build_watch_event(
        "alice",
        diff,
        event_id="evt-copy",
        observed_at=datetime(2026, 9, 4, tzinfo=UTC),
    )
    assert event is not None

    nested_new.append("two")
    changes["labels"]["old"].append("mutated")
    aliases.append("older_alice")

    assert event["changes"] == {"labels": {"old": [], "new": ["one"]}}
    assert event["previous_usernames"] == ["old_alice"]
