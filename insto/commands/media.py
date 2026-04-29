"""Media-group commands: `/stories`, `/highlights`, `/posts`, `/reels`, `/tagged`.

Every command in this module fetches DTOs through the facade, then either
exports JSON, prints CDN URLs (`--no-download`), or streams the media to
`./output/<user>/<type>/` via `OsintFacade.download_*`. The CDN streamer
applies per-resource byte budget, host allowlist, and atomic writes — the
command layer never touches raw HTTP.

`--no-download` always wins over the default download path and never writes
to disk; `--json` (mutually exclusive with `--csv` at the global level) wins
over both and writes the schema-wrapped JSON envelope. `/highlights` is
JSON-only when exporting because its output is non-flat.
"""

from __future__ import annotations

import argparse
import dataclasses
from pathlib import Path
from typing import Any

from insto.commands._base import (
    ArgsBuilder,
    CommandContext,
    CommandUsageError,
    add_target_arg,
    command,
    compose_args,
    resolve_export_dest,
    with_target,
)
from insto.models import Post, Story
from insto.ui.render import render_highlights_tree, render_media_grid

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
            help=f"number of items to fetch (default {default})",
        )

    return builder


def _resolve_count(ctx: CommandContext, default: int) -> int:
    """Pick the effective `N`: global `--limit` wins over positional `count`."""
    if ctx.limit is not None:
        return int(ctx.limit)
    return int(getattr(ctx.args, "count", default))


def _export_json(
    ctx: CommandContext,
    payload: Any,
    *,
    command_name: str,
    target: str,
) -> Path | None:
    dest_arg = ctx.args.json if ctx.args.json is not None else ""
    dest = resolve_export_dest(dest_arg)
    return ctx.facade.export_json(
        payload,
        command=command_name,
        target=target,
        dest=dest,
    )


def _post_to_flat_row(post: Post) -> dict[str, Any]:
    return {
        "pk": post.pk,
        "code": post.code,
        "taken_at": post.taken_at,
        "media_type": post.media_type,
        "owner_username": post.owner_username or "",
        "like_count": post.like_count,
        "comment_count": post.comment_count,
        "caption": post.caption,
        "location_name": post.location_name or "",
        "hashtags": post.hashtags,
        "mentions": post.mentions,
        "media_urls": post.media_urls,
    }


async def _emit_posts(
    ctx: CommandContext,
    *,
    posts: list[Post],
    username: str,
    command_name: str,
    title: str,
) -> Any:
    """Shared output path for `/posts`, `/reels`, `/tagged`.

    Four modes, picked in this order:

      1. `--csv`            → flatten to one row per post and write CSV.
      2. `--json`           → write JSON envelope, no render, no download.
      3. `--no-download`    → print one URL per media file, no render.
      4. default            → render media grid, then download every URL via
                              the facade CDN streamer.

    CSV is only declared flat for `posts` in `CSV_FLAT_COMMANDS`; reels /
    tagged still reject `--csv` at the global-flag check.
    """
    fmt = ctx.output_format()
    if fmt == "csv":
        rows = [_post_to_flat_row(p) for p in posts]
        dest_arg = ctx.args.csv if ctx.args.csv is not None else ""
        ctx.facade.export_csv(
            rows,
            command=command_name,
            target=username,
            dest=resolve_export_dest(dest_arg),
        )
        return posts

    if fmt == "json":
        _export_json(
            ctx,
            [dataclasses.asdict(p) for p in posts],
            command_name=command_name,
            target=username,
        )
        return posts

    if not posts:
        ctx.print(f"@{username} has no {command_name}")
        return posts

    if ctx.no_download:
        for post in posts:
            for url in post.media_urls:
                print(url)
        return posts

    ctx.print(render_media_grid(posts, title=title))
    saved: list[Path] = []
    for post in posts:
        paths = await ctx.facade.download_post_media(post)
        saved.extend(paths)
        for path in paths:
            ctx.print(f"saved {path}")
    return saved


# ---------------------------------------------------------------------------
# /stories
# ---------------------------------------------------------------------------


@command(
    "stories",
    "Download active stories of the active target",
    add_args=add_target_arg,
)
@with_target
async def stories_cmd(ctx: CommandContext, username: str) -> Any:
    stories: list[Story] = await ctx.facade.user_stories(username, limit=ctx.limit)

    if ctx.output_format() == "json":
        _export_json(
            ctx,
            [dataclasses.asdict(s) for s in stories],
            command_name="stories",
            target=username,
        )
        return stories

    if not stories:
        ctx.print(f"@{username} has no active stories")
        return stories

    if ctx.no_download:
        for story in stories:
            print(story.media_url)
        return stories

    saved: list[Path] = []
    for story in stories:
        path = await ctx.facade.download_story(story)
        saved.append(path)
        ctx.print(f"saved {path}")
    return saved


# ---------------------------------------------------------------------------
# /highlights
# ---------------------------------------------------------------------------


def _add_highlights_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--download",
        type=int,
        default=None,
        metavar="N",
        help="download all items of the Nth highlight (1-indexed)",
    )


@command(
    "highlights",
    "List highlights or download items of the Nth one with --download N",
    add_args=compose_args(add_target_arg, _add_highlights_args),
)
@with_target
async def highlights_cmd(ctx: CommandContext, username: str) -> Any:
    highlights = await ctx.facade.user_highlights(username, limit=ctx.limit)

    if ctx.output_format() == "json":
        _export_json(
            ctx,
            [dataclasses.asdict(h) for h in highlights],
            command_name="highlights",
            target=username,
        )
        return highlights

    if ctx.args.download is None:
        if not highlights:
            ctx.print(f"@{username} has no highlights")
        else:
            ctx.print(render_highlights_tree(highlights))
        return highlights

    n = int(ctx.args.download)
    if n < 1 or n > len(highlights):
        raise CommandUsageError(f"--download {n}: out of range (have {len(highlights)} highlights)")
    chosen = highlights[n - 1]
    items = await ctx.facade.highlight_items(chosen.pk)

    if ctx.no_download:
        for item in items:
            print(item.media_url)
        return items

    saved: list[Path] = []
    for item in items:
        path = await ctx.facade.download_highlight_item(item, owner_username=username)
        saved.append(path)
        ctx.print(f"saved {path}")
    return saved


# ---------------------------------------------------------------------------
# /posts, /reels, /tagged
# ---------------------------------------------------------------------------


@command(
    "posts",
    "Fetch the last N feed posts of the active target (default 12)",
    add_args=_add_count_arg(12),
    csv=True,
)
@with_target
async def posts_cmd(ctx: CommandContext, username: str) -> Any:
    n = _resolve_count(ctx, 12)
    posts = await ctx.facade.user_posts(username, limit=n)
    return await _emit_posts(
        ctx,
        posts=posts,
        username=username,
        command_name="posts",
        title=f"posts from @{username} (last {n})",
    )


@command(
    "reels",
    "Fetch the last N reels (video posts) of the active target (default 10)",
    add_args=_add_count_arg(10),
)
@with_target
async def reels_cmd(ctx: CommandContext, username: str) -> Any:
    n = _resolve_count(ctx, 10)
    # Reels are video posts in the user feed. The HikerAPI v0.1 backend has
    # no dedicated reels endpoint, so we fetch a wider slice of the feed and
    # filter; the `*3` window keeps the call count low while still finding
    # `n` reels in feeds where most posts are images.
    fetch_window = max(n * 3, 30)
    posts = await ctx.facade.user_posts(username, limit=fetch_window)
    reels = [p for p in posts if p.media_type == "video"][:n]
    return await _emit_posts(
        ctx,
        posts=reels,
        username=username,
        command_name="reels",
        title=f"reels from @{username} (last {n})",
    )


@command(
    "tagged",
    "Fetch the last N posts tagging the active target (default 10)",
    add_args=_add_count_arg(10),
)
@with_target
async def tagged_cmd(ctx: CommandContext, username: str) -> Any:
    n = _resolve_count(ctx, 10)
    posts = await ctx.facade.user_tagged(username, limit=n)
    return await _emit_posts(
        ctx,
        posts=posts,
        username=username,
        command_name="tagged",
        title=f"tagged posts of @{username} (last {n})",
    )


@command(
    "reposts",
    "Fetch posts the active target reposted (IG repost surface, hiker only)",
    add_args=_add_count_arg(20),
)
@with_target
async def reposts_cmd(ctx: CommandContext, username: str) -> Any:
    n = _resolve_count(ctx, 20)
    posts = await ctx.facade.user_reposts(username, limit=n)
    return await _emit_posts(
        ctx,
        posts=posts,
        username=username,
        command_name="reposts",
        title=f"reposts by @{username} (last {n})",
    )


__all__ = [
    "highlights_cmd",
    "posts_cmd",
    "reels_cmd",
    "reposts_cmd",
    "stories_cmd",
    "tagged_cmd",
]
