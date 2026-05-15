"""Opt-in live audit for aiograpi private GraphQL pagination surfaces.

Requires ``TEST_ACCOUNTS_URL``. Skips cleanly when unset. The script uses
account session settings from the private account provider and prints only
surface names, status, counts, cursor presence, and id-overlap summaries; it
must not print usernames, passwords, proxies, account URLs, cursors, target
ids, or raw Instagram payloads.

Run::

    TEST_ACCOUNTS_URL=... uv run --extra aiograpi python \
        tests/live/aiograpi_private_graphql_audit.py

Optional:

    IG_PROXY=socks5h://127.0.0.1:9050 ...
    ... --use-account-proxy
    ... --target-username instagram
    ... --target-user-id 25025320

Endpoint failures are reported but do not fail the process. The audit exits
non-zero only when configured accounts cannot be fetched or none can
authenticate.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import logging
import os
import sys
import tempfile
import uuid
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from insto._redact import redact_secrets, register_secret  # noqa: E402
from insto.backends.aiograpi import AiograpiBackend  # noqa: E402

FOLLOWERS_ROOT = "xdt_api__v1__friendships__followers"
FOLLOWING_ROOT = "xdt_api__v1__friendships__following"


@dataclass(frozen=True)
class CheckResult:
    surface: str
    status: str
    detail: str


def _suppress_sdk_tracebacks() -> None:
    """Keep audit output to summaries; aiograpi logs raw tracebacks on fallback."""
    for name in ("aiograpi", "insto.backends.aiograpi"):
        logging.getLogger(name).setLevel(logging.CRITICAL)


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


def _safe_error(exc: BaseException, *, limit: int = 160) -> str:
    first_line = str(exc).splitlines()[0] if str(exc) else ""
    message = f"{type(exc).__name__}: {first_line}"
    return redact_secrets(message[:limit])


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
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            detail = await factory()
    except Exception as exc:
        return CheckResult(surface, "fail", _safe_error(exc))
    return CheckResult(surface, "ok", str(detail))


def _object_id(item: Any) -> str | None:
    if isinstance(item, dict):
        for key in ("pk", "pk_id", "id"):
            value = item.get(key)
            if value:
                return str(value)
        return None
    for attr in ("pk", "pk_id", "id"):
        value = getattr(item, attr, None)
        if value:
            return str(value)
    return None


def _object_ids(items: Iterable[Any]) -> list[str]:
    ids: list[str] = []
    for item in items:
        value = _object_id(item)
        if value:
            ids.append(value)
    return ids


def _find_root(payload: Any, root_field: str) -> dict[str, Any]:
    stack = [payload]
    while stack:
        item = stack.pop()
        if isinstance(item, list):
            stack.extend(item)
            continue
        if not isinstance(item, dict):
            continue
        value = item.get(root_field)
        if isinstance(value, dict):
            return value
        stack.extend(value for value in item.values() if isinstance(value, dict))
        stack.extend(value for value in item.values() if isinstance(value, list))
    return {}


def _cursor_present(root: dict[str, Any]) -> bool:
    for key in ("next_max_id", "next_cursor", "cursor", "max_id"):
        if root.get(key):
            return True
    page_info = root.get("page_info")
    if isinstance(page_info, dict):
        return bool(page_info.get("end_cursor") or page_info.get("has_next_page"))
    return False


def _private_graphql_user_ids(payload: Any, *, root_field: str) -> tuple[list[str], bool]:
    root = _find_root(payload, root_field)
    users: list[Any] = []
    raw_users = root.get("users")
    if isinstance(raw_users, list):
        users.extend(raw_users)
    raw_items = root.get("items")
    if isinstance(raw_items, list):
        for item in raw_items:
            users.append(item.get("user") if isinstance(item, dict) and "user" in item else item)
    edges = root.get("edges")
    if isinstance(edges, list):
        for edge in edges:
            users.append(edge.get("node") if isinstance(edge, dict) else edge)
    return _object_ids(users), _cursor_present(root)


def _comparison_summary(
    *,
    current_ids: list[str],
    candidate_ids: list[str],
    candidate_cursor_present: bool,
) -> str:
    current_set = set(current_ids)
    candidate_set = set(candidate_ids)
    overlap = len(current_set & candidate_set)
    if current_ids == candidate_ids:
        verdict = "ok"
    elif not current_ids and not candidate_ids:
        verdict = "empty"
    elif current_ids and not candidate_ids:
        verdict = "candidate_empty"
    elif not current_ids and candidate_ids:
        verdict = "current_empty"
    elif overlap:
        verdict = "partial"
    else:
        verdict = "mismatch"
    cursor = "yes" if candidate_cursor_present else "no"
    return (
        f"current={len(current_ids)} candidate={len(candidate_ids)} "
        f"overlap={overlap} cursor={cursor} verdict={verdict}"
    )


async def _resolve_target_user_id(
    backend: AiograpiBackend,
    *,
    account_user_id: str,
    target_user_id: str | None,
    target_username: str | None,
) -> str:
    if target_user_id:
        register_secret(target_user_id)
        return str(target_user_id)
    if target_username:
        register_secret(target_username)
        return str(
            await backend._call(lambda: backend._client.user_id_from_username(target_username))
        )
    register_secret(account_user_id)
    return str(account_user_id)


async def _probe_account(
    index: int,
    account: dict[str, Any],
    *,
    use_account_proxy: bool,
    limit: int,
    target_user_id: str | None,
    target_username: str | None,
) -> list[CheckResult]:
    _register_account_secrets(os.environ.get("TEST_ACCOUNTS_URL", ""), account)
    username = str(account.get("username") or "")
    password = str(account.get("password") or "")
    totp_seed = str(account.get("totp_seed") or "")
    if not username or not password:
        return [CheckResult("auth", "fail", "account object missing username/password")]

    if target_user_id:
        register_secret(target_user_id)
    if target_username:
        register_secret(target_username)

    with tempfile.TemporaryDirectory(prefix=f"insto-aio-pgql-audit-{index}-") as tmp:
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

            account_user_id = ""

            async def auth() -> str:
                nonlocal account_user_id
                info = await backend._call(lambda: backend._client.account_info())
                account_user_id = str(getattr(info, "pk", "") or "")
                return f"user_id_present={'yes' if account_user_id else 'no'}"

            auth_result = await _run_check("auth", auth)
            results.append(auth_result)
            if auth_result.status != "ok":
                results.extend(
                    [
                        CheckResult("target", "skip", "auth failed"),
                        CheckResult("followers_private_graphql", "skip", "auth failed"),
                        CheckResult("following_private_graphql", "skip", "auth failed"),
                        CheckResult("media_graphql", "skip", "auth failed"),
                    ]
                )
                return results

            target = ""

            async def target_check() -> str:
                nonlocal target
                target = await _resolve_target_user_id(
                    backend,
                    account_user_id=account_user_id,
                    target_user_id=target_user_id,
                    target_username=target_username,
                )
                return "source=provided" if (target_user_id or target_username) else "source=self"

            target_result = await _run_check("target", target_check)
            results.append(target_result)
            if target_result.status != "ok":
                results.extend(
                    [
                        CheckResult("followers_private_graphql", "skip", "target failed"),
                        CheckResult("following_private_graphql", "skip", "target failed"),
                        CheckResult("media_graphql", "skip", "target failed"),
                    ]
                )
                return results

            async def followers_private_graphql() -> str:
                current = await backend._call(
                    lambda: backend._client.user_followers(target, amount=limit)
                )
                current_items = current.values() if isinstance(current, dict) else current
                payload = await backend._call(
                    lambda: backend._client.private_graphql_followers_list(
                        target,
                        rank_token=str(uuid.uuid4()),
                    )
                )
                candidate_ids, cursor = _private_graphql_user_ids(
                    payload,
                    root_field=FOLLOWERS_ROOT,
                )
                return _comparison_summary(
                    current_ids=_object_ids(current_items),
                    candidate_ids=candidate_ids,
                    candidate_cursor_present=cursor,
                )

            results.append(await _run_check("followers_private_graphql", followers_private_graphql))

            async def following_private_graphql() -> str:
                current = await backend._call(
                    lambda: backend._client.user_following(target, amount=limit)
                )
                current_items = current.values() if isinstance(current, dict) else current
                payload = await backend._call(
                    lambda: backend._client.private_graphql_following_list(
                        target,
                        rank_token=str(uuid.uuid4()),
                    )
                )
                candidate_ids, cursor = _private_graphql_user_ids(
                    payload,
                    root_field=FOLLOWING_ROOT,
                )
                return _comparison_summary(
                    current_ids=_object_ids(current_items),
                    candidate_ids=candidate_ids,
                    candidate_cursor_present=cursor,
                )

            results.append(await _run_check("following_private_graphql", following_private_graphql))

            async def media_graphql() -> str:
                current = await backend._call(
                    lambda: backend._client.user_medias(int(target), amount=limit, sleep=0)
                )
                candidate, cursor = await backend._call(
                    lambda: backend._client.user_medias_paginated_gql(
                        target,
                        amount=limit,
                        sleep=0,
                    )
                )
                return _comparison_summary(
                    current_ids=_object_ids(current),
                    candidate_ids=_object_ids(candidate),
                    candidate_cursor_present=bool(cursor),
                )

            results.append(await _run_check("media_graphql", media_graphql))
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
    parser.add_argument("--count", type=int, default=1, help="number of test accounts to audit")
    parser.add_argument("--limit", type=int, default=3, help="rows per current-wrapper surface")
    parser.add_argument("--target-user-id", help="optional target pk; redacted from output")
    parser.add_argument("--target-username", help="optional target username; redacted from output")
    parser.add_argument(
        "--use-account-proxy",
        action="store_true",
        help="use each account object's proxy when IG_PROXY is not set",
    )
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None) -> int:
    _suppress_sdk_tracebacks()
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
                limit=max(1, int(args.limit)),
                target_user_id=args.target_user_id,
                target_username=args.target_username,
            )
        )
    return _print_results(all_results)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
