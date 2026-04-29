"""Profile-group commands: `/info`, `/propic`, `/email`, `/phone`, `/export`.

Every command in this module resolves the active target via `with_target`,
fetches the `Profile` (and `user_about` payload where applicable) through
the facade, then either renders a `rich`-style view, downloads media, or
exports JSON depending on the global flags.

`access` is honoured uniformly: a `private` / `deleted` profile is still
rendered or exported (the facts about an empty / private profile are still
useful intel) but contact / propic commands report their absence with a
plain note instead of failing — see `_access_note`.
"""

from __future__ import annotations

import argparse
import dataclasses
from pathlib import Path
from typing import Any

from insto.commands._base import (
    CommandContext,
    CommandUsageError,
    add_target_arg,
    command,
    download_or_print_url,
    resolve_export_dest,
    with_target,
)
from insto.models import Post, Profile
from insto.service.facade import _safe_pk
from insto.ui.render import render_profile


def _access_note(profile: Profile) -> str | None:
    """Return a one-line note when a profile cannot offer per-field intel."""
    if profile.access == "deleted":
        return f"@{profile.username} is deleted — no data available"
    if profile.access == "private":
        return f"@{profile.username} is private — only public-profile fields visible"
    return None


def _profile_payload(profile: Profile, about: dict[str, Any]) -> dict[str, Any]:
    """Combine `profile` and `about` into a serialisable export envelope."""
    return {"profile": dataclasses.asdict(profile), "about": about}


# ---------------------------------------------------------------------------
# /info
# ---------------------------------------------------------------------------


@command(
    "info",
    "Show full profile (with user_about) for the active target",
    add_args=add_target_arg,
)
@with_target
async def info_cmd(ctx: CommandContext, username: str) -> tuple[Profile, dict[str, Any]]:
    profile, about = await ctx.facade.profile_info(username)
    if ctx.output_format() == "json":
        dest_arg = ctx.args.json if ctx.args.json is not None else ""
        dest = resolve_export_dest(dest_arg)
        ctx.facade.export_json(
            _profile_payload(profile, about),
            command="info",
            target=username,
            dest=dest,
        )
    else:
        ctx.print(render_profile(profile, about))
    return profile, about


# ---------------------------------------------------------------------------
# /about
# ---------------------------------------------------------------------------


@command(
    "about",
    "Show the IG `user_about` payload (joined date, country, former usernames)",
    add_args=add_target_arg,
)
@with_target
async def about_cmd(ctx: CommandContext, username: str) -> dict[str, Any]:
    """Surface the raw ``user_about`` dict.

    `/info` already folds the populated about-fields into the profile
    panel, but `/about` is for analysts who want the dict on its own —
    cheaper than `/info` (one fewer call) and JSON-exportable for
    pipelines that don't care about the rest of the profile.
    """
    pk = await ctx.facade.resolve_pk(username)
    payload = await ctx.facade.backend.get_user_about(pk)
    if ctx.output_format() == "json":
        dest_arg = ctx.args.json if ctx.args.json is not None else ""
        dest = resolve_export_dest(dest_arg)
        ctx.facade.export_json(payload, command="about", target=username, dest=dest)
        return payload
    if not payload:
        ctx.print(f"@{username} has no user_about data")
        return payload
    # Render as a compact two-column key/value grid. No Panel — this is
    # meant to be a quick-look companion to /info, not its replacement.
    from rich.table import Table

    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="field", no_wrap=True)
    grid.add_column(style="value", overflow="fold")
    for key, value in payload.items():
        if value in (None, ""):
            continue
        grid.add_row(key.replace("_", " "), str(value))
    ctx.print(f"@{username} — user_about:")
    ctx.print(grid)
    return payload


# ---------------------------------------------------------------------------
# /propic
# ---------------------------------------------------------------------------


@command(
    "propic",
    "Download the HD profile picture of the active target",
    add_args=add_target_arg,
)
@with_target
async def propic_cmd(ctx: CommandContext, username: str) -> Path | None:
    profile = await ctx.facade.profile(username)
    if not profile.avatar_url:
        ctx.print(f"@{username} has no profile picture URL")
        return None
    # Use the already-validated `username` rather than the network-sourced
    # `profile.username` as the path segment. The two are identical for any
    # well-formed Instagram response; passing the validated one keeps the
    # path-traversal guard at the user-input boundary. `profile.pk` is also
    # backend-derived, so sanitize it the same way `facade.download_propic`
    # does.
    dest_dir = ctx.facade._media_dir(username, "propic")
    dest = dest_dir / _safe_pk(profile.pk)
    out = await download_or_print_url(
        ctx.facade,
        profile.avatar_url,
        dest,
        no_download=ctx.no_download,
    )
    if out is not None:
        ctx.print(f"saved {out}")
    return out


# ---------------------------------------------------------------------------
# /email and /phone — single-field contact lookups
# ---------------------------------------------------------------------------


def _emit_contact(
    ctx: CommandContext,
    *,
    field_name: str,
    profile: Profile,
    about: dict[str, Any],
    value: str | None,
) -> dict[str, Any]:
    """Render and optionally export a single contact field.

    The export envelope keeps `username` alongside the value so that the
    JSON file stays useful when the consumer has a list of targets and one
    contact per file.
    """
    payload = {
        "username": profile.username,
        "pk": profile.pk,
        field_name: value,
        "is_eligible_to_show_email": about.get("is_eligible_to_show_email"),
    }
    if ctx.output_format() == "json":
        dest_arg = ctx.args.json if ctx.args.json is not None else ""
        dest = resolve_export_dest(dest_arg)
        ctx.facade.export_json(
            payload,
            command=field_name,
            target=profile.username,
            dest=dest,
        )
        return payload

    note = _access_note(profile)
    if note:
        ctx.print(note)
    if value:
        ctx.print(f"{field_name}: {value}")
    else:
        ctx.print(f"@{profile.username} has no public {field_name}")
    return payload


@command(
    "email",
    "Show the public email (if any) for the active target",
    add_args=add_target_arg,
)
@with_target
async def email_cmd(ctx: CommandContext, username: str) -> dict[str, Any]:
    profile, about = await ctx.facade.profile_info(username)
    return _emit_contact(
        ctx,
        field_name="email",
        profile=profile,
        about=about,
        value=profile.public_email,
    )


@command(
    "phone",
    "Show the public phone (if any) for the active target",
    add_args=add_target_arg,
)
@with_target
async def phone_cmd(ctx: CommandContext, username: str) -> dict[str, Any]:
    profile, about = await ctx.facade.profile_info(username)
    return _emit_contact(
        ctx,
        field_name="phone",
        profile=profile,
        about=about,
        value=profile.public_phone,
    )


# ---------------------------------------------------------------------------
# /export — full profile + about as JSON
# ---------------------------------------------------------------------------


@command(
    "export",
    "Export the full profile + user_about payload as a versioned JSON file",
    add_args=add_target_arg,
)
@with_target
async def export_cmd(ctx: CommandContext, username: str) -> Path | None:
    fmt = ctx.output_format()
    if fmt is not None and fmt != "json":
        raise CommandUsageError("/export only supports JSON output; pass --json or --json -")
    profile, about = await ctx.facade.profile_info(username)
    dest_arg = ctx.args.json if ctx.args.json is not None else ""
    dest = resolve_export_dest(dest_arg)
    out = ctx.facade.export_json(
        _profile_payload(profile, about),
        command="export",
        target=username,
        dest=dest,
    )
    if out is not None:
        ctx.print(f"wrote {out}")
    return out


# ---------------------------------------------------------------------------
# /postinfo — resolve a post URL/code/pk to the full Post DTO
# ---------------------------------------------------------------------------


def _add_postinfo_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "ref",
        help="post reference: full URL (https://www.instagram.com/p/<code>/), "
        "shortcode (DXPduuvEY7S), or numeric pk",
    )


@command(
    "postinfo",
    "Resolve a post URL / shortcode / pk to its full metadata (no target needed)",
    add_args=_add_postinfo_args,
)
async def postinfo_cmd(ctx: CommandContext) -> Post:
    ref = (getattr(ctx.args, "ref", "") or "").strip()
    if not ref:
        raise CommandUsageError("/postinfo needs a URL, shortcode, or pk")
    post = await ctx.facade.post_info(ref)

    fmt = ctx.output_format()
    if fmt == "json":
        dest = resolve_export_dest(ctx.args.json if ctx.args.json is not None else "")
        ctx.facade.export_json(
            dataclasses.asdict(post),
            command="postinfo",
            target=post.code or post.pk,
            dest=dest,
        )
        return post

    # Render as a key/value Panel — same style as /info but for a post.
    from rich.panel import Panel
    from rich.table import Table

    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="field", no_wrap=True)
    grid.add_column(style="value", overflow="fold")
    grid.add_row("pk", post.pk)
    grid.add_row("code", post.code)
    grid.add_row("type", post.media_type)
    grid.add_row("taken at", str(post.taken_at))
    grid.add_row("owner", f"@{post.owner_username}" if post.owner_username else "—")
    grid.add_row("likes", str(post.like_count))
    grid.add_row("comments", str(post.comment_count))
    if post.location_name:
        grid.add_row("location", post.location_name)
    if post.caption:
        grid.add_row("caption", post.caption)
    if post.hashtags:
        grid.add_row("hashtags", ", ".join(f"#{t}" for t in post.hashtags))
    if post.mentions:
        grid.add_row("mentions", ", ".join(f"@{m}" for m in post.mentions))
    if post.media_urls:
        grid.add_row("media", post.media_urls[0])
    title = f"post {post.code or post.pk}"
    ctx.print(Panel(grid, title=title, border_style="panel.border", padding=(1, 2)))
    return post


# ---------------------------------------------------------------------------
# /pinned
# ---------------------------------------------------------------------------


@command(
    "pinned",
    "Pinned posts of the active target (Instagram allows up to 3)",
    add_args=add_target_arg,
)
@with_target
async def pinned_cmd(ctx: CommandContext, username: str) -> list[Post]:
    n = int(ctx.limit) if ctx.limit is not None and ctx.limit > 0 else 12
    posts = await ctx.facade.user_pinned(username, limit=n)

    fmt = ctx.output_format()
    if fmt == "json":
        dest = resolve_export_dest(ctx.args.json if ctx.args.json is not None else "")
        ctx.facade.export_json(
            [dataclasses.asdict(p) for p in posts],
            command="pinned",
            target=username,
            dest=dest,
        )
        return posts

    from insto.ui.render import render_media_grid

    if not posts:
        ctx.print(f"@{username} has no pinned posts")
        return posts
    ctx.print(render_media_grid(posts, title=f"pinned posts of @{username} ({len(posts)})"))
    return posts


__all__ = [
    "about_cmd",
    "email_cmd",
    "export_cmd",
    "info_cmd",
    "phone_cmd",
    "pinned_cmd",
    "postinfo_cmd",
    "propic_cmd",
]
