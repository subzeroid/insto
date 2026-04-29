"""Discovery / utility commands powered by aiograpi 0.8.x ports.

Three commands grouped here because they share a single source of new
capability (aiograpi >= 0.8.x) and don't fit neatly into the existing
``profile`` / ``network`` / ``media`` modules:

  - ``/resolve <url>`` — expand an Instagram short-link
    (``instagram.com/share/...``) to the canonical URL via a HEAD
    request. aiograpi only; HikerAPI raises a clear error.
  - ``/audio <track_id>`` — list clips that use a given audio asset.
    Both backends.
  - ``/recommended`` — IG's "recommended in same category" list for
    the active target. aiograpi only.
"""

from __future__ import annotations

import argparse
import dataclasses
from collections.abc import Sequence
from pathlib import Path
from typing import IO, Any

from insto.commands._base import (
    CommandContext,
    CommandUsageError,
    add_target_arg,
    command,
    resolve_export_dest,
    with_target,
)
from insto.models import Post, User
from insto.ui.render import render_media_grid, render_user_table

# ---------------------------------------------------------------------------
# /resolve
# ---------------------------------------------------------------------------


def _add_resolve_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "url",
        help="short URL to resolve (e.g. https://instagram.com/share/...)",
    )


@command(
    "resolve",
    "Expand an Instagram short-link to its canonical URL (aiograpi only)",
    add_args=_add_resolve_args,
)
async def resolve_cmd(ctx: CommandContext) -> str:
    url = (getattr(ctx.args, "url", "") or "").strip()
    if not url:
        raise CommandUsageError("/resolve needs a URL")
    canonical = await ctx.facade.resolve_short_url(url)
    ctx.print(canonical)
    return canonical


# ---------------------------------------------------------------------------
# /audio
# ---------------------------------------------------------------------------


def _add_audio_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("track_id", help="Instagram audio asset id")
    parser.add_argument(
        "count",
        nargs="?",
        type=int,
        default=20,
        help="number of clips to fetch (default 20)",
    )


def _post_rows(posts: Sequence[Post]) -> list[dict[str, Any]]:
    """Flatten posts to JSON-friendly rows for export."""
    return [dataclasses.asdict(p) for p in posts]


def _resolve_dest(ctx: CommandContext, *, fmt: str) -> Path | IO[bytes] | None:
    arg = ctx.args.json if fmt == "json" else ctx.args.csv
    return resolve_export_dest(arg if arg is not None else "")


@command(
    "audio",
    "List clips using a given audio asset (Instagram audio_asset_id)",
    add_args=_add_audio_args,
)
async def audio_cmd(ctx: CommandContext) -> list[Post]:
    track_id = (getattr(ctx.args, "track_id", "") or "").strip()
    if not track_id:
        raise CommandUsageError("/audio needs a track_id")
    n = int(ctx.limit) if ctx.limit is not None else int(getattr(ctx.args, "count", 20))
    clips = await ctx.facade.audio_clips(track_id, limit=n)

    fmt = ctx.output_format()
    if fmt == "json":
        ctx.facade.export_json(
            _post_rows(clips),
            command="audio",
            target=track_id,
            dest=_resolve_dest(ctx, fmt="json"),
        )
        return clips
    if not clips:
        ctx.print(f"no clips found for audio {track_id}")
        return clips
    ctx.print(render_media_grid(clips, title=f"audio {track_id} ({len(clips)} clips)"))
    return clips


# ---------------------------------------------------------------------------
# /recommended
# ---------------------------------------------------------------------------


@command(
    "recommended",
    "IG's category-based account recommendations for the active target (aiograpi only)",
    csv=True,
    add_args=add_target_arg,
)
@with_target
async def recommended_cmd(ctx: CommandContext, username: str) -> list[User]:
    users = await ctx.facade.recommended(username)
    if ctx.limit is not None and ctx.limit > 0:
        users = users[: int(ctx.limit)]

    fmt = ctx.output_format()
    if fmt == "json":
        ctx.facade.export_json(
            [dataclasses.asdict(u) for u in users],
            command="recommended",
            target=username,
            dest=_resolve_dest(ctx, fmt="json"),
        )
        return users
    if fmt == "csv":
        from insto.commands.network import _user_rows

        ctx.facade.export_csv(
            _user_rows(users),
            command="recommended",
            target=username,
            dest=_resolve_dest(ctx, fmt="csv"),
        )
        return users
    if fmt == "maltego":
        from insto.commands.network import _user_maltego_rows

        ctx.facade.export_maltego(
            _user_maltego_rows(users),
            command="recommended",
            entity_type="user",
            target=username,
        )
        return users

    if not users:
        ctx.print(f"@{username} has no category recommendations (no business category set?)")
        return users
    ctx.print(render_user_table(users, title=f"recommended for @{username} ({len(users)})"))
    return users
