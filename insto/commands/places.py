"""`/place` and `/placeposts` — geo-OSINT.

Two complementary commands wrapping Instagram's location surface:

- ``/place <query>`` — free-text search for places. Lists matched
  locations with name, lat/lng, IG location pk, and Facebook places
  id. The pk is what you copy into ``/placeposts``.
- ``/placeposts <pk>`` — top posts geotagged at this location.
  Reveals who, what, when at a place — the killer geo-OSINT primitive.
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
    command,
    resolve_export_dest,
)
from insto.models import Place, Post
from insto.ui.render import render_media_grid


def _resolve_dest(ctx: CommandContext, *, fmt: str) -> Path | IO[bytes] | None:
    arg = ctx.args.json if fmt == "json" else ctx.args.csv
    return resolve_export_dest(arg if arg is not None else "")


# ---------------------------------------------------------------------------
# /place
# ---------------------------------------------------------------------------


def _add_place_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("query", help="text to search for (e.g. 'Eiffel Tower')")
    parser.add_argument(
        "count",
        nargs="?",
        type=int,
        default=20,
        help="number of places to fetch (default 20)",
    )


def _place_rows(places: Sequence[Place]) -> list[dict[str, Any]]:
    return [
        {
            "rank": i,
            "pk": p.pk,
            "name": p.name,
            "city": p.city,
            "address": p.address,
            "lat": p.lat,
            "lng": p.lng,
            "facebook_id": p.facebook_id,
        }
        for i, p in enumerate(places, 1)
    ]


def _place_maltego_rows(places: Sequence[Place]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for i, p in enumerate(places, 1):
        gps = (
            f"{p.lat:.4f},{p.lng:.4f}"
            if p.lat is not None and p.lng is not None
            else ""
        )
        rows.append(
            {
                "value": p.name,
                "weight": 1,
                "notes": gps,
                "rank": i,
                "pk": p.pk,
                "city": p.city,
                "lat": p.lat,
                "lng": p.lng,
            }
        )
    return rows


@command(
    "place",
    "Search Instagram places by text — name, GPS, location pk for /placeposts",
    csv=True,
    add_args=_add_place_args,
)
async def place_cmd(ctx: CommandContext) -> list[Place]:
    query = (getattr(ctx.args, "query", "") or "").strip()
    if not query:
        raise CommandUsageError("/place needs a non-empty query")
    n = int(ctx.limit) if ctx.limit is not None and ctx.limit > 0 else int(
        getattr(ctx.args, "count", 20)
    )
    places = await ctx.facade.search_places(query, limit=n)

    fmt = ctx.output_format()
    if fmt == "json":
        ctx.facade.export_json(
            [dataclasses.asdict(p) for p in places],
            command="place",
            target=query,
            dest=_resolve_dest(ctx, fmt="json"),
        )
        return places
    if fmt == "csv":
        ctx.facade.export_csv(
            _place_rows(places),
            command="place",
            target=query,
            dest=_resolve_dest(ctx, fmt="csv"),
        )
        return places
    if fmt == "maltego":
        ctx.facade.export_maltego(
            _place_maltego_rows(places),
            command="place",
            entity_type="location",
            target=query,
        )
        return places

    if not places:
        ctx.print(f"no places matched {query!r}")
        return places
    # Human-readable table: pk, name, lat/lng, city.
    from rich.table import Table

    grid = Table(title=f"places matching {query!r} ({len(places)})", show_lines=False)
    grid.add_column("#", style="muted", justify="right")
    grid.add_column("pk", style="muted", no_wrap=True)
    grid.add_column("name", style="value")
    grid.add_column("city", style="muted")
    grid.add_column("lat,lng", style="muted", no_wrap=True)
    for i, p in enumerate(places, 1):
        gps = f"{p.lat:.4f}, {p.lng:.4f}" if p.lat is not None and p.lng is not None else "—"
        grid.add_row(str(i), p.pk, p.name, p.city or "—", gps)
    ctx.print(grid)
    ctx.print("→ /placeposts <pk> to see top posts at a location")
    return places


# ---------------------------------------------------------------------------
# /placeposts
# ---------------------------------------------------------------------------


def _add_placeposts_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("place_pk", help="IG location pk (from /place)")
    parser.add_argument(
        "count",
        nargs="?",
        type=int,
        default=30,
        help="number of posts to fetch (default 30)",
    )


@command(
    "placeposts",
    "List top posts at a given Instagram location pk (geo-OSINT)",
    add_args=_add_placeposts_args,
)
async def placeposts_cmd(ctx: CommandContext) -> list[Post]:
    pk = (getattr(ctx.args, "place_pk", "") or "").strip()
    if not pk:
        raise CommandUsageError("/placeposts needs a location pk")
    n = int(ctx.limit) if ctx.limit is not None and ctx.limit > 0 else int(
        getattr(ctx.args, "count", 30)
    )
    posts = await ctx.facade.place_posts(pk, limit=n)

    fmt = ctx.output_format()
    if fmt == "json":
        ctx.facade.export_json(
            [dataclasses.asdict(p) for p in posts],
            command="placeposts",
            target=pk,
            dest=_resolve_dest(ctx, fmt="json"),
        )
        return posts

    if not posts:
        ctx.print(f"no posts at place {pk}")
        return posts
    ctx.print(render_media_grid(posts, title=f"top posts at place {pk} ({len(posts)})"))
    return posts
