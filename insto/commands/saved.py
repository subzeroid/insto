"""Read-only saved-media commands.

These commands are aiograpi-only. They expose saved collections and saved
media reads, but no save, unsave, collection-create, edit, delete, or other
account-mutation flows.
"""

from __future__ import annotations

import argparse
import dataclasses
from pathlib import Path
from typing import IO, Any

from rich.table import Table

from insto.commands._base import CommandContext, command, resolve_export_dest
from insto.models import Post, SavedCollection
from insto.ui.render import render_media_grid


def _add_collections_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "count",
        nargs="?",
        type=int,
        default=20,
        help="number of saved collections to list (default 20)",
    )


def _add_saved_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--collection",
        default="",
        help="saved collection id or exact name; omit for all saved posts",
    )
    parser.add_argument(
        "count",
        nargs="?",
        type=int,
        default=20,
        help="number of saved posts to list (default 20)",
    )


def _resolve_count(ctx: CommandContext, default: int = 20) -> int:
    if ctx.limit is not None:
        return int(ctx.limit) if ctx.limit > 0 else default
    return int(getattr(ctx.args, "count", default))


def _resolve_json_dest(ctx: CommandContext) -> Path | IO[bytes] | None:
    return resolve_export_dest(ctx.args.json if ctx.args.json is not None else "")


def _resolve_csv_dest(ctx: CommandContext) -> Path | IO[bytes] | None:
    return resolve_export_dest(ctx.args.csv if ctx.args.csv is not None else "")


def _collection_row(collection: SavedCollection) -> dict[str, Any]:
    return dataclasses.asdict(collection)


def _post_row(post: Post) -> dict[str, Any]:
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


def _render_collections(collections: list[SavedCollection]) -> Table:
    table = Table(title=f"Saved collections ({len(collections)})")
    table.add_column("Collection ID", style="cyan", no_wrap=True)
    table.add_column("Name")
    table.add_column("Type", no_wrap=True)
    table.add_column("Media", justify="right")
    for collection in collections:
        table.add_row(
            collection.pk,
            collection.name,
            collection.collection_type,
            str(collection.media_count),
        )
    return table


@command(
    "collections",
    "List saved collections for the logged-in aiograpi account",
    add_args=_add_collections_args,
    csv=True,
    requires=("saved_read",),
)
async def collections_cmd(ctx: CommandContext) -> list[SavedCollection]:
    count = _resolve_count(ctx)
    collections = await ctx.facade.saved_collections(limit=count)
    fmt = ctx.output_format()

    if fmt == "csv":
        ctx.facade.export_csv(
            [_collection_row(collection) for collection in collections],
            command="collections",
            target=None,
            dest=_resolve_csv_dest(ctx),
        )
        return collections

    if fmt == "json":
        ctx.facade.export_json(
            [dataclasses.asdict(collection) for collection in collections],
            command="collections",
            target=None,
            dest=_resolve_json_dest(ctx),
        )
        return collections

    if not collections:
        ctx.print("no saved collections found")
        return collections
    ctx.print(_render_collections(collections))
    return collections


@command(
    "saved",
    "List saved posts for the logged-in aiograpi account",
    add_args=_add_saved_args,
    csv=True,
    requires=("saved_read",),
)
async def saved_cmd(ctx: CommandContext) -> list[Post]:
    count = _resolve_count(ctx)
    collection = str(getattr(ctx.args, "collection", "") or "").strip() or None
    posts = await ctx.facade.saved_posts(collection=collection, limit=count)
    fmt = ctx.output_format()

    if fmt == "csv":
        ctx.facade.export_csv(
            [_post_row(post) for post in posts],
            command="saved",
            target=None,
            dest=_resolve_csv_dest(ctx),
        )
        return posts

    if fmt == "json":
        ctx.facade.export_json(
            [dataclasses.asdict(post) for post in posts],
            command="saved",
            target=None,
            dest=_resolve_json_dest(ctx),
        )
        return posts

    label = f"saved collection {collection}" if collection else "saved posts"
    if not posts:
        ctx.print(f"no {label} found")
        return posts
    ctx.print(render_media_grid(posts, title=f"{label} ({len(posts)})"))
    return posts
