"""Live end-to-end smoke for the HikerAPI backend.

Exits 0 if all REQUIRED checks pass; non-zero otherwise. Optional checks
(per-target flaky surfaces like ``/similar``) are reported but never fail
the build — Instagram refuses chaining for many targets and we don't
want a noisy gate.

Required env: ``HIKERAPI_TOKEN_TEST`` — a real HikerAPI token. Skips
cleanly with exit 0 if unset, so this script is safe to call from CI
gates that may or may not have the token wired.

Run::

    HIKERAPI_TOKEN_TEST=... uv run python tests/live/smoke.py

Cost: ~10 HikerAPI calls per run, single-digit cents at standard rates.
Run it before each release tag — see ``CONTRIBUTING.md``.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from insto.backends.hiker import HikerBackend  # noqa: E402
from insto.exceptions import ProfileNotFound  # noqa: E402

# Targets chosen to be (1) public, (2) old, (3) very unlikely to disappear,
# (4) representative of the surfaces we care about. ``instagram`` (pk
# 25025320) is the canonical sentinel; ``nasa`` is a stable secondary
# fixture for hashtag/tagged paths.
PUBLIC_USER = "instagram"
PUBLIC_PK_EXPECTED = "25025320"
NONEXISTENT_USER = "insto_nonexistent_xxx9999_yyyz"
HASHTAG = "python"


async def _run_required(backend: HikerBackend) -> list[tuple[str, BaseException]]:
    """Each REQ check exercises one OSINTBackend method on a real HTTP
    response. A failure here means the SDK contract drifted, the token is
    bad, or HikerAPI is down — block the release."""
    failures: list[tuple[str, BaseException]] = []

    async def check(name: str, coro_factory):  # type: ignore[no-untyped-def]
        try:
            out = await coro_factory()
            print(f"REQ {name}: {out}")
        except Exception as exc:  # noqa: BLE001
            failures.append((name, exc))
            print(f"REQ {name} FAIL: {type(exc).__name__}: {str(exc)[:140]}")

    async def resolve() -> str:
        pk = await backend.resolve_target(PUBLIC_USER)
        assert pk == PUBLIC_PK_EXPECTED, f"pk drift: got {pk}, expected {PUBLIC_PK_EXPECTED}"
        return f"@{PUBLIC_USER} → pk={pk}"

    async def profile() -> str:
        prof = await backend.get_profile(PUBLIC_PK_EXPECTED)
        assert prof.username == PUBLIC_USER
        return f"{prof.username} followers={prof.follower_count}"

    async def posts() -> str:
        items = []
        async for p in backend.iter_user_posts(PUBLIC_PK_EXPECTED, limit=2):
            items.append(p)
        assert len(items) == 2, f"expected 2 posts, got {len(items)}"
        return f"got {len(items)} posts (first pk={items[0].pk})"

    async def followers() -> str:
        users = []
        async for u in backend.iter_user_followers(PUBLIC_PK_EXPECTED, limit=5):
            users.append(u)
        assert len(users) == 5, f"expected 5 followers, got {len(users)}"
        return f"got {len(users)} followers (first @{users[0].username})"

    async def tagged() -> str:
        items = []
        async for p in backend.iter_user_tagged(PUBLIC_PK_EXPECTED, limit=2):
            items.append(p)
        # Some accounts have zero tagged content; assert only the iter ran.
        return f"got {len(items)} tagged items (limit=2)"

    async def hashtag() -> str:
        items = []
        async for p in backend.iter_hashtag_posts(HASHTAG, limit=2):
            items.append(p)
        assert len(items) >= 1, f"expected ≥ 1 hashtag media, got {len(items)}"
        return f"got {len(items)} #{HASHTAG} medias"

    async def quota() -> str:
        # HikerAPI doesn't send x-quota-* headers on every endpoint, so
        # the response-hook path can leave _quota empty. The /sys/balance
        # call is the authoritative source — that's what /quota in the
        # CLI uses too.
        q = await backend.refresh_quota()
        assert q.remaining is not None, "refresh_quota returned remaining=None"
        return f"remaining={q.remaining} rate={q.rate}rps"

    async def not_found() -> str:
        try:
            await backend.resolve_target(NONEXISTENT_USER)
        except ProfileNotFound:
            return "ProfileNotFound (as expected)"
        raise AssertionError(f"expected ProfileNotFound for @{NONEXISTENT_USER}, got success")

    await check("resolve", resolve)
    await check("profile", profile)
    await check("posts", posts)
    await check("followers", followers)
    await check("tagged", tagged)
    await check("hashtag", hashtag)
    await check("quota", quota)
    await check("not_found", not_found)
    return failures


async def _run_optional(backend: HikerBackend) -> tuple[int, int]:
    """Per-target flaky surfaces. Don't fail the run on these — Instagram
    routinely 403s ``/similar`` for accounts it considers high-profile or
    locked down, and that's expected."""
    opt_pass = 0
    opt_total = 0

    async def opt(name: str, coro_factory) -> None:  # type: ignore[no-untyped-def]
        nonlocal opt_pass, opt_total
        opt_total += 1
        try:
            out = await coro_factory()
            opt_pass += 1
            print(f"opt {name}: {out}")
        except Exception as exc:  # noqa: BLE001
            print(f"opt {name}: {type(exc).__name__}: {str(exc)[:140]}")

    async def similar() -> str:
        users = await backend.get_suggested(PUBLIC_PK_EXPECTED)
        return f"got {len(users)} suggested for @{PUBLIC_USER}"

    await opt("similar", similar)
    return opt_pass, opt_total


async def main() -> int:
    token = os.environ.get("HIKERAPI_TOKEN_TEST")
    if not token:
        print("SKIP: HIKERAPI_TOKEN_TEST not set")
        return 0

    backend = HikerBackend(token=token)
    try:
        failures = await _run_required(backend)
        opt_pass, opt_total = await _run_optional(backend)
    finally:
        await backend.aclose()

    print()
    print(f"OPTIONAL: {opt_pass}/{opt_total} flaky surfaces OK")
    if failures:
        print(f"\nFAILED: {len(failures)} required check(s)")
        for name, exc in failures:
            print(f"  - {name}: {type(exc).__name__}: {exc}")
        return 1
    print("\nALL REQUIRED PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
