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

import dataclasses
from pathlib import Path
from typing import Any

from insto.commands._base import (
    CommandContext,
    CommandUsageError,
    command,
    download_or_print_url,
    resolve_export_dest,
    with_target,
)
from insto.models import Profile
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
# /propic
# ---------------------------------------------------------------------------


@command(
    "propic",
    "Download the HD profile picture of the active target",
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
    # path-traversal guard at the user-input boundary.
    dest_dir = ctx.facade._media_dir(username, "propic")
    dest = dest_dir / profile.pk
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


@command("email", "Show the public email (if any) for the active target")
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


@command("phone", "Show the public phone (if any) for the active target")
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


__all__ = [
    "email_cmd",
    "export_cmd",
    "info_cmd",
    "phone_cmd",
    "propic_cmd",
]
