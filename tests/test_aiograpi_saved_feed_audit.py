from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

from insto._redact import clear_registered_secrets, register_secret

MODULE_PATH = Path(__file__).parent / "live" / "aiograpi_saved_feed_audit.py"
SPEC = importlib.util.spec_from_file_location("aiograpi_saved_feed_audit", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
audit = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = audit
SPEC.loader.exec_module(audit)


@pytest.fixture(autouse=True)
def _clean_redaction_registry() -> None:
    clear_registered_secrets()
    yield
    clear_registered_secrets()


def test_build_accounts_url_overrides_count() -> None:
    url = audit._build_accounts_url("https://accounts.example.test/list?foo=1&count=9", count=2)
    assert url == "https://accounts.example.test/list?foo=1&count=2"


def test_safe_error_redacts_registered_values_and_query_tokens() -> None:
    register_secret("very-secret-account-url")
    exc = RuntimeError(
        "failed very-secret-account-url "
        "https://api.example.test/path?token=abc123 "
        "socks5://user:pass@127.0.0.1:9050"
    )

    out = audit._safe_error(exc)

    assert "very-secret-account-url" not in out
    assert "abc123" not in out
    assert "user:pass" not in out
    assert "***" in out


def test_collection_summary_does_not_include_collection_names() -> None:
    class Collection:
        name = "private saved things"
        media_count = 4

    out = audit._summarize_collections([Collection()])

    assert out == "count=1 media_total=4"
    assert "private saved things" not in out


def test_timeline_summary_reports_shape_without_values() -> None:
    payload = {
        "items": [{"caption": "private text", "user": {"username": "alice"}}],
        "next_max_id": "cursor-secret",
        "status": "ok",
    }

    out = audit._timeline_summary(payload)

    assert "items=1" in out
    assert "has_next=yes" in out
    assert "private text" not in out
    assert "alice" not in out
    assert "cursor-secret" not in out


def test_proxy_for_account_prefers_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IG_PROXY", "socks5h://127.0.0.1:9050")

    proxy = audit._proxy_for_account(
        {"proxy": "http://account-proxy.example"},
        use_account_proxy=True,
    )

    assert proxy == "socks5h://127.0.0.1:9050"


def test_proxy_for_account_ignores_account_proxy_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("IG_PROXY", raising=False)

    proxy = audit._proxy_for_account(
        {"proxy": "http://account-proxy.example"},
        use_account_proxy=False,
    )

    assert proxy is None
    assert "IG_PROXY" not in os.environ
