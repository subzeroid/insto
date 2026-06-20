"""Opt-in live end-to-end test for the aiograpi backend, through ``/info``.

Unlike the saved-feed / private-GraphQL audits (which drive ``AiograpiBackend``
directly), this script proves the *whole* chain "from install to ``/info``":
it spawns the real ``insto`` entrypoint as a subprocess, configured for the
aiograpi backend via environment variables, logs in with a pooled account
(including TOTP), resolves ``@instagram`` and asserts the JSON profile output.

Requires ``TEST_ACCOUNTS_URL``. Skips cleanly when unset. The script prints
only status, return codes and shape — never usernames, passwords, proxies,
account URLs, or raw Instagram payloads (all routed through
``insto._redact``).

Run::

    TEST_ACCOUNTS_URL=... uv run --extra aiograpi python tests/live/aiograpi_info_e2e.py

Optional::

    IG_PROXY=socks5h://127.0.0.1:9050 ...

Exits non-zero only when accounts cannot be fetched or no pooled account can
complete a correct ``/info`` for ``@instagram``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from insto._redact import redact_secrets, register_secret  # noqa: E402

# Stable public target — pk is a fixed Instagram fact, low assertion risk.
TARGET_USERNAME = "instagram"
TARGET_PK = "25025320"
SUBPROCESS_TIMEOUT = 120.0
# Must match aiograpi's default session filename under INSTO_HOME so the
# subprocess backend picks up the pre-seeded session (see cli/config).
SESSION_FILENAME = "aiograpi.session.json"


def _build_accounts_url(raw_url: str, *, count: int) -> str:
    parts = urlsplit(raw_url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["count"] = str(count)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _register_nested_secrets(value: Any) -> None:
    if isinstance(value, str):
        register_secret(value)
    elif isinstance(value, dict):
        for nested in value.values():
            _register_nested_secrets(nested)
    elif isinstance(value, list | tuple):
        for nested in value:
            _register_nested_secrets(nested)


def _register_account_secrets(accounts_url: str, account: dict[str, Any]) -> None:
    register_secret(accounts_url)
    for key in ("username", "password", "proxy", "totp_seed"):
        register_secret(account.get(key))
    _register_nested_secrets(account.get("client_settings"))


def _safe_error(exc: BaseException, *, limit: int = 140) -> str:
    first_line = str(exc).splitlines()[0] if str(exc) else ""
    message = f"{type(exc).__name__}: {first_line}"
    return redact_secrets(message[:limit])


def _account_totp(account: dict[str, Any]) -> str:
    """TOTP seed lives either top-level or under ``client_settings``."""
    seed = account.get("totp_seed")
    if not seed:
        settings = account.get("client_settings")
        if isinstance(settings, dict):
            seed = settings.get("totp_seed")
    return str(seed or "")


def _build_subprocess_env(
    account: dict[str, Any],
    base_env: dict[str, str],
    *,
    tmp_home: str,
) -> dict[str, str]:
    """Construct the child-process env that selects the aiograpi backend.

    ``HIKERAPI_PROXY`` is the proxy the CLI feeds to *every* backend
    (``cli._build_backend`` passes ``config.hiker_proxy`` to aiograpi too).
    Only an explicit ``IG_PROXY`` is honoured.
    """
    env = dict(base_env)
    env["INSTO_HOME"] = tmp_home
    env["INSTO_BACKEND"] = "aiograpi"
    env["AIOGRAPI_USERNAME"] = str(account.get("username") or "")
    env["AIOGRAPI_PASSWORD"] = str(account.get("password") or "")
    env["AIOGRAPI_TOTP_SEED"] = _account_totp(account)
    # Default to no proxy: the seeded session (client_settings) carries the
    # account's device trust, so the login does not need one. Honour an
    # explicit IG_PROXY when the caller supplies it, matching the saved-feed
    # audit's default behaviour.
    proxy = base_env.get("IG_PROXY")
    if proxy:
        env["HIKERAPI_PROXY"] = str(proxy)
    return env


def _write_session(tmp_home: str, account: dict[str, Any]) -> str | None:
    """Seed the account's saved session so the subprocess reuses its device.

    The pooled accounts ship a ``client_settings`` blob (device uuids, phone
    id, etc.). Reusing it — exactly as the saved-feed audit does — lets the
    backend validate via ``account_info()`` and skip a cold login that IG
    would likely challenge. ``totp_seed`` is never written to the session.
    Returns the session path, or ``None`` when the account has no settings.
    """
    settings = account.get("client_settings")
    if not isinstance(settings, dict) or not settings:
        return None
    settings = {key: value for key, value in settings.items() if key != "totp_seed"}
    path = Path(tmp_home) / SESSION_FILENAME
    path.write_text(json.dumps(settings), encoding="utf-8")
    os.chmod(path, 0o600)
    return str(path)


def _extract_profile(stdout: str) -> dict[str, Any]:
    """Parse the insto JSON envelope and return its ``data.profile`` object."""
    blob = stdout.strip()
    if not blob:
        raise RuntimeError("empty stdout from insto subprocess")
    try:
        envelope = json.loads(blob)
    except json.JSONDecodeError:
        # Be tolerant of any leading/trailing noise around the JSON object.
        start, end = blob.find("{"), blob.rfind("}")
        if start == -1 or end == -1:
            raise RuntimeError("no JSON object in subprocess stdout") from None
        envelope = json.loads(blob[start : end + 1])
    profile = envelope.get("data", {}).get("profile")
    if not isinstance(profile, dict):
        raise RuntimeError("JSON envelope missing data.profile")
    return profile


async def _fetch_accounts(accounts_url: str, *, count: int) -> list[dict[str, Any]]:
    url = _build_accounts_url(accounts_url, count=count)
    register_secret(accounts_url)
    register_secret(url)
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(url, headers={"User-Agent": "insto-live-e2e/1"})
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise RuntimeError("TEST_ACCOUNTS_URL returned non-list payload")
    accounts = [item for item in payload if isinstance(item, dict)]
    if not accounts:
        raise RuntimeError("TEST_ACCOUNTS_URL returned no account objects")
    return accounts


async def _info_via_cli(index: int, account: dict[str, Any]) -> bool:
    """Run ``insto @instagram -c info --json -`` for one account. True on pass."""
    _register_account_secrets(os.environ.get("TEST_ACCOUNTS_URL", ""), account)
    if not account.get("username") or not account.get("password"):
        print(f"account[{index}] auth: fail (missing username/password)")
        return False

    with tempfile.TemporaryDirectory(prefix=f"insto-e2e-{index}-") as tmp:
        env = _build_subprocess_env(account, dict(os.environ), tmp_home=tmp)
        _write_session(tmp, account)
        cmd = [
            sys.executable,
            "-m",
            "insto",
            f"@{TARGET_USERNAME}",
            "-c",
            "info",
            "--json",
            "-",
        ]
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                cmd,
                env=env,
                capture_output=True,
                text=True,
                timeout=SUBPROCESS_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            print(f"account[{index}] info: fail (timeout after {SUBPROCESS_TIMEOUT:.0f}s)")
            return False

        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()
            detail = redact_secrets(tail[-1]) if tail else "no output"
            print(f"account[{index}] info: fail (rc={proc.returncode}) {detail[:140]}")
            return False

        try:
            profile = _extract_profile(proc.stdout)
        except Exception as exc:
            print(f"account[{index}] info: fail (parse) {_safe_error(exc)}")
            return False

        username = str(profile.get("username") or "")
        pk = str(profile.get("pk") or "")
        if username == TARGET_USERNAME and pk == TARGET_PK:
            print(f"account[{index}] info: ok (username={username} pk={pk})")
            return True
        print(f"account[{index}] info: fail (got username={username!r} pk={pk!r})")
        return False


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=3, help="number of test accounts to try")
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(argv or []))
    accounts_url = os.environ.get("TEST_ACCOUNTS_URL")
    if not accounts_url:
        print("SKIP: TEST_ACCOUNTS_URL not set")
        return 0

    try:
        accounts = await _fetch_accounts(accounts_url, count=args.count)
    except Exception as exc:
        print(f"FAILED: could not fetch test accounts: {_safe_error(exc)}")
        return 1

    passed = 0
    for index, account in enumerate(accounts[: args.count], start=1):
        if await _info_via_cli(index, account):
            passed += 1
            break  # one clean /info is enough to prove the chain

    if passed:
        print(f"\nPASS: /info via aiograpi CLI succeeded ({passed} account)")
        return 0
    print(f"\nFAILED: no pooled account completed /info for @{TARGET_USERNAME}")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
