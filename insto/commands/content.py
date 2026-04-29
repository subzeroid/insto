"""Content-analysis commands: `/locations`, `/hashtags`, `/mentions`, `/captions`, `/likes`.

Every command in this module operates on a *bounded window* of recent posts.
The window size defaults to 50 and is overridden by the global `--limit N`
flag. The default exists to keep a casual `/hashtags` invocation cheap on
quota — at 50 posts per analysis the cost is bounded and predictable.

The rendered output of every command starts with an explicit window header,
e.g. `Hashtags from @alice (last 50 posts):`. The header is part of the
contract: it tells the operator *what they are looking at* before they read
the table, so a result with three hashtags from a 50-post window is not
mistaken for "this user only has three hashtags total".

Both the TopList commands (`hashtags`, `mentions`, `locations`) and the
flat-row commands (`captions`, `likes`) are listed in
`insto.service.exporter.CSV_FLAT_COMMANDS` and may be exported as CSV.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import IO, Any

from insto.commands._base import (
    CommandContext,
    add_target_arg,
    command,
    resolve_export_dest,
    with_target,
)
from insto.models import Post
from insto.service.analytics import LikesStats, TopList, aggregate_likes
from insto.ui.render import render_kv

# ---------------------------------------------------------------------------
# Defaults / helpers
# ---------------------------------------------------------------------------

CONTENT_DEFAULT_WINDOW = 50


def _resolve_window(ctx: CommandContext) -> int:
    """Pick the effective window size: `--limit N` (positive) wins, else 50."""
    raw = ctx.limit
    if raw is None or raw <= 0:
        return CONTENT_DEFAULT_WINDOW
    return int(raw)


def _window_header(kind: str, username: str, window: int) -> str:
    """`Hashtags from @user (last 50 posts):` — printed above every result."""
    return f"{kind} from @{username} (last {window} posts):"


def _resolve_dest(ctx: CommandContext, *, fmt: str) -> Path | IO[bytes] | None:
    arg = ctx.args.json if fmt == "json" else ctx.args.csv
    return resolve_export_dest(arg if arg is not None else "")


def _toplist_rows(result: TopList) -> list[dict[str, Any]]:
    """Flatten a `TopList` into CSV-friendly rows (rank starts at 1)."""
    key_field = {
        "hashtags": "hashtag",
        "mentions": "mention",
        "locations": "location",
    }.get(result.kind, "key")
    return [
        {"rank": i, key_field: key, "count": count}
        for i, (key, count) in enumerate(result.items, 1)
    ]


def _toplist_envelope(result: TopList) -> dict[str, Any]:
    """JSON-friendly envelope for `TopList` (preserves window header context)."""
    return {
        "target": result.target,
        "kind": result.kind,
        "window": result.window,
        "analyzed": result.analyzed,
        "items": [{"key": k, "count": c} for k, c in result.items],
        "empty": result.empty,
    }


_TOPLIST_MALTEGO_KIND: dict[str, str] = {
    "hashtags": "hashtag",
    "mentions": "mention",
    "locations": "location",
    "wcommented": "user",
    "wtagged": "user",
}


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
    kind_title: str,
) -> TopList:
    """Render or export a `TopList`. Always prints the window header first."""
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
            _toplist_rows(result),
            command=command_name,
            target=result.target,
            dest=_resolve_dest(ctx, fmt="csv"),
        )
        return result
    if fmt == "maltego":
        ctx.facade.export_maltego(
            _toplist_maltego_rows(result),
            command=command_name,
            entity_type=_TOPLIST_MALTEGO_KIND[command_name],
            target=result.target,
        )
        return result

    ctx.print(_window_header(kind_title, result.target, result.window))
    if result.empty:
        ctx.print(f"no posts to analyze for @{result.target}")
        return result
    if not result.items:
        ctx.print(f"no {result.kind} found in the analysed window")
        return result
    ctx.print(
        render_kv(
            result.items,
            key_label=result.kind.rstrip("s"),
            value_label="count",
        )
    )
    return result


# ---------------------------------------------------------------------------
# /hashtags, /mentions, /locations
# ---------------------------------------------------------------------------


@command(
    "hashtags",
    "Top hashtags in captions of the active target's recent posts",
    csv=True,
    add_args=add_target_arg,
)
@with_target
async def hashtags_cmd(ctx: CommandContext, username: str) -> TopList:
    window = _resolve_window(ctx)
    result = await ctx.facade.hashtags(username, limit=window)
    return await _emit_toplist(ctx, result=result, command_name="hashtags", kind_title="Hashtags")


@command(
    "mentions",
    "Top @-mentions in captions of the active target's recent posts",
    csv=True,
    add_args=add_target_arg,
)
@with_target
async def mentions_cmd(ctx: CommandContext, username: str) -> TopList:
    window = _resolve_window(ctx)
    result = await ctx.facade.mentions(username, limit=window)
    return await _emit_toplist(ctx, result=result, command_name="mentions", kind_title="Mentions")


@command(
    "locations",
    "Top geo-tagged locations of the active target's recent posts",
    csv=True,
    add_args=add_target_arg,
)
@with_target
async def locations_cmd(ctx: CommandContext, username: str) -> TopList:
    window = _resolve_window(ctx)
    result = await ctx.facade.locations(username, limit=window)
    return await _emit_toplist(ctx, result=result, command_name="locations", kind_title="Locations")


# ---------------------------------------------------------------------------
# /captions
# ---------------------------------------------------------------------------


def _caption_rows(posts: Sequence[Post]) -> list[dict[str, Any]]:
    """Flatten posts to a flat-CSV row per post, caption first-class."""
    return [
        {
            "rank": i,
            "pk": p.pk,
            "code": p.code,
            "taken_at": p.taken_at,
            "like_count": p.like_count,
            "comment_count": p.comment_count,
            "caption": p.caption.replace("\n", " ").strip(),
        }
        for i, p in enumerate(posts, 1)
    ]


@command(
    "captions",
    "Dump captions of the active target's recent posts",
    csv=True,
    add_args=add_target_arg,
)
@with_target
async def captions_cmd(ctx: CommandContext, username: str) -> list[Post]:
    window = _resolve_window(ctx)
    posts = await ctx.facade.user_posts(username, limit=window)

    fmt = ctx.output_format()
    if fmt == "json":
        ctx.facade.export_json(
            {
                "target": username,
                "window": window,
                "analyzed": len(posts),
                "items": _caption_rows(posts),
            },
            command="captions",
            target=username,
            dest=_resolve_dest(ctx, fmt="json"),
        )
        return posts
    if fmt == "csv":
        ctx.facade.export_csv(
            _caption_rows(posts),
            command="captions",
            target=username,
            dest=_resolve_dest(ctx, fmt="csv"),
        )
        return posts

    ctx.print(_window_header("Captions", username, window))
    if not posts:
        ctx.print(f"no posts to analyze for @{username}")
        return posts
    rows: list[tuple[str, str]] = []
    for p in posts:
        caption = p.caption.replace("\n", " ").strip() or "(no caption)"
        rows.append((p.code, caption))
    ctx.print(render_kv(rows, key_label="code", value_label="caption"))
    return posts


# ---------------------------------------------------------------------------
# /likes — aggregate likes over a bounded window of posts
# ---------------------------------------------------------------------------


def _likes_rows(stats: LikesStats) -> list[dict[str, Any]]:
    """Flatten `LikesStats.top_posts` to flat-CSV rows.

    Each row carries the windowed totals so the CSV is self-describing even
    if a downstream tool only loads the first row (Excel, csvkit).
    """
    return [
        {
            "rank": i,
            "code": code,
            "like_count": likes,
            "window": stats.window,
            "analyzed": stats.analyzed,
            "total_likes": stats.total_likes,
            "avg_likes": round(stats.avg_likes, 2),
        }
        for i, (code, likes) in enumerate(stats.top_posts, 1)
    ]


@command(
    "likes",
    "Aggregate like-count stats over the active target's recent posts",
    csv=True,
    add_args=add_target_arg,
)
@with_target
async def likes_cmd(ctx: CommandContext, username: str) -> LikesStats:
    window = _resolve_window(ctx)
    posts = await ctx.facade.user_posts(username, limit=window)
    stats = aggregate_likes(posts, target=username, limit=window)

    fmt = ctx.output_format()
    if fmt == "json":
        ctx.facade.export_json(
            {
                "target": stats.target,
                "window": stats.window,
                "analyzed": stats.analyzed,
                "total_likes": stats.total_likes,
                "avg_likes": stats.avg_likes,
                "top_posts": [{"code": c, "like_count": n} for c, n in stats.top_posts],
                "empty": stats.empty,
            },
            command="likes",
            target=username,
            dest=_resolve_dest(ctx, fmt="json"),
        )
        return stats
    if fmt == "csv":
        ctx.facade.export_csv(
            _likes_rows(stats),
            command="likes",
            target=username,
            dest=_resolve_dest(ctx, fmt="csv"),
        )
        return stats

    ctx.print(_window_header("Likes", username, window))
    if stats.empty:
        ctx.print(f"no posts to analyze for @{username}")
        return stats
    ctx.print(
        f"total: {stats.total_likes:,} likes · "
        f"avg: {stats.avg_likes:,.1f} per post · "
        f"analyzed {stats.analyzed} posts"
    )
    if stats.top_posts:
        ctx.print(
            render_kv(
                stats.top_posts,
                key_label="code",
                value_label="likes",
            )
        )
    return stats


# ---------------------------------------------------------------------------
# /timeline — posting cadence histogram
# ---------------------------------------------------------------------------


# Unicode block ladder, eight steps from "almost empty" to "full".
_BAR_LADDER: str = " ▁▂▃▄▅▆▇█"
_DAY_LABELS: tuple[str, ...] = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _spark(values: list[int]) -> str:
    """Map a list of counts to a string of block characters of equal length.

    Highest bucket gets `█`, empty buckets get a space. Linear scaling
    against the max so the visual emphasises *relative* posting
    intensity, not absolute counts (a 5-post target and a 500-post
    target should both produce a meaningful shape)."""
    if not values:
        return ""
    peak = max(values)
    if peak == 0:
        return " " * len(values)
    out: list[str] = []
    for v in values:
        if v == 0:
            out.append(" ")
            continue
        idx = max(1, min(len(_BAR_LADDER) - 1, round(v / peak * (len(_BAR_LADDER) - 1))))
        out.append(_BAR_LADDER[idx])
    return "".join(out)


@command(
    "timeline",
    "Posting cadence histogram (hour-of-day + day-of-week) over recent posts",
    csv=False,
    add_args=add_target_arg,
)
@with_target
async def timeline_cmd(ctx: CommandContext, username: str):  # type: ignore[no-untyped-def]
    from datetime import UTC, datetime

    window = _resolve_window(ctx)
    result = await ctx.facade.timeline(username, limit=window)

    fmt = ctx.output_format()
    if fmt == "json":
        ctx.facade.export_json(
            {
                "target": result.target,
                "window": result.window,
                "analyzed": result.analyzed,
                "hour_of_day": result.hour_of_day,
                "day_of_week": result.day_of_week,
                "first_post_ts": result.first_post_ts,
                "last_post_ts": result.last_post_ts,
                "empty": result.empty,
            },
            command="timeline",
            target=username,
            dest=_resolve_dest(ctx, fmt="json"),
        )
        return result

    if result.empty:
        ctx.print(f"no timestamped posts to analyze for @{username}")
        return result

    span = ""
    if result.first_post_ts and result.last_post_ts:
        first = datetime.fromtimestamp(result.first_post_ts, tz=UTC).strftime("%Y-%m-%d")
        last = datetime.fromtimestamp(result.last_post_ts, tz=UTC).strftime("%Y-%m-%d")
        span = f" ({first} → {last})"
    ctx.print(f"@{username} posting cadence — {result.analyzed} posts{span}")
    ctx.print("")
    # Hour-of-day: 24-char sparkline (one block per UTC hour). Range is
    # implicit in the label; trying to print individual hour ticks under
    # 24 single-width columns produces an unreadable slop.
    spark = _spark(result.hour_of_day)
    peak_hour = result.hour_of_day.index(max(result.hour_of_day)) if any(result.hour_of_day) else 0
    ctx.print(f"  hour 00 → 23 (UTC, peak {peak_hour:02d}h): {spark}")
    ctx.print("")
    # Day-of-week: 7-row "Mon  N  █████" listing — easier to read than a
    # 7-character sparkline.
    peak = max(result.day_of_week) or 1
    bar_width = 30
    for label, count in zip(_DAY_LABELS, result.day_of_week, strict=True):
        bar = "█" * round(count / peak * bar_width) if count else ""
        ctx.print(f"  {label} {count:>3}  {bar}")
    return result


# ---------------------------------------------------------------------------
# /where — geo-fingerprint (anchor + centroid + top places)
# ---------------------------------------------------------------------------


@command(
    "where",
    "Geo-fingerprint: anchor place + centroid + top geotagged places (OSINT)",
    csv=False,
    add_args=add_target_arg,
)
@with_target
async def where_cmd(ctx: CommandContext, username: str):  # type: ignore[no-untyped-def]
    window = _resolve_window(ctx)
    result = await ctx.facade.where(username, limit=window, top=10)

    fmt = ctx.output_format()
    if fmt == "json":
        # GeoFingerprintResult includes nested GeoPlace dataclasses;
        # dataclasses.asdict handles them recursively.
        import dataclasses

        ctx.facade.export_json(
            dataclasses.asdict(result),
            command="where",
            target=username,
            dest=_resolve_dest(ctx, fmt="json"),
        )
        return result

    if result.empty:
        ctx.print(f"no posts to analyze for @{username}")
        return result
    if result.geotagged == 0:
        ctx.print(f"@{username}: 0 of {result.analyzed} posts had a geotag")
        return result

    ratio = f"{result.geotagged} of {result.analyzed} posts geotagged"
    ctx.print(f"@{username} geo fingerprint — {ratio}")
    ctx.print("")

    # Anchor + centroid + radius — the headline.
    if result.anchor is not None:
        a = result.anchor
        pct = 100 * a.count / result.geotagged
        ctx.print(
            f"  anchor:    {a.name or '(unnamed)'}  "
            f"({a.lat:.4f}°N, {a.lng:.4f}°E) — {a.count} posts ({pct:.0f}%)"
        )
    if result.centroid_lat is not None and result.centroid_lng is not None:
        radius = result.radius_km or 0.0
        ctx.print(
            f"  centroid:  {result.centroid_lat:.3f}°N, {result.centroid_lng:.3f}°E "
            f"— max radius {radius:.0f} km"
        )
    ctx.print("")

    # Top places bar chart (place name + horizontal bar + count).
    peak = max((p.count for p in result.places), default=1)
    bar_width = 30
    for p in result.places:
        bar = "█" * round(p.count / peak * bar_width) if p.count else ""
        label = (p.name or f"pk={p.pk}")[:30]
        ctx.print(f"  {label:<30}  {p.count:>3}  {bar}")
    return result


__all__ = [
    "CONTENT_DEFAULT_WINDOW",
    "captions_cmd",
    "hashtags_cmd",
    "likes_cmd",
    "locations_cmd",
    "mentions_cmd",
    "timeline_cmd",
    "where_cmd",
]
