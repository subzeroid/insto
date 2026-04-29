"""Interaction commands: `/comments`, `/wcommented`, `/wtagged`.

The three commands here are about *who interacts with the target* — they
sit one layer above the content-analysis group: instead of looking at the
target's own posts (captions, hashtags), they aggregate the actions of
*other* users (commenters, taggers) over a bounded window of the target's
recent posts.

Window semantics mirror `insto.commands.content`: the effective window is
`--limit N` if provided (positive), else `CONTENT_DEFAULT_WINDOW` (50). The
window is the *post* window — the analytic itself iterates `iter_post_*`
inside the facade, so the comment / tag count is implicitly bounded by
both the post window and the per-call backend cap.

`/comments` is a two-mode command:

- with `post_code` — fetch and dump the comments on that specific post;
  the post is resolved by walking the target's recent feed (`user_posts`)
  within the same bounded window. If the code is not in the window the
  command raises `CommandUsageError` with a helpful hint.
- without — concatenate comments across the recent post window into a
  single flat list. This is the bulk-export view used by analysts who
  want the raw comment stream of a target without resolving each post by
  hand.

All three commands are flat-row CSV-eligible and listed in
`insto.service.exporter.CSV_FLAT_COMMANDS`.
"""

from __future__ import annotations

import argparse
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
from insto.models import Comment
from insto.service.analytics import FansResult, TopList
from insto.ui.render import render_kv

INTERACTIONS_DEFAULT_WINDOW = 50


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_window(ctx: CommandContext) -> int:
    raw = ctx.limit
    if raw is None or raw <= 0:
        return INTERACTIONS_DEFAULT_WINDOW
    return int(raw)


def _resolve_dest(ctx: CommandContext, *, fmt: str) -> Path | IO[bytes] | None:
    arg = ctx.args.json if fmt == "json" else ctx.args.csv
    return resolve_export_dest(arg if arg is not None else "")


def _toplist_rows(result: TopList, *, key_field: str) -> list[dict[str, Any]]:
    return [
        {"rank": i, key_field: key, "count": count}
        for i, (key, count) in enumerate(result.items, 1)
    ]


def _toplist_envelope(result: TopList) -> dict[str, Any]:
    return {
        "target": result.target,
        "kind": result.kind,
        "window": result.window,
        "analyzed": result.analyzed,
        "items": [{"key": k, "count": c} for k, c in result.items],
        "empty": result.empty,
    }


def _truncate(text: str, max_len: int = 60) -> str:
    cleaned = text.replace("\n", " ").strip()
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[: max_len - 1] + "…"


# ---------------------------------------------------------------------------
# /comments
# ---------------------------------------------------------------------------


def _add_post_code_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "post_code",
        nargs="?",
        default=None,
        help="post shortcode (e.g. Cp123); omit to aggregate across recent posts",
    )


def _comment_rows(
    comments: Sequence[Comment],
    *,
    post_code_by_pk: dict[str, str],
) -> list[dict[str, Any]]:
    return [
        {
            "rank": i,
            "post_code": post_code_by_pk.get(c.media_pk, ""),
            "comment_pk": c.pk,
            "user": c.user_username,
            "text": c.text.replace("\n", " ").strip(),
            "like_count": c.like_count,
            "created_at": c.created_at,
        }
        for i, c in enumerate(comments, 1)
    ]


def _comments_envelope(
    *,
    target: str,
    window: int,
    analyzed_posts: int,
    post_code: str | None,
    comments: Sequence[Comment],
    post_code_by_pk: dict[str, str],
) -> dict[str, Any]:
    return {
        "target": target,
        "window": window,
        "analyzed_posts": analyzed_posts,
        "post_code": post_code,
        "items": _comment_rows(comments, post_code_by_pk=post_code_by_pk),
        "empty": len(comments) == 0,
    }


@command(
    "comments",
    "Dump comments on one post (by code) or aggregate across recent posts",
    add_args=_add_post_code_arg,
    csv=True,
)
@with_target
async def comments_cmd(ctx: CommandContext, username: str) -> list[Comment]:
    window = _resolve_window(ctx)
    post_code: str | None = getattr(ctx.args, "post_code", None)

    posts = await ctx.facade.user_posts(username, limit=window)
    post_code_by_pk: dict[str, str] = {p.pk: p.code for p in posts}

    if post_code is not None:
        cleaned = post_code.strip()
        match = next((p for p in posts if p.code == cleaned), None)
        if match is None:
            raise CommandUsageError(
                f"post {cleaned!r} not found in last {window} posts of @{username}"
            )
        # When the user names a single post, treat `--limit` as a comment cap
        # (a single post can have tens of thousands of comments — bound it).
        comments = await ctx.facade.post_comments(match.pk, limit=window)
        analyzed_posts = 1
        header = f"Comments on {match.code} (post by @{username}):"
    else:
        # Aggregate mode: `--limit` caps the post window (already applied to
        # `posts` above). Per-post comment retrieval falls back to the facade
        # default (50/post), so the spec §9 bounded-window guarantee holds —
        # a 50-post celebrity target pulls at most ~2.5k comments, not the
        # entire comment history.
        comments = []
        for post in posts:
            comments.extend(await ctx.facade.post_comments(post.pk))
        analyzed_posts = len(posts)
        header = f"Comments from @{username} (last {window} posts):"

    fmt = ctx.output_format()
    if fmt == "json":
        ctx.facade.export_json(
            _comments_envelope(
                target=username,
                window=window,
                analyzed_posts=analyzed_posts,
                post_code=post_code.strip() if post_code is not None else None,
                comments=comments,
                post_code_by_pk=post_code_by_pk,
            ),
            command="comments",
            target=username,
            dest=_resolve_dest(ctx, fmt="json"),
        )
        return comments
    if fmt == "csv":
        ctx.facade.export_csv(
            _comment_rows(comments, post_code_by_pk=post_code_by_pk),
            command="comments",
            target=username,
            dest=_resolve_dest(ctx, fmt="csv"),
        )
        return comments

    ctx.print(header)
    if not comments:
        if post_code is None and not posts:
            ctx.print(f"no posts to analyze for @{username}")
        else:
            ctx.print("no comments found")
        return comments

    rows: list[tuple[str, str]] = []
    for c in comments:
        prefix = post_code_by_pk.get(c.media_pk) or c.media_pk
        rows.append((f"@{c.user_username}", f"[{prefix}] {_truncate(c.text)}"))
    ctx.print(render_kv(rows, key_label="user", value_label="comment"))
    return comments


# ---------------------------------------------------------------------------
# /wcommented and /wtagged
# ---------------------------------------------------------------------------


def _toplist_maltego_rows(result: TopList) -> list[dict[str, Any]]:
    """Flatten a `TopList` into Maltego-friendly rows (`value` = key, weight = count)."""
    return [
        {"value": key, "weight": count, "rank": i} for i, (key, count) in enumerate(result.items, 1)
    ]


async def _emit_toplist(
    ctx: CommandContext,
    *,
    result: TopList,
    command_name: str,
    key_field: str,
    header: str,
    empty_msg: str,
) -> TopList:
    fmt = ctx.output_format()
    if fmt == "json":
        ctx.facade.export_json(
            _toplist_envelope(result),
            command=command_name,
            target=result.target,
            dest=_resolve_dest(ctx, fmt="json"),
        )
        return result
    if fmt == "csv":
        ctx.facade.export_csv(
            _toplist_rows(result, key_field=key_field),
            command=command_name,
            target=result.target,
            dest=_resolve_dest(ctx, fmt="csv"),
        )
        return result
    if fmt == "maltego":
        ctx.facade.export_maltego(
            _toplist_maltego_rows(result),
            command=command_name,
            entity_type="user",
            target=result.target,
        )
        return result

    ctx.print(header)
    if result.empty:
        ctx.print(empty_msg)
        return result
    if not result.items:
        ctx.print("no entries found in the analysed window")
        return result
    ctx.print(
        render_kv(
            result.items,
            key_label=key_field,
            value_label="count",
        )
    )
    return result


@command(
    "wcommented",
    "Top users commenting on the active target's recent posts",
    csv=True,
    add_args=add_target_arg,
)
@with_target
async def wcommented_cmd(ctx: CommandContext, username: str) -> TopList:
    window = _resolve_window(ctx)
    result = await ctx.facade.wcommented(username, limit=window)
    return await _emit_toplist(
        ctx,
        result=result,
        command_name="wcommented",
        key_field="user",
        header=f"Top commenters on @{username} (last {window} posts):",
        empty_msg=f"no posts to analyze for @{username}",
    )


@command(
    "wtagged",
    "Top users who tagged the active target in their posts",
    csv=True,
    add_args=add_target_arg,
)
@with_target
async def wtagged_cmd(ctx: CommandContext, username: str) -> TopList:
    window = _resolve_window(ctx)
    result = await ctx.facade.wtagged(username, limit=window)
    return await _emit_toplist(
        ctx,
        result=result,
        command_name="wtagged",
        key_field="owner",
        header=f"Users tagging @{username} (last {window} tagged posts):",
        empty_msg=f"no tagged posts to analyze for @{username}",
    )


# ---------------------------------------------------------------------------
# /wliked
# ---------------------------------------------------------------------------


@command(
    "wliked",
    "Top users liking the active target's recent posts",
    csv=True,
    add_args=add_target_arg,
)
@with_target
async def wliked_cmd(ctx: CommandContext, username: str) -> TopList:
    window = _resolve_window(ctx)
    result = await ctx.facade.wliked(username, limit=window)
    return await _emit_toplist(
        ctx,
        result=result,
        command_name="wliked",
        key_field="user",
        header=f"Top likers on @{username} (last {window} posts):",
        empty_msg=f"no posts to analyze for @{username}",
    )


# ---------------------------------------------------------------------------
# /fans — composite likers + commenters ranking
# ---------------------------------------------------------------------------


_FANS_DEFAULT_TOP = 20
_FANS_DEFAULT_COMMENT_WEIGHT = 3


def _fans_envelope(result: FansResult) -> dict[str, Any]:
    return {
        "target": result.target,
        "kind": "fans",
        "window": result.window,
        "analyzed_posts": result.analyzed_posts,
        "comment_weight": result.comment_weight,
        "items": [
            {
                "rank": i,
                "username": row.username,
                "likes": row.likes,
                "comments": row.comments,
                "score": row.score,
            }
            for i, row in enumerate(result.items, 1)
        ],
        "empty": result.empty,
    }


def _fans_csv_rows(result: FansResult) -> list[dict[str, Any]]:
    return [
        {
            "rank": i,
            "user": row.username,
            "likes": row.likes,
            "comments": row.comments,
            "score": row.score,
        }
        for i, row in enumerate(result.items, 1)
    ]


def _fans_maltego_rows(result: FansResult) -> list[dict[str, Any]]:
    """Maltego rows for /fans. Notes carries the human-readable
    breakdown (`12L+3C`) so the Maltego node label is informative
    without requiring the Properties JSON."""
    return [
        {
            "value": row.username,
            "weight": row.score,
            "notes": f"{row.likes}L+{row.comments}C",
            "rank": i,
            "likes": row.likes,
            "comments": row.comments,
            "score": row.score,
        }
        for i, row in enumerate(result.items, 1)
    ]


@command(
    "fans",
    "Top fans (likers + commenters, weighted) across the target's recent posts",
    csv=True,
    add_args=add_target_arg,
)
@with_target
async def fans_cmd(ctx: CommandContext, username: str) -> FansResult:
    window = _resolve_window(ctx)
    result = await ctx.facade.fans(
        username,
        limit=window,
        comment_weight=_FANS_DEFAULT_COMMENT_WEIGHT,
        top=_FANS_DEFAULT_TOP,
    )
    fmt = ctx.output_format()
    if fmt == "json":
        ctx.facade.export_json(
            _fans_envelope(result),
            command="fans",
            target=username,
            dest=_resolve_dest(ctx, fmt="json"),
        )
        return result
    if fmt == "csv":
        ctx.facade.export_csv(
            _fans_csv_rows(result),
            command="fans",
            target=username,
            dest=_resolve_dest(ctx, fmt="csv"),
        )
        return result
    if fmt == "maltego":
        ctx.facade.export_maltego(
            _fans_maltego_rows(result),
            command="fans",
            entity_type="user",
            target=username,
        )
        return result

    ctx.print(
        f"Top fans of @{username} "
        f"(last {result.analyzed_posts} of {window} posts, "
        f"score = likes + {result.comment_weight}*comments):"
    )
    if result.empty:
        ctx.print(f"no posts to analyze for @{username}")
        return result
    if not result.items:
        ctx.print("no engagement found in the analysed window")
        return result
    rows = [(f"@{r.username}", f"{r.score:>4}  ({r.likes}L + {r.comments}C)") for r in result.items]
    ctx.print(render_kv(rows, key_label="user", value_label="score"))
    return result


__all__ = [
    "INTERACTIONS_DEFAULT_WINDOW",
    "comments_cmd",
    "fans_cmd",
    "wcommented_cmd",
    "wliked_cmd",
    "wtagged_cmd",
]
