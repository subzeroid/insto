"""Pure mappers from HikerAPI raw payloads to insto DTOs.

These functions are the *only* place where raw `dict[str, Any]` from the
HikerAPI SDK is interpreted. Above the mapper layer, code consumes
`Profile` / `Post` / `User` / `Comment` / `Story` / `Highlight` DTOs and
never touches a HikerAPI key by name.

Contract:

- Each mapper takes a raw dict (and optional context kwargs the endpoint
  does not provide inline, e.g. `media_pk` for comments) and returns one
  fully-populated DTO.
- Documented required fields raise `SchemaDrift(endpoint, missing_field)`
  when absent or `None`. Never `KeyError`. The `endpoint` label identifies
  the mapper (`"user"`, `"media"`, `"user_short"`, `"comment"`, `"story"`,
  `"highlight"`) so callers / logs can pinpoint which response shape
  drifted upstream.
- Optional fields fall back to dataclass defaults; mappers never invent
  data the payload does not contain.
- Pure: no I/O, no logging, no global state. Safe to call from any thread.

The HikerAPI v2 response shape mirrors Instagram's private API
(`UserV1` / `MediaV1` / `CommentV1` / `StoryV1` style), so these mappers
also work as a reference for what each insto DTO field is sourced from.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from insto.exceptions import SchemaDrift
from insto.models import (
    Comment,
    Highlight,
    HighlightItem,
    Post,
    Profile,
    Story,
    User,
)

# ---- helpers ---------------------------------------------------------------

_HASHTAG_RE = re.compile(r"(?<![\w])#([A-Za-z0-9_]+)")
_MENTION_RE = re.compile(r"(?<![\w])@([A-Za-z0-9._]+)")

_MEDIA_TYPE_POST: dict[int, str] = {1: "image", 2: "video", 8: "carousel"}
_MEDIA_TYPE_STORY: dict[int, str] = {1: "image", 2: "video"}


def _require(d: dict[str, Any], key: str, endpoint: str) -> Any:
    """Return `d[key]`, raising `SchemaDrift` if missing or `None`."""
    value = d.get(key)
    if value is None:
        raise SchemaDrift(endpoint, key)
    return value


def _to_unix(value: Any, *, endpoint: str, field: str) -> int:
    """Coerce a HikerAPI timestamp to unix-seconds.

    HikerAPI is inconsistent: some endpoints return integer unix seconds,
    others return ISO-8601 strings like ``2026-04-17T17:45:12Z``. Both
    shapes are documented as the same logical field, so we accept either
    and raise SchemaDrift only when the value is genuinely unparseable.
    """
    if isinstance(value, bool):
        # bool is a subclass of int, so it would silently slip through;
        # reject explicitly to surface the upstream bug instead of
        # storing taken_at=1.
        raise SchemaDrift(endpoint, field)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        s = value.strip()
        if s.isdigit() or (s.startswith("-") and s[1:].isdigit()):
            return int(s)
        try:
            # fromisoformat in 3.11+ handles trailing 'Z' as UTC.
            normalized = s.replace("Z", "+00:00") if s.endswith("Z") else s
            return int(datetime.fromisoformat(normalized).timestamp())
        except ValueError as exc:
            raise SchemaDrift(endpoint, field) from exc
    raise SchemaDrift(endpoint, field)


def _opt_str(value: Any) -> str | None:
    """Coerce optional payload value to `str | None` (drop empty strings)."""
    if value is None:
        return None
    s = str(value)
    return s or None


def _hashtags(caption: str) -> list[str]:
    return _HASHTAG_RE.findall(caption)


def _mentions(caption: str) -> list[str]:
    return _MENTION_RE.findall(caption)


def _post_media_urls(d: dict[str, Any], media_type: str) -> list[str]:
    """Assemble the media URL list for a Post DTO.

    image  → [thumbnail]
    video  → [video_url]
    carousel → one URL per resource (video_url if video, else thumbnail)
    """
    if media_type == "image":
        thumb = d.get("thumbnail_url")
        return [str(thumb)] if thumb else []
    if media_type == "video":
        video = d.get("video_url")
        return [str(video)] if video else []
    # carousel
    urls: list[str] = []
    for r in d.get("resources") or []:
        if not isinstance(r, dict):
            continue
        if r.get("media_type") == 2 and r.get("video_url"):
            urls.append(str(r["video_url"]))
        elif r.get("thumbnail_url"):
            urls.append(str(r["thumbnail_url"]))
    return urls


# ---- profile / user --------------------------------------------------------


def map_profile(d: dict[str, Any]) -> Profile:
    """Map a HikerAPI `user` payload to a `Profile` DTO.

    Required: `pk`, `username`. Avatar URL prefers `_hd` then base.
    `access` is derived from `is_private` (v0.1: HikerAPI cannot represent
    `followed` / `blocked` — those land with the aiograpi backend in v0.2).
    """
    pk = _require(d, "pk", "user")
    username = _require(d, "username", "user")
    is_private = bool(d.get("is_private", False))
    avatar = d.get("profile_pic_url_hd") or d.get("profile_pic_url")
    return Profile(
        pk=str(pk),
        username=str(username),
        access="private" if is_private else "public",
        full_name=str(d.get("full_name") or ""),
        biography=str(d.get("biography") or ""),
        external_url=_opt_str(d.get("external_url")),
        is_verified=bool(d.get("is_verified", False)),
        is_business=bool(d.get("is_business", False)),
        is_private=is_private,
        public_email=_opt_str(d.get("public_email")),
        public_phone=_opt_str(d.get("public_phone_number") or d.get("contact_phone_number")),
        business_category=_opt_str(d.get("business_category_name") or d.get("category_name")),
        follower_count=int(d.get("follower_count") or 0),
        following_count=int(d.get("following_count") or 0),
        media_count=int(d.get("media_count") or 0),
        avatar_url=_opt_str(avatar),
    )


def map_user(d: dict[str, Any]) -> User:
    """Map a short user record (followers / following / likers list item)."""
    pk = _require(d, "pk", "user_short")
    username = _require(d, "username", "user_short")
    return User(
        pk=str(pk),
        username=str(username),
        full_name=str(d.get("full_name") or ""),
        is_private=bool(d.get("is_private", False)),
        is_verified=bool(d.get("is_verified", False)),
    )


# ---- post ------------------------------------------------------------------


def map_post(d: dict[str, Any]) -> Post:
    """Map a HikerAPI `media` payload to a `Post` DTO.

    Required: `pk`, `code`, `taken_at`, `media_type`. Hashtags / mentions
    are parsed from the caption (HikerAPI does not return them as separate
    structured fields). For carousels, each resource contributes one URL.
    """
    pk = _require(d, "pk", "media")
    code = _require(d, "code", "media")
    taken_at = _require(d, "taken_at", "media")
    raw_type = _require(d, "media_type", "media")
    media_type = _MEDIA_TYPE_POST.get(int(raw_type))
    if media_type is None:
        raise SchemaDrift("media", "media_type")

    caption = str(d.get("caption_text") or "")
    location = d.get("location") if isinstance(d.get("location"), dict) else None
    user = d.get("user") if isinstance(d.get("user"), dict) else None

    return Post(
        pk=str(pk),
        code=str(code),
        taken_at=_to_unix(taken_at, endpoint="media", field="taken_at"),
        media_type=media_type,  # type: ignore[arg-type]
        caption=caption,
        like_count=int(d.get("like_count") or 0),
        comment_count=int(d.get("comment_count") or 0),
        location_name=_opt_str(location.get("name")) if location else None,
        location_pk=_opt_str(location.get("pk")) if location else None,
        hashtags=_hashtags(caption),
        mentions=_mentions(caption),
        media_urls=_post_media_urls(d, media_type),
        thumbnail_url=_opt_str(d.get("thumbnail_url")),
        owner_pk=_opt_str(user.get("pk")) if user else None,
        owner_username=_opt_str(user.get("username")) if user else None,
    )


# ---- comment ---------------------------------------------------------------


def map_comment(d: dict[str, Any], *, media_pk: str) -> Comment:
    """Map a HikerAPI comment payload. `media_pk` is supplied by the caller.

    The HikerAPI comment list endpoint scopes responses to one media but
    does not echo the parent media pk inside each comment record. Callers
    pass it explicitly so the resulting `Comment` DTO is self-describing.
    """
    pk = _require(d, "pk", "comment")
    text = _require(d, "text", "comment")
    created_at = _require(d, "created_at", "comment")
    user = d.get("user")
    if not isinstance(user, dict):
        raise SchemaDrift("comment", "user")
    user_pk = _require(user, "pk", "comment")
    user_username = _require(user, "username", "comment")
    reply_to = d.get("replied_to_comment_id") or d.get("parent_comment_id")
    return Comment(
        pk=str(pk),
        media_pk=str(media_pk),
        user_pk=str(user_pk),
        user_username=str(user_username),
        text=str(text),
        created_at=_to_unix(created_at, endpoint="comment", field="created_at"),
        like_count=int(d.get("comment_like_count") or 0),
        reply_to_pk=_opt_str(reply_to),
    )


# ---- story -----------------------------------------------------------------


def map_story(d: dict[str, Any]) -> Story:
    """Map a HikerAPI story payload to a `Story` DTO.

    Required: `pk`, `taken_at`, `media_type`. `expiring_at` defaults to
    `taken_at + 86400` if absent (Instagram's documented 24h TTL).
    """
    pk = _require(d, "pk", "story")
    taken_at = _to_unix(_require(d, "taken_at", "story"), endpoint="story", field="taken_at")
    raw_type = _require(d, "media_type", "story")
    media_type = _MEDIA_TYPE_STORY.get(int(raw_type))
    if media_type is None:
        raise SchemaDrift("story", "media_type")

    raw_expiry = d.get("expiring_at") or d.get("expires_at")
    expires_at = (
        _to_unix(raw_expiry, endpoint="story", field="expiring_at")
        if raw_expiry is not None
        else taken_at + 86400
    )
    media_url = (
        (d.get("video_url") if media_type == "video" else d.get("thumbnail_url"))
        or d.get("thumbnail_url")
        or ""
    )
    user = d.get("user") if isinstance(d.get("user"), dict) else None

    return Story(
        pk=str(pk),
        taken_at=taken_at,
        expires_at=expires_at,
        media_type=media_type,  # type: ignore[arg-type]
        media_url=str(media_url),
        thumbnail_url=_opt_str(d.get("thumbnail_url")),
        owner_pk=_opt_str(user.get("pk")) if user else None,
        owner_username=_opt_str(user.get("username")) if user else None,
    )


# ---- highlight -------------------------------------------------------------


def map_highlight(d: dict[str, Any]) -> Highlight:
    """Map a HikerAPI highlight reel payload to a `Highlight` DTO.

    Required: `pk`, `title`. Cover URL is dug out of `cover_media`'s
    nested `cropped_image_version` (HikerAPI follows Instagram's nested
    shape) and falls back to direct `cover_url` / `cover_image` keys
    seen in some endpoints.
    """
    pk = _require(d, "pk", "highlight")
    title = _require(d, "title", "highlight")

    cover_url: str | None = None
    cover_media = d.get("cover_media")
    if isinstance(cover_media, dict):
        cropped = cover_media.get("cropped_image_version")
        if isinstance(cropped, dict):
            cover_url = _opt_str(cropped.get("url"))
    cover_url = cover_url or _opt_str(d.get("cover_url") or d.get("cover_image"))

    user = d.get("user") if isinstance(d.get("user"), dict) else None

    return Highlight(
        pk=str(pk),
        title=str(title),
        cover_url=cover_url,
        item_count=int(d.get("media_count") or 0),
        owner_pk=_opt_str(user.get("pk")) if user else None,
        owner_username=_opt_str(user.get("username")) if user else None,
    )


def map_highlight_item(d: dict[str, Any], *, highlight_pk: str) -> HighlightItem:
    """Map a single item inside a highlight reel.

    Items are essentially preserved stories — the same shape as a story
    payload, scoped to a parent highlight. `highlight_pk` is supplied by
    the caller because the items endpoint does not echo it.
    """
    pk = _require(d, "pk", "highlight_item")
    taken_at = _to_unix(
        _require(d, "taken_at", "highlight_item"),
        endpoint="highlight_item",
        field="taken_at",
    )
    raw_type = _require(d, "media_type", "highlight_item")
    media_type = _MEDIA_TYPE_STORY.get(int(raw_type))
    if media_type is None:
        raise SchemaDrift("highlight_item", "media_type")
    media_url = (
        (d.get("video_url") if media_type == "video" else d.get("thumbnail_url"))
        or d.get("thumbnail_url")
        or ""
    )
    return HighlightItem(
        pk=str(pk),
        highlight_pk=str(highlight_pk),
        taken_at=taken_at,
        media_type=media_type,  # type: ignore[arg-type]
        media_url=str(media_url),
        thumbnail_url=_opt_str(d.get("thumbnail_url")),
    )
