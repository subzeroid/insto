"""Network-group commands: `/followers`, `/followings`, `/mutuals`, `/similar`.

Every command in this module fetches `User` DTOs through the facade, then
either renders a user table, exports JSON, or exports flat CSV. None of
these commands writes media to disk; `--no-download` is meaningless here
and is silently ignored.

`/mutuals` carries a defensive symmetric default of `--limit 1000` on each
side (followers and following). The cap protects against pulling 1M+ users
on a celebrity target by accident; passing `--limit 0` opts out completely
and pulls everything the backend will give. When either side fills the cap
the rendered output appends a `(truncated at … / … — pass --limit to widen)`
note so the operator can see the result is partial.

The flat CSV schema for all four commands is identical:

    rank, pk, username, full_name, is_private, is_verified

It is the same schema enforced by `insto.service.exporter.CSV_FLAT_COMMANDS`
for these names.
"""

from __future__ import annotations

import argparse
import dataclasses
from collections.abc import Sequence
from pathlib import Path
from typing import IO, Any

from insto.commands._base import (
    ArgsBuilder,
    CommandContext,
    command,
    resolve_export_dest,
    with_target,
)
from insto.models import User
from insto.service.analytics import MutualsResult
from insto.ui.render import render_user_table

# Defensive symmetric cap on /mutuals when no --limit is given.
MUTUALS_DEFAULT_LIMIT = 1000

# Per-side cap used when the user passes `--limit 0` (opt-out). We still
# bound the request so we never spin forever on a misconfigured backend;
# a million users is a hard ceiling that no real-world Instagram account
# crosses without an enterprise contract.
MUTUALS_UNBOUNDED_LIMIT = 1_000_000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _add_count_arg(default: int) -> ArgsBuilder:
    """Build a parser hook adding an optional positional `count` (default `default`)."""

    def builder(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "count",
            nargs="?",
            type=int,
            default=default,
            help=f"number of users to fetch (default {default})",
        )

    return builder


def _resolve_count(ctx: CommandContext, default: int) -> int:
    """Pick the effective `N`: global `--limit` wins over positional `count`."""
    if ctx.limit is not None:
        return int(ctx.limit)
    return int(getattr(ctx.args, "count", default))


def _user_rows(users: Sequence[User]) -> list[dict[str, Any]]:
    """Flatten users to CSV-friendly rows (rank starts at 1)."""
    return [
        {
            "rank": i,
            "pk": u.pk,
            "username": u.username,
            "full_name": u.full_name,
            "is_private": u.is_private,
            "is_verified": u.is_verified,
        }
        for i, u in enumerate(users, 1)
    ]


def _resolve_dest(ctx: CommandContext, *, fmt: str) -> Path | IO[bytes] | None:
    arg = ctx.args.json if fmt == "json" else ctx.args.csv
    return resolve_export_dest(arg if arg is not None else "")


def _export_users(
    ctx: CommandContext,
    *,
    users: Sequence[User],
    command_name: str,
    target: str,
) -> bool:
    """Emit users in the requested export format. Returns True if exported."""
    fmt = ctx.output_format()
    if fmt == "json":
        ctx.facade.export_json(
            [dataclasses.asdict(u) for u in users],
            command=command_name,
            target=target,
            dest=_resolve_dest(ctx, fmt="json"),
        )
        return True
    if fmt == "csv":
        ctx.facade.export_csv(
            _user_rows(users),
            command=command_name,
            target=target,
            dest=_resolve_dest(ctx, fmt="csv"),
        )
        return True
    return False


# ---------------------------------------------------------------------------
# /followers and /followings
# ---------------------------------------------------------------------------


@command(
    "followers",
    "Fetch the first N followers of the active target (default 50)",
    add_args=_add_count_arg(50),
    csv=True,
)
@with_target
async def followers_cmd(ctx: CommandContext, username: str) -> list[User]:
    n = _resolve_count(ctx, 50)
    users = await ctx.facade.followers(username, limit=n)
    if _export_users(ctx, users=users, command_name="followers", target=username):
        return users
    if not users:
        ctx.print(f"@{username} has no followers")
        return users
    ctx.print(render_user_table(users, title=f"followers of @{username} ({len(users)})"))
    return users


@command(
    "followings",
    "Fetch the first N accounts the active target follows (default 50)",
    add_args=_add_count_arg(50),
    csv=True,
)
@with_target
async def followings_cmd(ctx: CommandContext, username: str) -> list[User]:
    n = _resolve_count(ctx, 50)
    users = await ctx.facade.followings(username, limit=n)
    if _export_users(ctx, users=users, command_name="followings", target=username):
        return users
    if not users:
        ctx.print(f"@{username} follows nobody")
        return users
    ctx.print(render_user_table(users, title=f"@{username} follows ({len(users)})"))
    return users


# ---------------------------------------------------------------------------
# /similar
# ---------------------------------------------------------------------------


@command(
    "similar",
    "Show suggested similar accounts for the active target",
    csv=True,
)
@with_target
async def similar_cmd(ctx: CommandContext, username: str) -> list[User]:
    users = await ctx.facade.similar(username)
    if ctx.limit is not None and ctx.limit > 0:
        users = users[: int(ctx.limit)]
    if _export_users(ctx, users=users, command_name="similar", target=username):
        return users
    if not users:
        ctx.print(f"@{username} has no suggested accounts")
        return users
    ctx.print(render_user_table(users, title=f"similar to @{username} ({len(users)})"))
    return users


# ---------------------------------------------------------------------------
# /mutuals
# ---------------------------------------------------------------------------


def _resolve_mutuals_limit(ctx: CommandContext) -> int:
    """Translate `--limit` into a per-side cap for /mutuals.

    * unset                → MUTUALS_DEFAULT_LIMIT (1000 each side)
    * `--limit N` (N > 0)  → N each side
    * `--limit 0`          → MUTUALS_UNBOUNDED_LIMIT (effectively no cap)
    * negative             → falls back to default; argparse already guards int
    """
    raw = ctx.limit
    if raw is None:
        return MUTUALS_DEFAULT_LIMIT
    if raw == 0:
        return MUTUALS_UNBOUNDED_LIMIT
    if raw < 0:
        return MUTUALS_DEFAULT_LIMIT
    return int(raw)


def _mutuals_truncated_note(result: MutualsResult, *, side_limit: int) -> str | None:
    """Return a one-line warning when either side filled the cap, else None."""
    if side_limit >= MUTUALS_UNBOUNDED_LIMIT:
        return None
    foll_full = result.follower_analyzed >= side_limit
    folw_full = result.following_analyzed >= side_limit
    if not (foll_full or folw_full):
        return None
    return (
        f"(truncated at {result.follower_analyzed} followers / "
        f"{result.following_analyzed} following — pass --limit to widen)"
    )


@command(
    "mutuals",
    "Show users in both the active target's followers and following lists",
    csv=True,
)
@with_target
async def mutuals_cmd(ctx: CommandContext, username: str) -> MutualsResult:
    side_limit = _resolve_mutuals_limit(ctx)
    result = await ctx.facade.mutuals(
        username,
        follower_limit=side_limit,
        following_limit=side_limit,
    )

    fmt = ctx.output_format()
    if fmt == "json":
        ctx.facade.export_json(
            dataclasses.asdict(result),
            command="mutuals",
            target=username,
            dest=_resolve_dest(ctx, fmt="json"),
        )
        return result
    if fmt == "csv":
        ctx.facade.export_csv(
            _user_rows(result.items),
            command="mutuals",
            target=username,
            dest=_resolve_dest(ctx, fmt="csv"),
        )
        return result

    if result.empty or not result.items:
        ctx.print(f"@{username} has no mutuals in the analysed window")
    else:
        ctx.print(
            render_user_table(
                result.items, title=f"mutuals of @{username} ({len(result.items)})"
            )
        )
    note = _mutuals_truncated_note(result, side_limit=side_limit)
    if note is not None:
        ctx.print(note)
    return result


__all__ = [
    "MUTUALS_DEFAULT_LIMIT",
    "MUTUALS_UNBOUNDED_LIMIT",
    "followers_cmd",
    "followings_cmd",
    "mutuals_cmd",
    "similar_cmd",
]
