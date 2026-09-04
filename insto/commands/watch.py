"""Watch / diff / history commands: `/watch`, `/unwatch`, `/watching`, `/diff`, `/history`.

`/watch <user> [interval]` persists a registration in SQLite. A foreground
daemon or REPL executor reconciles that source of truth into local tasks.

`/diff <user>` is the one-shot equivalent of a watch tick: take a fresh
profile, diff against the most recent stored snapshot, then store the new
snapshot. `/history` reads the last N rows from the sqlite `cli_history`
table (the same table that powers the welcome screen's recent targets).

Watcher output is formatted by the scheduler service and routed through the
owning runtime, so registration creates no detached command closure.
"""

from __future__ import annotations

import argparse
from typing import Any

from insto._redact import redact_secrets
from insto.commands._base import (
    CommandContext,
    CommandUsageError,
    _validate_username,
    command,
    resolve_export_dest,
    with_target,
)
from insto.models import WatchSpec
from insto.service.watch import format_watch_diff

MIN_WATCH_INTERVAL_SECONDS = 300
DEFAULT_WATCH_INTERVAL_SECONDS = 300
DEFAULT_HISTORY_LIMIT = 25


# ---------------------------------------------------------------------------
# /watch
# ---------------------------------------------------------------------------


def _add_watch_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "target",
        nargs="?",
        help="Instagram username to watch (defaults to active /target)",
    )
    parser.add_argument(
        "interval",
        nargs="?",
        type=int,
        default=None,
        help=(
            f"poll interval in seconds (default {DEFAULT_WATCH_INTERVAL_SECONDS}, "
            f"min {MIN_WATCH_INTERVAL_SECONDS})"
        ),
    )


def watch_public_dict(spec: WatchSpec) -> dict[str, object]:
    """Return the stable public watch payload without its concurrency token."""
    return {
        "user": spec.user,
        "interval_seconds": spec.interval_seconds,
        "last_ok": spec.last_ok,
        "last_error": redact_secrets(spec.last_error) if spec.last_error else None,
        "consecutive_errors": spec.consecutive_errors,
        "status": spec.status,
    }


@command(
    "watch",
    "Periodically snapshot the active target and notify on changes",
    add_args=_add_watch_args,
)
async def watch_cmd(ctx: CommandContext) -> dict[str, Any]:
    raw = getattr(ctx.args, "target", None)
    if raw:
        username = str(raw).lstrip("@").strip()
    elif ctx.session.target:
        username = ctx.session.target
    else:
        raise CommandUsageError("no target set — pass a username or run /target <user> first")
    if not username:
        raise CommandUsageError("usage: /watch <username> [interval-seconds]")
    username = _validate_username(username).lower()

    interval = (
        int(ctx.args.interval) if ctx.args.interval is not None else DEFAULT_WATCH_INTERVAL_SECONDS
    )
    if interval < MIN_WATCH_INTERVAL_SECONDS:
        raise CommandUsageError(
            f"interval must be at least {MIN_WATCH_INTERVAL_SECONDS} seconds (got {interval})"
        )

    result = await ctx.facade.history.register_watch_async(username, interval)
    if result.kind == "already_active":
        raise CommandUsageError(f"already watching @{username}")
    if result.kind == "full":
        raise CommandUsageError(
            f"too many active watches (max {ctx.facade.watches.max_watches}); "
            "drop one with /unwatch first"
        )
    assert result.spec is not None
    spec = result.spec
    if ctx.facade.watch_daemon is not None:
        ctx.facade.watch_daemon.request_reconcile()

    payload = watch_public_dict(spec)
    active_count = sum(
        row.status == "active" for row in await ctx.facade.history.list_watches_async()
    )
    ctx.print(
        f"watching @{username} every {interval}s "
        f"({active_count}/{ctx.facade.watches.max_watches})"
    )
    if ctx.facade.watch_role == "oneshot":
        ctx.print("registration saved; run `insto watch-daemon` to execute persisted watches")
    return payload


# ---------------------------------------------------------------------------
# /unwatch
# ---------------------------------------------------------------------------


def _add_unwatch_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "target",
        help="username currently being watched",
    )


@command(
    "unwatch",
    "Cancel a running /watch task",
    add_args=_add_unwatch_args,
)
async def unwatch_cmd(ctx: CommandContext) -> bool:
    raw = getattr(ctx.args, "target", None)
    if not raw:
        raise CommandUsageError("usage: /unwatch <username>")
    username = str(raw).lstrip("@").strip()
    if not username:
        raise CommandUsageError("usage: /unwatch <username>")
    username = _validate_username(username).lower()
    removed = await ctx.facade.history.delete_watch_async(username)
    if not removed:
        ctx.print(f"@{username} is not being watched")
        return False
    if ctx.facade.watch_daemon is not None:
        ctx.facade.watch_daemon.request_reconcile()
    ctx.print(f"unwatched @{username}")
    return True


# ---------------------------------------------------------------------------
# /watching
# ---------------------------------------------------------------------------


@command("watching", "List active watches for this session")
async def watching_cmd(ctx: CommandContext) -> list[dict[str, Any]]:
    specs = await ctx.facade.history.list_watches_async()
    rows = [watch_public_dict(spec) for spec in specs]
    fmt = ctx.output_format()
    if fmt == "json":
        dest_arg = ctx.args.json if ctx.args.json is not None else ""
        ctx.facade.export_json(
            rows,
            command="watching",
            target=None,
            dest=resolve_export_dest(dest_arg),
        )
        return rows
    if not specs:
        ctx.print("no active watches")
        return rows
    for spec in specs:
        last_ok = spec.last_ok if spec.last_ok is not None else "—"
        suffix = ""
        if spec.last_error:
            suffix = f"  err={redact_secrets(spec.last_error)}"
        ctx.print(
            f"@{spec.user}  every {spec.interval_seconds}s  "
            f"status={spec.status}  last_ok={last_ok}{suffix}"
        )
    return rows


# ---------------------------------------------------------------------------
# /diff
# ---------------------------------------------------------------------------


def _add_diff_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "target",
        nargs="?",
        help="Instagram username (defaults to active /target)",
    )
    parser.add_argument(
        "--snapshot",
        action="store_true",
        help="store a fresh snapshot after diffing (default: do not store)",
    )


@command(
    "diff",
    "Diff the current profile against the last stored snapshot",
    add_args=_add_diff_args,
)
@with_target
async def diff_cmd(ctx: CommandContext, username: str) -> dict[str, Any]:
    diff = await ctx.facade.diff(username)
    if getattr(ctx.args, "snapshot", False):
        await ctx.facade.snapshot(username)
    fmt = ctx.output_format()
    if fmt == "json":
        dest_arg = ctx.args.json if ctx.args.json is not None else ""
        ctx.facade.export_json(
            diff,
            command="diff",
            target=username,
            dest=resolve_export_dest(dest_arg),
        )
        return diff
    ctx.print(format_watch_diff(username, diff))
    return diff


# ---------------------------------------------------------------------------
# /history
# ---------------------------------------------------------------------------


def _add_history_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "count",
        nargs="?",
        type=int,
        default=DEFAULT_HISTORY_LIMIT,
        help=f"how many recent commands to show (default {DEFAULT_HISTORY_LIMIT})",
    )


@command(
    "history",
    "Show the most recent commands from cli_history",
    add_args=_add_history_args,
)
async def history_cmd(ctx: CommandContext) -> list[dict[str, Any]]:
    n = int(getattr(ctx.args, "count", DEFAULT_HISTORY_LIMIT))
    if ctx.limit is not None:
        n = int(ctx.limit)
    if n <= 0:
        n = DEFAULT_HISTORY_LIMIT
    rows = await ctx.facade.history.recent_commands_async(n)
    fmt = ctx.output_format()
    if fmt == "json":
        dest_arg = ctx.args.json if ctx.args.json is not None else ""
        ctx.facade.export_json(
            rows,
            command="history",
            target=None,
            dest=resolve_export_dest(dest_arg),
        )
        return rows
    if not rows:
        ctx.print("no recorded commands yet")
        return rows
    for row in rows:
        target = f" @{row['target']}" if row["target"] else ""
        ctx.print(f"{row['ts']}  {row['cmd']}{target}")
    return rows


__all__ = [
    "DEFAULT_HISTORY_LIMIT",
    "DEFAULT_WATCH_INTERVAL_SECONDS",
    "MIN_WATCH_INTERVAL_SECONDS",
    "diff_cmd",
    "history_cmd",
    "unwatch_cmd",
    "watch_cmd",
    "watching_cmd",
]
