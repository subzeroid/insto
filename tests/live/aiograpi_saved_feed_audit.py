"""Opt-in live audit for aiograpi saved collections and personal feed.

Requires ``TEST_ACCOUNTS_URL``. Skips cleanly when unset. The script uses
account session settings from the private account provider and prints only
surface names, status, counts, and response-shape summaries; it must not print
usernames, passwords, proxies, account URLs, collection names, captions, or raw
Instagram payloads.

Run::

    TEST_ACCOUNTS_URL=... uv run --extra aiograpi python tests/live/aiograpi_saved_feed_audit.py

Optional:

    IG_PROXY=socks5h://127.0.0.1:9050 ...
    ... --use-account-proxy

Endpoint failures are reported but do not fail the process. The audit exits
non-zero only when configured accounts cannot be fetched or none can
authenticate.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from insto._redact import redact_secrets, register_secret  # noqa: E402
from insto.backends.aiograpi import AiograpiBackend  # noqa: E402


@dataclass(frozen=True)
class CheckResult:
    surface: str
    status: str
    detail: str


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


def _collection_id(collection: Any) -> str | None:
    for attr in ("id", "pk", "collection_pk"):
        value = getattr(collection, attr, None)
        if value:
            return str(value)
    return None


def _summarize_collections(collections: list[Any]) -> str:
    media_total = sum(
        int(getattr(item, "media_count", 0) or 0)
        for item in collections
        if str(getattr(item, "media_count", "") or "0").isdigit()
    )
    return f"count={len(collections)} media_total={media_total}"


def _summarize_media(items: list[Any]) -> str:
    by_type = Counter(str(getattr(item, "media_type", "unknown") or "unknown") for item in items)
    counts = ",".join(f"{key}:{value}" for key, value in sorted(by_type.items()))
    return f"count={len(items)} types={counts or 'none'}"


def _timeline_summary(payload: Any) -> str:
    if not isinstance(payload, dict):
        return f"type={type(payload).__name__}"
    item_count = len(payload.get("items") or payload.get("feed_items") or [])
    keys = ",".join(sorted(str(key) for key in payload)[:10])
    has_next = "yes" if payload.get("next_max_id") or payload.get("max_id") else "no"
    return f"items={item_count} has_next={has_next} keys={keys}"


def _proxy_for_account(account: dict[str, Any], *, use_account_proxy: bool) -> str | None:
    proxy = os.environ.get("IG_PROXY")
    if proxy:
        register_secret(proxy)
        return proxy
    if use_account_proxy:
        value = account.get("proxy")
        if isinstance(value, str) and value:
            register_secret(value)
            return value
    return None


async def _fetch_accounts(accounts_url: str, *, count: int) -> list[dict[str, Any]]:
    url = _build_accounts_url(accounts_url, count=count)
    register_secret(accounts_url)
    register_secret(url)
    async with httpx.AsyncClient(timeout=20, verify=False) as client:
        response = await client.get(url, headers={"User-Agent": "insto-live-audit/1"})
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise RuntimeError("TEST_ACCOUNTS_URL returned non-list payload")
    accounts = [item for item in payload if isinstance(item, dict)]
    if not accounts:
        raise RuntimeError("TEST_ACCOUNTS_URL returned no account objects")
    return accounts


def _write_session(path: Path, account: dict[str, Any]) -> None:
    settings = dict(account.get("client_settings") or {})
    settings.pop("totp_seed", None)
    path.write_text(json.dumps(settings), encoding="utf-8")
    os.chmod(path, 0o600)


async def _run_check(surface: str, factory: Any) -> CheckResult:
    try:
        detail = await factory()
    except Exception as exc:
        return CheckResult(surface, "fail", _safe_error(exc))
    return CheckResult(surface, "ok", str(detail))


async def _probe_account(
    index: int,
    account: dict[str, Any],
    *,
    use_account_proxy: bool,
    media_limit: int,
) -> list[CheckResult]:
    _register_account_secrets(os.environ.get("TEST_ACCOUNTS_URL", ""), account)
    username = str(account.get("username") or "")
    password = str(account.get("password") or "")
    totp_seed = str(account.get("totp_seed") or "")
    if not username or not password:
        return [CheckResult("auth", "fail", "account object missing username/password")]

    with tempfile.TemporaryDirectory(prefix=f"insto-aio-audit-{index}-") as tmp:
        session_path = Path(tmp) / "session.json"
        _write_session(session_path, account)
        backend = AiograpiBackend(
            username=username,
            password=password,
            totp_seed=totp_seed or None,
            session_path=session_path,
            proxy=_proxy_for_account(account, use_account_proxy=use_account_proxy),
        )
        try:
            results: list[CheckResult] = []

            async def auth() -> str:
                info = await backend._call(lambda: backend._client.account_info())
                return f"user_id_present={'yes' if getattr(info, 'pk', None) else 'no'}"

            auth_result = await _run_check("auth", auth)
            results.append(auth_result)
            if auth_result.status != "ok":
                results.extend(
                    [
                        CheckResult("collections", "skip", "auth failed"),
                        CheckResult("saved_media", "skip", "auth failed"),
                        CheckResult("collection_media", "skip", "auth failed"),
                        CheckResult("timeline_feed", "skip", "auth failed"),
                    ]
                )
                return results

            collections: list[Any] = []

            async def collections_check() -> str:
                nonlocal collections
                collections = list(await backend._call(lambda: backend._client.collections()))
                return _summarize_collections(collections)

            results.append(await _run_check("collections", collections_check))

            async def saved_media() -> str:
                items = list(
                    await backend._call(
                        lambda: backend._client.collection_medias("saved", amount=media_limit)
                    )
                )
                return _summarize_media(items)

            results.append(await _run_check("saved_media", saved_media))

            collection_pk = next(
                (_collection_id(item) for item in collections if _collection_id(item)),
                None,
            )
            if collection_pk is None:
                results.append(CheckResult("collection_media", "skip", "no collections"))
            else:

                async def collection_media() -> str:
                    items = list(
                        await backend._call(
                            lambda: backend._client.collection_medias(
                                collection_pk, amount=media_limit
                            )
                        )
                    )
                    return _summarize_media(items)

                results.append(await _run_check("collection_media", collection_media))

            async def timeline_feed() -> str:
                payload = await backend._call(lambda: backend._client.get_timeline_feed())
                return _timeline_summary(payload)

            results.append(await _run_check("timeline_feed", timeline_feed))
            return results
        finally:
            await backend.aclose()


def _print_results(all_results: list[list[CheckResult]]) -> int:
    auth_ok = 0
    auth_total = 0
    totals: dict[str, Counter[str]] = {}
    for index, results in enumerate(all_results, start=1):
        print(f"account[{index}]")
        for result in results:
            totals.setdefault(result.surface, Counter())[result.status] += 1
            if result.surface == "auth":
                auth_total += 1
                auth_ok += int(result.status == "ok")
            print(f"  {result.surface}: {result.status} {result.detail}")

    print()
    print("summary")
    for surface in sorted(totals):
        counts = totals[surface]
        print(f"  {surface}: ok={counts['ok']} fail={counts['fail']} skip={counts['skip']}")
    if auth_ok == 0:
        print(f"\nFAILED: 0/{auth_total} accounts authenticated")
        return 1
    print(f"\nAUTH PASS: {auth_ok}/{auth_total} accounts authenticated")
    return 0


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=2, help="number of test accounts to audit")
    parser.add_argument("--media-limit", type=int, default=3, help="media rows per media surface")
    parser.add_argument(
        "--use-account-proxy",
        action="store_true",
        help="use each account object's proxy when IG_PROXY is not set",
    )
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

    all_results: list[list[CheckResult]] = []
    for index, account in enumerate(accounts[: args.count], start=1):
        all_results.append(
            await _probe_account(
                index,
                account,
                use_account_proxy=bool(args.use_account_proxy),
                media_limit=max(1, int(args.media_limit)),
            )
        )
    return _print_results(all_results)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
