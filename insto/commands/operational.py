"""Operational / meta commands: `/quota`, `/health`, `/config`, `/purge`.

These commands inspect or mutate session-local state — the backend's quota
snapshot, the most recent backend error, the resolved config, or the sqlite
stores. They do not hit the network beyond what the backend already cached
and they never download media.

`/purge` is the only mutating command. It refuses to run without an
interactive `y/N` confirmation unless `--yes` is passed (which the global
parser already exposes), and it dispatches to the relevant `HistoryStore`
purge method based on the positional `kind` argument:

    /purge history    → wipe `cli_history`
    /purge snapshots  → wipe `snapshots` (optionally for one --user)
    /purge cache      → wipe both `cli_history` and `snapshots`

Watches are intentionally not purgeable here — they are user-declared
intent, not cache, and the user already has `/unwatch` for that.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
from typing import Any

from insto.commands._base import (
    CommandContext,
    CommandUsageError,
    command,
    resolve_export_dest,
)
from insto.config import effective_config_report
from insto.exceptions import SchemaDrift

# ---------------------------------------------------------------------------
# /quota
# ---------------------------------------------------------------------------


@command("quota", "Show the last-known backend quota snapshot")
async def quota_cmd(ctx: CommandContext) -> dict[str, Any]:
    quota = ctx.facade.quota()
    payload = dataclasses.asdict(quota)
    fmt = ctx.output_format()
    if fmt == "json":
        dest_arg = ctx.args.json if ctx.args.json is not None else ""
        ctx.facade.export_json(
            payload,
            command="quota",
            target=None,
            dest=resolve_export_dest(dest_arg),
        )
        return payload
    rem = "?" if quota.remaining is None else str(quota.remaining)
    lim = "?" if quota.limit is None else str(quota.limit)
    reset = "?" if quota.reset_at is None else str(quota.reset_at)
    ctx.print(f"quota: remaining={rem} limit={lim} reset_at={reset}")
    return payload


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------


def _format_last_error(err: BaseException | None) -> str:
    if err is None:
        return "—"
    return f"{type(err).__name__}: {err}"


@command("health", "Ping the backend and report quota + last error + drift count")
async def health_cmd(ctx: CommandContext) -> dict[str, Any]:
    quota = ctx.facade.quota()
    last_err = ctx.facade.last_error()
    schema_drifts = 1 if isinstance(last_err, SchemaDrift) else 0
    payload: dict[str, Any] = {
        "backend": type(ctx.facade.backend).__name__,
        "quota": dataclasses.asdict(quota),
        "last_error": _format_last_error(last_err),
        "schema_drifts": schema_drifts,
    }
    fmt = ctx.output_format()
    if fmt == "json":
        dest_arg = ctx.args.json if ctx.args.json is not None else ""
        ctx.facade.export_json(
            payload,
            command="health",
            target=None,
            dest=resolve_export_dest(dest_arg),
        )
        return payload
    rem = "?" if quota.remaining is None else str(quota.remaining)
    ctx.print(f"backend: {payload['backend']}")
    ctx.print(f"quota remaining: {rem}")
    ctx.print(f"last error: {payload['last_error']}")
    ctx.print(f"schema drifts: {schema_drifts}")
    return payload


# ---------------------------------------------------------------------------
# /config
# ---------------------------------------------------------------------------


@command("config", "Show effective configuration with per-key origin")
async def config_cmd(ctx: CommandContext) -> list[dict[str, Any]]:
    rows = effective_config_report(ctx.facade.config)
    fmt = ctx.output_format()
    if fmt == "json":
        dest_arg = ctx.args.json if ctx.args.json is not None else ""
        ctx.facade.export_json(
            rows,
            command="config",
            target=None,
            dest=resolve_export_dest(dest_arg),
        )
        return rows
    for row in rows:
        value = row["value"]
        display = "—" if value is None else str(value)
        ctx.print(f"{row['key']:<22} {display:<40} [{row['origin']}]")
    return rows


# ---------------------------------------------------------------------------
# /purge
# ---------------------------------------------------------------------------


_PURGE_KINDS = ("history", "snapshots", "cache")


def _add_purge_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "kind",
        choices=_PURGE_KINDS,
        help="which store to wipe",
    )
    parser.add_argument(
        "--user",
        default=None,
        help="restrict snapshot purge to one target_pk (snapshots only)",
    )


async def _confirm(ctx: CommandContext, message: str) -> bool:
    """Interactive y/N prompt; the caller must short-circuit on `--yes`."""
    ctx.print(message + " [y/N]")
    answer = await asyncio.to_thread(input, "")
    return answer.strip().lower() in {"y", "yes"}


@command(
    "purge",
    "Wipe sqlite-backed history, snapshots, or both (cache)",
    add_args=_add_purge_args,
)
async def purge_cmd(ctx: CommandContext) -> dict[str, Any]:
    kind = str(ctx.args.kind)
    user_filter = ctx.args.user
    if user_filter is not None and kind != "snapshots":
        raise CommandUsageError("--user can only be combined with /purge snapshots")

    target_label = "snapshots for that user" if user_filter else f"all {kind} entries"
    if not ctx.yes:
        confirmed = await _confirm(
            ctx, f"about to permanently delete {target_label}; continue?"
        )
        if not confirmed:
            ctx.print("aborted")
            return {"kind": kind, "deleted": 0, "aborted": True}

    history = ctx.facade.history
    if kind == "history":
        deleted = await asyncio.to_thread(history.purge_history)
        result: dict[str, Any] = {"kind": "history", "deleted": deleted}
        ctx.print(f"deleted {deleted} cli_history row(s)")
    elif kind == "snapshots":
        deleted = await asyncio.to_thread(history.purge_snapshots, user_filter)
        result = {"kind": "snapshots", "deleted": deleted, "user": user_filter}
        scope = f" for {user_filter}" if user_filter else ""
        ctx.print(f"deleted {deleted} snapshot row(s){scope}")
    else:  # cache
        counts = await asyncio.to_thread(history.purge_cache)
        result = {"kind": "cache", **counts}
        ctx.print(
            f"deleted {counts['cli_history_deleted']} cli_history row(s) "
            f"and {counts['snapshots_deleted']} snapshot row(s)"
        )

    fmt = ctx.output_format()
    if fmt == "json":
        dest_arg = ctx.args.json if ctx.args.json is not None else ""
        ctx.facade.export_json(
            result,
            command="purge",
            target=None,
            dest=resolve_export_dest(dest_arg),
        )
    return result


__all__ = [
    "config_cmd",
    "health_cmd",
    "purge_cmd",
    "quota_cmd",
]
