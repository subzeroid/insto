"""Watch / diff / history commands: `/watch`, `/unwatch`, `/watching`, `/diff`, `/history`.

`/watch <user> [interval]` registers a per-session task on the facade's
`WatchManager` that periodically takes a fresh snapshot of `<user>` and
prints any field-level diff against the previous one. The interval has a
five-minute floor; the manager itself caps simultaneous watches at three.
Watches are session-only — they do not survive REPL exit.

`/diff <user>` is the one-shot equivalent of a watch tick: take a fresh
profile, diff against the most recent stored snapshot, then store the new
snapshot. `/history` reads the last N rows from the sqlite `cli_history`
table (the same table that powers the welcome screen's recent targets).

The watch tick uses `prompt_toolkit.patch_stdout` so that a notification
printed mid-tick does not corrupt the user's in-progress prompt line. When
the patch_stdout context manager is unavailable (one-shot CLI / unit
tests), notifications fall through to the supplied console directly.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
from collections.abc import Iterator
from typing import Any

from insto.commands._base import (
    CommandContext,
    CommandUsageError,
    _validate_username,
    command,
    resolve_export_dest,
    with_target,
)
from insto.service.watch import WatchError

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


@contextlib.contextmanager
def _patched_stdout() -> Iterator[None]:
    """Best-effort `prompt_toolkit.patch_stdout`; no-op outside a REPL."""
    try:
        from prompt_toolkit.patch_stdout import patch_stdout
    except Exception:  # pragma: no cover - prompt_toolkit always installed in v0.1
        yield
        return
    try:
        with patch_stdout(raw=True):
            yield
    except Exception:
        # patch_stdout requires a running prompt application; outside of one
        # it raises. Fall through silently — the caller's print still works.
        yield


def _format_diff(username: str, diff: dict[str, Any]) -> str:
    """Compact one-paragraph rendering of `history.diff(...)` output."""
    if diff.get("first_seen"):
        return f"@{username}: first snapshot — no prior state to diff against"
    changes = diff.get("changes") or {}
    prior = diff.get("previous_usernames") or []
    if not changes and not prior:
        return f"@{username}: no changes"
    parts: list[str] = []
    for field_name in sorted(changes):
        delta = changes[field_name]
        old = delta.get("old")
        new = delta.get("new")
        parts.append(f"{field_name}: {old!r} -> {new!r}")
    if prior:
        parts.append(f"aliases: {', '.join(prior)}")
    return f"@{username} changed — " + "; ".join(parts)


def _build_tick(
    ctx: CommandContext,
    username: str,
    *,
    notify: bool = True,
) -> Any:
    """Construct the per-tick coroutine factory for `_user_`.

    The closure captures the facade (not the context) so it survives the
    end of the originating command. `notify=False` is used by tests when
    they only care about state mutation.
    """
    facade = ctx.facade
    console = ctx.console

    async def tick() -> None:
        # Single profile fetch per tick — diff first, then persist the fresh
        # snapshot so the next tick compares against the freshest one and we
        # never report the same change twice.
        diff = await facade.diff_and_snapshot(username)
        if not notify or console is None:
            return
        message = _format_diff(username, diff)
        with _patched_stdout():
            console.print(message)

    return tick


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
    username = _validate_username(username)

    interval = (
        int(ctx.args.interval) if ctx.args.interval is not None else DEFAULT_WATCH_INTERVAL_SECONDS
    )
    if interval < MIN_WATCH_INTERVAL_SECONDS:
        raise CommandUsageError(
            f"interval must be at least {MIN_WATCH_INTERVAL_SECONDS} seconds (got {interval})"
        )

    tick = _build_tick(ctx, username)
    try:
        spec = ctx.facade.watches.add(username, interval, tick=tick)
    except WatchError as exc:
        raise CommandUsageError(str(exc)) from exc

    payload = dataclasses.asdict(spec)
    ctx.print(
        f"watching @{username} every {interval}s "
        f"({len(ctx.facade.watches)}/{ctx.facade.watches.max_watches})"
    )
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
    username = _validate_username(username)
    removed = ctx.facade.watches.remove(username)
    if not removed:
        ctx.print(f"@{username} is not being watched")
        return False
    ctx.print(f"unwatched @{username}")
    return True


# ---------------------------------------------------------------------------
# /watching
# ---------------------------------------------------------------------------


@command("watching", "List active watches for this session")
async def watching_cmd(ctx: CommandContext) -> list[dict[str, Any]]:
    specs = ctx.facade.watches.list()
    rows = [dataclasses.asdict(s) for s in specs]
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
            suffix = f"  err={spec.last_error}"
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
    ctx.print(_format_diff(username, diff))
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
