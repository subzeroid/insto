from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from insto._redact import clear_registered_secrets, register_secret

MODULE_PATH = Path(__file__).parent / "live" / "aiograpi_info_e2e.py"
SPEC = importlib.util.spec_from_file_location("aiograpi_info_e2e", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
e2e = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = e2e
SPEC.loader.exec_module(e2e)


@pytest.fixture(autouse=True)
def _clean_redaction_registry() -> None:
    clear_registered_secrets()
    yield
    clear_registered_secrets()


def test_main_skips_clean_when_url_unset(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import asyncio

    monkeypatch.delenv("TEST_ACCOUNTS_URL", raising=False)

    rc = asyncio.run(e2e.main([]))

    assert rc == 0
    assert "SKIP" in capsys.readouterr().out


def test_account_totp_reads_top_level_seed() -> None:
    assert e2e._account_totp({"totp_seed": "TOPSEED"}) == "TOPSEED"


def test_account_totp_reads_nested_client_settings_seed() -> None:
    account = {"client_settings": {"totp_seed": "NESTEDSEED", "uuid": "x"}}
    assert e2e._account_totp(account) == "NESTEDSEED"


def test_account_totp_missing_returns_empty() -> None:
    assert e2e._account_totp({"client_settings": {}}) == ""


def test_build_accounts_url_overrides_count() -> None:
    url = e2e._build_accounts_url("https://accounts.example.test/list?foo=1&count=9", count=2)
    assert url == "https://accounts.example.test/list?foo=1&count=2"


def test_safe_error_redacts_registered_values_and_query_tokens() -> None:
    register_secret("very-secret-account-url")
    exc = RuntimeError(
        "failed very-secret-account-url https://api.example.test/path?token=abc123 "
        "socks5://user:pass@127.0.0.1:9050"
    )

    out = e2e._safe_error(exc)

    assert "very-secret-account-url" not in out
    assert "abc123" not in out
    assert "user:pass" not in out
    assert "***" in out


def test_build_subprocess_env_wires_aiograpi_creds() -> None:
    account = {
        "username": "ig_user",
        "password": "ig_pass",
        "proxy": "http://acct-proxy.example:8080",
        "client_settings": {"totp_seed": "SEED2FA"},
    }

    env = e2e._build_subprocess_env(account, {"PATH": "/usr/bin"}, tmp_home="/tmp/insto-home")

    assert env["INSTO_BACKEND"] == "aiograpi"
    assert env["AIOGRAPI_USERNAME"] == "ig_user"
    assert env["AIOGRAPI_PASSWORD"] == "ig_pass"
    assert env["AIOGRAPI_TOTP_SEED"] == "SEED2FA"
    assert env["INSTO_HOME"] == "/tmp/insto-home"
    # Parent env is carried through, not replaced.
    assert env["PATH"] == "/usr/bin"


def test_build_subprocess_env_ignores_account_proxy_without_ig_proxy() -> None:
    # Pooled account proxies are unreliable (often 302 on CONNECT); only an
    # explicit IG_PROXY is honoured, matching the saved-feed audit default.
    account = {
        "username": "u",
        "password": "p",
        "proxy": "http://acct-proxy.example:8080",
    }

    env = e2e._build_subprocess_env(account, {"PATH": "/usr/bin"}, tmp_home="/tmp/h")

    assert "HIKERAPI_PROXY" not in env


def test_build_subprocess_env_uses_ig_proxy_env() -> None:
    account = {"username": "u", "password": "p", "proxy": "http://acct-proxy.example:8080"}
    base = {"PATH": "/usr/bin", "IG_PROXY": "socks5h://127.0.0.1:9050"}

    env = e2e._build_subprocess_env(account, base, tmp_home="/tmp/h")

    assert env["HIKERAPI_PROXY"] == "socks5h://127.0.0.1:9050"


def test_write_session_seeds_client_settings_without_totp(tmp_path) -> None:
    import json
    import stat

    account = {"client_settings": {"uuid": "abc", "totp_seed": "SEED"}}

    path = e2e._write_session(str(tmp_path), account)

    assert path == str(tmp_path / e2e.SESSION_FILENAME)
    data = json.loads(Path(path).read_text())
    assert data == {"uuid": "abc"}  # totp_seed never persisted to session
    assert stat.S_IMODE(Path(path).stat().st_mode) == 0o600


def test_write_session_noop_without_client_settings(tmp_path) -> None:
    assert e2e._write_session(str(tmp_path), {"username": "u"}) is None
    assert not (tmp_path / e2e.SESSION_FILENAME).exists()
