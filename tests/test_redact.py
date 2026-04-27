"""Tests for insto._redact.redact_secrets."""

from __future__ import annotations

import pytest

from insto._redact import redact_secrets


def test_no_secrets_passthrough() -> None:
    assert redact_secrets("nothing to hide here") == "nothing to hide here"
    assert redact_secrets("") == ""


def test_redacts_env_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HIKERAPI_TOKEN", "tok-secret-1234567890")
    text = "request failed for token tok-secret-1234567890 in middleware"
    assert "tok-secret-1234567890" not in redact_secrets(text)
    assert "***" in redact_secrets(text)


def test_redacts_query_signature() -> None:
    url = "https://cdn.example.com/p.jpg?signature=abcDEF123_-&size=1024"
    out = redact_secrets(url)
    assert "abcDEF123" not in out
    assert "signature=***" in out
    assert "size=1024" in out


def test_redacts_query_token() -> None:
    url = "https://api.example.com/v1?token=hush-hush&id=42"
    out = redact_secrets(url)
    assert "hush-hush" not in out
    assert "token=***" in out
    assert "id=42" in out


def test_redacts_bearer_header() -> None:
    text = "Authorization: Bearer abcdef.0123-XYZ_~+/="
    out = redact_secrets(text)
    assert "abcdef" not in out
    assert "Bearer ***" in out


def test_short_env_token_not_substituted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Avoid replacing trivially-short strings — would produce nonsense."""
    monkeypatch.setenv("HIKERAPI_TOKEN", "ab")
    assert redact_secrets("logs ab abc") == "logs ab abc"


def test_no_env_token_no_change(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HIKERAPI_TOKEN", raising=False)
    assert redact_secrets("nothing here") == "nothing here"
