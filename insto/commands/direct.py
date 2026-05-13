"""Read-only Direct inbox commands.

These commands are aiograpi-only and intentionally expose no write surface:
no send, reaction, seen, unsend, mute, approve, upload, or title-update flows.
"""

from __future__ import annotations

import argparse
import dataclasses
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

from rich.table import Table

from insto.commands._base import CommandContext, command, resolve_export_dest
from insto.models import DirectMessage, DirectThread


def _add_direct_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "count",
        nargs="?",
        type=int,
        default=20,
        help="number of threads to fetch (default 20)",
    )


def _add_direct_thread_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("thread_id", help="Direct thread id")
    parser.add_argument(
        "count",
        nargs="?",
        type=int,
        default=20,
        help="number of messages to fetch (default 20)",
    )


def _resolve_count(ctx: CommandContext, default: int = 20) -> int:
    if ctx.limit is not None:
        return int(ctx.limit) if ctx.limit > 0 else default
    return int(getattr(ctx.args, "count", default))


def _resolve_dest(ctx: CommandContext) -> Path | IO[bytes] | None:
    return resolve_export_dest(ctx.args.json if ctx.args.json is not None else "")


def _format_ts(timestamp: int) -> str:
    if timestamp <= 0:
        return ""
    return datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%d %H:%M:%SZ")


def _participants(thread: DirectThread) -> str:
    names = [user.username for user in thread.users if user.username]
    return ", ".join(names)


def _thread_flags(thread: DirectThread) -> str:
    flags: list[str] = []
    if thread.is_group:
        flags.append("group")
    if thread.is_pending:
        flags.append("pending")
    if thread.is_archived:
        flags.append("archived")
    if thread.is_muted:
        flags.append("muted")
    return ", ".join(flags)


def _message_preview(message: DirectMessage) -> str:
    if message.text:
        return message.text
    refs: list[str] = []
    if message.media_code:
        refs.append(f"media:{message.media_code}")
    elif message.media_pk:
        refs.append(f"media:{message.media_pk}")
    if message.link_url:
        refs.append(message.link_url)
    return " ".join(refs)


def _render_threads(threads: list[DirectThread]) -> Table:
    table = Table(title=f"Direct threads ({len(threads)})")
    table.add_column("Thread ID", style="cyan", no_wrap=True)
    table.add_column("Title")
    table.add_column("Participants")
    table.add_column("Last activity", no_wrap=True)
    table.add_column("Messages", justify="right")
    table.add_column("Flags")
    for thread in threads:
        table.add_row(
            thread.pk,
            thread.title or _participants(thread),
            _participants(thread),
            _format_ts(thread.last_activity_at),
            str(thread.message_count),
            _thread_flags(thread),
        )
    return table


def _render_messages(thread_id: str, messages: list[DirectMessage]) -> Table:
    table = Table(title=f"Direct thread {thread_id} ({len(messages)} messages)")
    table.add_column("Time", no_wrap=True)
    table.add_column("Sender", style="cyan", no_wrap=True)
    table.add_column("Type", no_wrap=True)
    table.add_column("Text / ref")
    for message in messages:
        table.add_row(
            _format_ts(message.timestamp),
            message.sender_pk,
            message.item_type,
            _message_preview(message),
        )
    return table


@command(
    "direct",
    "List read-only Direct threads (aiograpi only)",
    add_args=_add_direct_args,
    requires=("direct_read",),
)
async def direct_cmd(ctx: CommandContext) -> list[DirectThread]:
    count = _resolve_count(ctx)
    threads = await ctx.facade.direct_threads(limit=count)

    if ctx.output_format() == "json":
        ctx.facade.export_json(
            [dataclasses.asdict(thread) for thread in threads],
            command="direct",
            target=None,
            dest=_resolve_dest(ctx),
        )
        return threads

    if not threads:
        ctx.print("no Direct threads found")
        return threads
    ctx.print(_render_threads(threads))
    return threads


@command(
    "direct-thread",
    "Show read-only Direct messages for one thread (aiograpi only)",
    add_args=_add_direct_thread_args,
    requires=("direct_read",),
)
async def direct_thread_cmd(ctx: CommandContext) -> list[DirectMessage]:
    thread_id = str(getattr(ctx.args, "thread_id", "") or "").strip()
    count = _resolve_count(ctx)
    messages = await ctx.facade.direct_messages(thread_id, limit=count)

    if ctx.output_format() == "json":
        ctx.facade.export_json(
            [dataclasses.asdict(message) for message in messages],
            command="direct-thread",
            target=thread_id,
            dest=_resolve_dest(ctx),
        )
        return messages

    if not messages:
        ctx.print(f"thread {thread_id} has no messages")
        return messages
    ctx.print(_render_messages(thread_id, messages))
    return messages
