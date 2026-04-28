"""Operational / meta commands: `/quota`, `/health`, `/config`, `/purge`.

These commands inspect or mutate session-local state — the backend's quota
snapshot, the most recent backend error, the resolved config, or the sqlite
stores. They do not hit the network beyond what the backend already cached
and they never download media.

`/purge` is the only mutating command. It refuses to run without an
interactive `y/N` confirmation unless `--yes` is passed (which the global
parser already exposes), and it dispatches to the relevant store based on
the positional `kind` argument:

    /purge history    → wipe `cli_history`
    /purge snapshots  → wipe `snapshots` (optionally for one --user)
    /purge cache      → wipe `./output/` (downloaded media + exports)

Watches are intentionally not purgeable here — they are user-declared
intent, not cache, and the user already has `/unwatch` for that.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import shutil
from pathlib import Path
from typing import Any

from insto.commands._base import (
    CommandContext,
    CommandUsageError,
    command,
    resolve_export_dest,
)
from insto.config import effective_config_report

# ---------------------------------------------------------------------------
# /quota
# ---------------------------------------------------------------------------


@command("quota", "Show the current backend quota / balance")
async def quota_cmd(ctx: CommandContext) -> dict[str, Any]:
    # Fresh fetch so /quota always reflects the live balance, not a stale
    # header captured from the previous command's response. Backends that
    # do not implement refresh_quota (e.g. FakeBackend, aiograpi v0.2)
    # silently fall back to the cached value.
    refresh = getattr(ctx.facade.backend, "refresh_quota", None)
    if refresh is not None:
        with contextlib.suppress(Exception):
            await refresh()
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
    rem = "?" if quota.remaining is None else f"{quota.remaining:,}"
    parts = [f"requests left: {rem}"]
    if quota.amount is not None and quota.currency:
        sym = {"USD": "$", "EUR": "€", "GBP": "£"}.get(quota.currency.upper(), quota.currency + " ")
        parts.append(f"balance: {sym}{quota.amount:,.2f}")
    if quota.rate is not None:
        parts.append(f"rate: {quota.rate} rps")
    ctx.print(" | ".join(parts))
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
    schema_drifts = ctx.facade.backend.get_schema_drift_count()
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
# /theme
# ---------------------------------------------------------------------------


def _add_theme_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "name",
        nargs="?",
        default=None,
        help="theme to switch to; omit to show the active theme + list",
    )


@command(
    "theme",
    "Show or switch the colour theme (persists in ~/.insto/config.toml)",
    add_args=_add_theme_args,
)
async def theme_cmd(ctx: CommandContext) -> dict[str, Any]:
    from insto.config import write_config
    from insto.ui.theme import is_known, list_themes

    requested = getattr(ctx.args, "name", None)
    available = list_themes()
    current = ctx.facade.config.theme

    if requested is None:
        # Read-only: print the active theme and the catalog.
        ctx.print(f"active theme: {current}")
        ctx.print("available: " + ", ".join(available))
        return {"active": current, "available": available, "switched": False}

    if not is_known(requested):
        raise CommandUsageError(
            f"unknown theme {requested!r}; available: " + ", ".join(available)
        )

    if requested == current:
        ctx.print(f"theme already {current!r}")
        return {"active": current, "available": available, "switched": False}

    # Persist to ~/.insto/config.toml so the choice survives the next launch.
    # Round-trip through a structured payload so we don't drop other keys.
    cfg = ctx.facade.config
    payload: dict[str, Any] = {"theme": requested}
    if cfg.hiker_token or cfg.hiker_proxy:
        hiker: dict[str, Any] = {}
        if cfg.hiker_token:
            hiker["token"] = cfg.hiker_token
        if cfg.hiker_proxy:
            hiker["proxy"] = cfg.hiker_proxy
        payload["hiker"] = hiker
    if cfg.sources.get("output_dir") != "default":
        payload["output_dir"] = str(cfg.output_dir)
    if cfg.sources.get("db_path") != "default":
        payload["db_path"] = str(cfg.db_path)
    write_config(payload)

    cfg.theme = requested
    ctx.print(
        f"theme: {current} → {requested}. "
        "Restart `insto` to apply across the welcome banner and prompt popup."
    )
    return {"active": requested, "previous": current, "available": available, "switched": True}


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
        help="restrict snapshot purge to one username or pk (snapshots only)",
    )


async def _confirm(ctx: CommandContext, message: str) -> bool:
    """Interactive y/N prompt; the caller must short-circuit on `--yes`."""
    ctx.print(message + " [y/N]")
    answer = await asyncio.to_thread(input, "")
    return answer.strip().lower() in {"y", "yes"}


def _purge_output_dir(output_dir: Path) -> int:
    """Recursively remove every entry directly under `output_dir`.

    The directory itself is preserved so subsequent commands keep a stable
    write target. Returns the number of top-level entries removed. Missing
    or empty trees count as zero deletions, never as errors. A failure on
    one entry does not abort the rest of the purge — that would leave the
    user with a half-cleaned tree and no actionable signal.
    """
    if not output_dir.exists() or not output_dir.is_dir():
        return 0
    removed = 0
    for entry in output_dir.iterdir():
        try:
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        except OSError:
            continue
        removed += 1
    return removed


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
        confirmed = await _confirm(ctx, f"about to permanently delete {target_label}; continue?")
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
    else:  # cache — wipe the on-disk media/export tree per spec §10
        output_dir = ctx.facade.config.output_dir
        deleted = await asyncio.to_thread(_purge_output_dir, output_dir)
        result = {"kind": "cache", "deleted": deleted, "output_dir": str(output_dir)}
        ctx.print(f"deleted {deleted} entr(ies) under {output_dir}")

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


# ---------------------------------------------------------------------------
# /help
# ---------------------------------------------------------------------------


@command("help", "List every registered command with its one-line description")
async def help_cmd(ctx: CommandContext) -> list[dict[str, str]]:
    from insto.commands._base import COMMANDS, command_signature

    rows = [
        {
            "name": name,
            "signature": command_signature(spec),
            "help": spec.help,
        }
        for name, spec in sorted(COMMANDS.items())
    ]
    fmt = ctx.output_format()
    if fmt == "json":
        dest_arg = ctx.args.json if ctx.args.json is not None else ""
        ctx.facade.export_json(
            rows,
            command="help",
            target=None,
            dest=resolve_export_dest(dest_arg),
        )
        return rows
    width = max(len(r["signature"]) for r in rows) if rows else 8
    for row in rows:
        ctx.print(f"{row['signature']:<{width}}  — {row['help']}")
    return rows


__all__ = [
    "config_cmd",
    "health_cmd",
    "help_cmd",
    "purge_cmd",
    "quota_cmd",
    "theme_cmd",
]
