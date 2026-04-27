"""`/dossier` — collect everything we know about one target in one shot.

Composition lives here, not on the facade. The command stitches together
existing facade methods (`profile_info`, `user_posts`, `followers`,
`followings`, `mutuals`, `hashtags`, `mentions`, `locations`, `wcommented`,
`wtagged`) so the facade does not grow a god-method. Sections that do not
depend on each other run concurrently via `asyncio.gather(...,
return_exceptions=True)`; one failed section does not cancel the rest, it
shows up as a `failed` row in `MANIFEST.md` and flips `partial: true`.

Hard preconditions, checked before any directory is created:

  1. **profile must be public** — `profile_info(...)` is the very first
     network call. If `access != "public"` we raise `CommandUsageError`
     and never touch disk. Private / blocked / deleted profiles cannot
     be dossier'd.
  2. **disk must have ≥ 2GB free** — `shutil.disk_usage` against the
     closest existing ancestor of `output_dir`. Skipping this check
     would leave a half-written dossier on a full disk.

Output layout:

    output/<user>/dossier/<utc-stamp>/
        profile.json
        posts.json
        posts/                # only when --no-download is NOT set
        followers.csv
        following.csv
        mutuals.csv
        hashtags.csv
        mentions.csv
        locations.csv
        wcommented.csv
        wtagged.csv
        MANIFEST.md
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import shutil
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from insto.commands._base import (
    CommandContext,
    CommandUsageError,
    command,
    with_target,
)
from insto.models import User
from insto.service import analytics
from insto.service.exporter import SCHEMA_VERSION
from insto.service.facade import OsintFacade

# 2GB free required at output_dir before /dossier may start.
DOSSIER_MIN_FREE_BYTES = 2 * 1024 * 1024 * 1024

DEFAULT_POSTS_LIMIT = 50
DEFAULT_NETWORK_LIMIT = 1000
DEFAULT_ANALYTICS_LIMIT = 50
DEFAULT_TAGGED_LIMIT = 50

# Order matches the gather() coroutine list below; used to label
# bare exceptions in the MANIFEST partial-failure rows.
SECTION_NAMES: tuple[str, ...] = (
    "posts",
    "followers",
    "following",
    "mutuals",
    "hashtags",
    "mentions",
    "locations",
    "wcommented",
    "wtagged",
)


@dataclass
class SectionResult:
    """One row in `MANIFEST.md`. Either `file` is set (success) or `error` is."""

    name: str
    file: Path | None = None
    count: int = 0
    truncated: bool = False
    error: str | None = None


def _utc_dirname() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _utc_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _existing_ancestor(p: Path) -> Path:
    """Walk up `p` until an existing directory is found (used for disk_usage)."""
    cur = p
    while not cur.exists():
        parent = cur.parent
        if parent == cur:
            return cur
        cur = parent
    return cur


def _check_disk(output_dir: Path) -> int:
    """Raise `CommandUsageError` if free space at `output_dir` is < 2GB."""
    target = _existing_ancestor(output_dir)
    usage = shutil.disk_usage(str(target))
    if usage.free < DOSSIER_MIN_FREE_BYTES:
        free_gb = usage.free / (1024**3)
        raise CommandUsageError(
            f"insufficient disk space at {target}: {free_gb:.2f}GB free, "
            "need at least 2GB for /dossier"
        )
    return usage.free


def _user_rows(users: Sequence[User]) -> list[dict[str, Any]]:
    """Flatten `User` DTOs to the standard CSV shape used by network commands."""
    return [
        {
            "rank": i,
            "pk": u.pk,
            "username": u.username,
            "full_name": u.full_name,
            "is_private": u.is_private,
            "is_verified": u.is_verified,
        }
        for i, u in enumerate(users, 1)
    ]


def _toplist_rows(top: analytics.TopList) -> list[dict[str, Any]]:
    return [{"rank": i, "key": key, "count": count} for i, (key, count) in enumerate(top.items, 1)]


# ---------------------------------------------------------------------------
# Section coroutines — each writes its file(s) and returns a SectionResult.
# Failures propagate to gather() and are captured into SectionResult(error=).
# ---------------------------------------------------------------------------


async def _do_posts(
    facade: OsintFacade,
    username: str,
    limit: int,
    dossier_dir: Path,
    *,
    no_download: bool,
) -> SectionResult:
    posts = await facade.user_posts(username, limit=limit)
    path = dossier_dir / "posts.json"
    facade.export_json(
        [dataclasses.asdict(p) for p in posts],
        command="dossier.posts",
        target=username,
        dest=path,
    )
    if not no_download and posts:
        media_dir = dossier_dir / "posts"
        media_dir.mkdir(parents=True, exist_ok=True)
        for post in posts:
            for idx, url in enumerate(post.media_urls):
                base = media_dir / (post.pk if idx == 0 else f"{post.pk}_{idx}")
                try:
                    await facade._stream(url, base, taken_at=post.taken_at)
                except Exception:
                    # A single failed media URL must not fail the whole section
                    # (the streamer already enforces atomic writes / no partial
                    # files on disk). The section reports its post count from
                    # the JSON manifest, not from media file count.
                    continue
    return SectionResult(name="posts", file=path, count=len(posts), truncated=len(posts) >= limit)


async def _do_followers(
    facade: OsintFacade, username: str, limit: int, dossier_dir: Path
) -> SectionResult:
    users = await facade.followers(username, limit=limit)
    path = dossier_dir / "followers.csv"
    facade.export_csv(_user_rows(users), command="followers", target=username, dest=path)
    return SectionResult(
        name="followers", file=path, count=len(users), truncated=len(users) >= limit
    )


async def _do_following(
    facade: OsintFacade, username: str, limit: int, dossier_dir: Path
) -> SectionResult:
    users = await facade.followings(username, limit=limit)
    path = dossier_dir / "following.csv"
    facade.export_csv(_user_rows(users), command="followings", target=username, dest=path)
    return SectionResult(
        name="following", file=path, count=len(users), truncated=len(users) >= limit
    )


async def _do_mutuals(
    facade: OsintFacade, username: str, limit: int, dossier_dir: Path
) -> SectionResult:
    result = await facade.mutuals(username, follower_limit=limit, following_limit=limit)
    path = dossier_dir / "mutuals.csv"
    facade.export_csv(_user_rows(result.items), command="mutuals", target=username, dest=path)
    return SectionResult(name="mutuals", file=path, count=len(result.items))


async def _do_hashtags(
    facade: OsintFacade, username: str, limit: int, dossier_dir: Path
) -> SectionResult:
    top = await facade.hashtags(username, limit=limit)
    path = dossier_dir / "hashtags.csv"
    facade.export_csv(_toplist_rows(top), command="hashtags", target=username, dest=path)
    return SectionResult(name="hashtags", file=path, count=len(top.items))


async def _do_mentions(
    facade: OsintFacade, username: str, limit: int, dossier_dir: Path
) -> SectionResult:
    top = await facade.mentions(username, limit=limit)
    path = dossier_dir / "mentions.csv"
    facade.export_csv(_toplist_rows(top), command="mentions", target=username, dest=path)
    return SectionResult(name="mentions", file=path, count=len(top.items))


async def _do_locations(
    facade: OsintFacade, username: str, limit: int, dossier_dir: Path
) -> SectionResult:
    top = await facade.locations(username, limit=limit)
    path = dossier_dir / "locations.csv"
    facade.export_csv(_toplist_rows(top), command="locations", target=username, dest=path)
    return SectionResult(name="locations", file=path, count=len(top.items))


async def _do_wcommented(
    facade: OsintFacade, username: str, limit: int, dossier_dir: Path
) -> SectionResult:
    top = await facade.wcommented(username, limit=limit)
    path = dossier_dir / "wcommented.csv"
    facade.export_csv(_toplist_rows(top), command="wcommented", target=username, dest=path)
    return SectionResult(name="wcommented", file=path, count=len(top.items))


async def _do_wtagged(
    facade: OsintFacade, username: str, limit: int, dossier_dir: Path
) -> SectionResult:
    top = await facade.wtagged(username, limit=limit)
    path = dossier_dir / "wtagged.csv"
    facade.export_csv(_toplist_rows(top), command="wtagged", target=username, dest=path)
    return SectionResult(name="wtagged", file=path, count=len(top.items))


# ---------------------------------------------------------------------------
# MANIFEST.md
# ---------------------------------------------------------------------------


def _write_manifest(
    dossier_dir: Path,
    *,
    username: str,
    sections: list[SectionResult],
    duration_s: float,
) -> Path:
    """Render the human-readable manifest. `partial=true` if any section errored."""
    partial = any(s.error is not None for s in sections)
    lines: list[str] = [
        f"# insto dossier — @{username}",
        "",
        f"- captured_at: {_utc_iso()}",
        f"- schema: {SCHEMA_VERSION}",
        f"- partial: {'true' if partial else 'false'}",
        f"- duration_seconds: {duration_s:.2f}",
        "",
        "## Sections",
        "",
    ]

    total_files = 0
    total_bytes = 0
    for section in sections:
        if section.error:
            lines.append(f"- **{section.name}** — failed: {section.error}")
            continue
        details: list[str] = []
        if section.file is not None:
            details.append(section.file.name)
            total_files += 1
            with contextlib.suppress(OSError):
                total_bytes += section.file.stat().st_size
        details.append(f"count={section.count}")
        if section.truncated:
            details.append("truncated=true")
        lines.append(f"- **{section.name}** — {', '.join(details)}")

    media_dir = dossier_dir / "posts"
    media_files = 0
    media_bytes = 0
    if media_dir.exists():
        for f in media_dir.iterdir():
            if f.is_file():
                media_files += 1
                with contextlib.suppress(OSError):
                    media_bytes += f.stat().st_size
        if media_files:
            lines.append(f"- **posts/** — {media_files} media file(s), {media_bytes} bytes")
        total_files += media_files
        total_bytes += media_bytes

    lines += [
        "",
        "## Stats",
        "",
        f"- total_files: {total_files}",
        f"- total_bytes: {total_bytes}",
        "",
    ]
    manifest = dossier_dir / "MANIFEST.md"
    manifest.write_text("\n".join(lines), encoding="utf-8")
    return manifest


# ---------------------------------------------------------------------------
# Command entry point
# ---------------------------------------------------------------------------


@command(
    "dossier",
    "Collect a full target package (profile, posts, network, analytics) "
    "under output/<user>/dossier/<ts>/",
)
@with_target
async def dossier_cmd(ctx: CommandContext, username: str) -> Path:
    started = time.monotonic()
    no_download = ctx.no_download
    user_limit = ctx.limit

    # 1. Pre-flight: profile must be public. NOTHING else fires on a
    #    non-public profile, and no directory is created.
    profile, about = await ctx.facade.profile_info(username)
    if profile.access != "public":
        raise CommandUsageError(f"cannot dossier @{username}: profile is {profile.access}")

    # 2. Disk pre-check: also before any directory is created.
    output_dir = ctx.facade.config.output_dir
    _check_disk(output_dir)

    # 3. Now safe to materialise the dossier directory.
    dossier_dir = output_dir / username / "dossier" / _utc_dirname()
    dossier_dir.mkdir(parents=True, exist_ok=True)

    posts_n = int(user_limit) if user_limit else DEFAULT_POSTS_LIMIT
    network_n = int(user_limit) if user_limit else DEFAULT_NETWORK_LIMIT
    analytics_n = int(user_limit) if user_limit else DEFAULT_ANALYTICS_LIMIT
    tagged_n = int(user_limit) if user_limit else DEFAULT_TAGGED_LIMIT

    profile_path = dossier_dir / "profile.json"
    ctx.facade.export_json(
        {"profile": dataclasses.asdict(profile), "about": about},
        command="dossier.profile",
        target=username,
        dest=profile_path,
    )
    sections: list[SectionResult] = [SectionResult(name="profile", file=profile_path, count=1)]

    coros = [
        _do_posts(ctx.facade, username, posts_n, dossier_dir, no_download=no_download),
        _do_followers(ctx.facade, username, network_n, dossier_dir),
        _do_following(ctx.facade, username, network_n, dossier_dir),
        _do_mutuals(ctx.facade, username, network_n, dossier_dir),
        _do_hashtags(ctx.facade, username, analytics_n, dossier_dir),
        _do_mentions(ctx.facade, username, analytics_n, dossier_dir),
        _do_locations(ctx.facade, username, analytics_n, dossier_dir),
        _do_wcommented(ctx.facade, username, analytics_n, dossier_dir),
        _do_wtagged(ctx.facade, username, tagged_n, dossier_dir),
    ]
    results = await asyncio.gather(*coros, return_exceptions=True)
    for name, r in zip(SECTION_NAMES, results, strict=True):
        if isinstance(r, BaseException):
            sections.append(SectionResult(name=name, error=f"{type(r).__name__}: {r}"))
        else:
            sections.append(r)

    duration = time.monotonic() - started
    _write_manifest(dossier_dir, username=username, sections=sections, duration_s=duration)
    ctx.print(f"wrote dossier to {dossier_dir}")
    return dossier_dir


__all__ = [
    "DEFAULT_ANALYTICS_LIMIT",
    "DEFAULT_NETWORK_LIMIT",
    "DEFAULT_POSTS_LIMIT",
    "DEFAULT_TAGGED_LIMIT",
    "DOSSIER_MIN_FREE_BYTES",
    "SECTION_NAMES",
    "SectionResult",
    "dossier_cmd",
]
