"""Tests for insto._redact.redact_secrets."""

from __future__ import annotations

import pytest

from insto._redact import clear_registered_secrets, redact_secrets, register_secret


@pytest.fixture(autouse=True)
def _reset_registered_secrets() -> None:
    clear_registered_secrets()


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


def test_registered_token_is_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A token loaded from config.toml (not from $HIKERAPI_TOKEN) must redact."""
    monkeypatch.delenv("HIKERAPI_TOKEN", raising=False)
    register_secret("toml-token-9876543210")
    text = "auth failed for token toml-token-9876543210 in handler"
    out = redact_secrets(text)
    assert "toml-token-9876543210" not in out
    assert "***" in out


def test_registered_short_value_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HIKERAPI_TOKEN", raising=False)
    register_secret("abc")
    assert redact_secrets("abc xyz abc") == "abc xyz abc"


def test_proxy_userinfo_is_redacted() -> None:
    text = "proxy connect failed for socks5://alice:hunter2@proxy.example.com:1080"
    out = redact_secrets(text)
    assert "alice" not in out
    assert "hunter2" not in out
    assert "***:***@proxy.example.com:1080" in out


def test_clear_registered_secrets() -> None:
    register_secret("removable-secret-1234")
    assert "***" in redact_secrets("see removable-secret-1234 here")
    clear_registered_secrets()
    assert redact_secrets("see removable-secret-1234 here") == "see removable-secret-1234 here"
