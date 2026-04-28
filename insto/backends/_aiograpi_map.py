"""Pure mappers from aiograpi Pydantic models to insto DTOs.

aiograpi exposes Instagram's private API as Pydantic models (User, Media,
Story, Comment, Highlight, ...). This module is the *only* place where
those models are interpreted: above the mapper layer, code consumes
`Profile` / `Post` / `User` / `Comment` / `Story` / `Highlight` DTOs.

Contract:

- Each mapper takes an aiograpi model (or compatible mock) and returns
  one fully-populated DTO.
- Required fields raise `SchemaDrift(endpoint, missing_field)` when
  absent or `None`. Never `AttributeError`.
- Optional fields fall back to dataclass defaults.
- Pure: no I/O, no logging, no global state. Thread-safe.

Pydantic v2 models can be read either as `obj.field` (attribute) or via
`obj.model_dump()`. We use attribute access — it skips the dict copy and
sidesteps datetime↔ISO serialization quirks.

Difference from `_hiker_map`:

- aiograpi returns `taken_at: datetime` (already a Python datetime),
  HikerAPI returns int unix or ISO-8601 string. We unwrap the datetime
  to unix int here so the DTOs stay comparable across backends.
- aiograpi returns `HttpUrl` for image / video URLs; cast to `str()` so
  CDN streaming code receives plain strings.
- `pk` is sometimes typed `int | str`; coerce to `str` everywhere — the
  backend boundary is always strings (see `_base.OSINTBackend`).
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


def _require(obj: Any, attr: str, endpoint: str) -> Any:
    """Read `obj.attr`, raising `SchemaDrift` on `None` / missing."""
    value = getattr(obj, attr, None)
    if value is None:
        raise SchemaDrift(endpoint, attr)
    return value


def _opt(obj: Any, attr: str) -> Any:
    """Read `obj.attr`, returning `None` if missing."""
    return getattr(obj, attr, None)


def _opt_str(value: Any) -> str | None:
    """Coerce optional value to `str | None`. Drops empty strings."""
    if value is None:
        return None
    s = str(value)
    return s or None


def _to_unix(value: Any, *, endpoint: str, field: str) -> int:
    """Coerce a Pydantic-or-raw timestamp into unix seconds.

    aiograpi normalises Instagram's mixed `taken_at` shapes into a
    `datetime.datetime`; we collapse it back to unix so the DTOs
    interop with `_hiker_map` outputs.
    """
    if value is None:
        raise SchemaDrift(endpoint, field)
    if isinstance(value, datetime):
        return int(value.timestamp())
    if isinstance(value, bool):
        # bool is a subclass of int — guard explicitly.
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
            normalized = s.replace("Z", "+00:00") if s.endswith("Z") else s
            return int(datetime.fromisoformat(normalized).timestamp())
        except ValueError as err:
            raise SchemaDrift(endpoint, field) from err
    raise SchemaDrift(endpoint, field)


def _hashtags(caption: str) -> list[str]:
    return _HASHTAG_RE.findall(caption)


def _mentions(caption: str) -> list[str]:
    return _MENTION_RE.findall(caption)


# ---- profile ---------------------------------------------------------------


def map_profile(user: Any) -> Profile:
    """Map an aiograpi `User` to a `Profile` DTO.

    Required: `pk`, `username`. Everything else falls back to defaults.
    `access` is derived from `is_private`; `aiograpi` does not expose a
    `followed`/`blocked`/`deleted` discriminator on the user object —
    those land on error paths (`PrivateAccount`, `UserNotFound`, etc.).
    """
    pk = _require(user, "pk", "user")
    username = _require(user, "username", "user")
    is_private = bool(_opt(user, "is_private"))
    avatar = _opt(user, "profile_pic_url_hd") or _opt(user, "profile_pic_url")
    business_category = (
        _opt(user, "category_name")
        or _opt(user, "business_category_name")
        or _opt(user, "category")
    )
    return Profile(
        pk=str(pk),
        username=str(username),
        full_name=_opt_str(_opt(user, "full_name")) or "",
        access="private" if is_private else "public",
        is_verified=bool(_opt(user, "is_verified")),
        is_private=is_private,
        is_business=bool(_opt(user, "is_business")),
        biography=_opt_str(_opt(user, "biography")) or "",
        external_url=_opt_str(_opt(user, "external_url")),
        public_email=_opt_str(_opt(user, "public_email")),
        public_phone=_opt_str(_opt(user, "public_phone_number")),
        business_category=_opt_str(business_category),
        avatar_url=_opt_str(avatar),
        follower_count=int(_opt(user, "follower_count") or 0),
        following_count=int(_opt(user, "following_count") or 0),
        media_count=int(_opt(user, "media_count") or 0),
    )


# ---- user_short ------------------------------------------------------------


def map_user_short(user: Any) -> User:
    """Map a `UserShort` to our minimal `User` DTO (used in followers /
    following / mutuals / suggestion lists)."""
    pk = _require(user, "pk", "user_short")
    username = _require(user, "username", "user_short")
    return User(
        pk=str(pk),
        username=str(username),
        full_name=_opt_str(_opt(user, "full_name")) or "",
        is_verified=bool(_opt(user, "is_verified")),
        is_private=bool(_opt(user, "is_private")),
    )


# ---- media (post) ----------------------------------------------------------


def _post_media_urls(media: Any, media_type: str) -> list[str]:
    """Pick the canonical URL list for a `Media`.

    image  → [highest-res candidate from `image_versions2`]
    video  → [video_url]
    carousel → one URL per `Resource`, video preferred when present.
    """
    if media_type == "image":
        ivs = _opt(media, "image_versions2")
        if ivs is not None:
            candidates = getattr(ivs, "items", None) or []
            if candidates:
                # Pick the largest by width (ig returns multiple sizes).
                best = max(
                    candidates,
                    key=lambda c: getattr(c, "width", 0) or 0,
                )
                url = _opt(best, "url")
                if url:
                    return [str(url)]
        thumb = _opt(media, "thumbnail_url")
        return [str(thumb)] if thumb else []
    if media_type == "video":
        video = _opt(media, "video_url")
        return [str(video)] if video else []
    # carousel
    urls: list[str] = []
    for res in _opt(media, "resources") or []:
        rtype = _MEDIA_TYPE_POST.get(int(_opt(res, "media_type") or 0))
        if rtype == "video":
            v = _opt(res, "video_url")
            if v:
                urls.append(str(v))
                continue
        thumb = _opt(res, "thumbnail_url")
        if thumb:
            urls.append(str(thumb))
    return urls


def map_post(media: Any) -> Post:
    """Map an aiograpi `Media` to a `Post` DTO."""
    pk = _require(media, "pk", "media")
    code = _require(media, "code", "media")
    taken_at = _to_unix(_require(media, "taken_at", "media"), endpoint="media", field="taken_at")
    raw_type = _require(media, "media_type", "media")
    media_type = _MEDIA_TYPE_POST.get(int(raw_type))
    if media_type is None:
        raise SchemaDrift("media", "media_type")

    caption = str(_opt(media, "caption_text") or "")
    location = _opt(media, "location")
    user = _opt(media, "user")
    return Post(
        pk=str(pk),
        code=str(code),
        taken_at=taken_at,
        media_type=media_type,  # type: ignore[arg-type]
        caption=caption,
        like_count=int(_opt(media, "like_count") or 0),
        comment_count=int(_opt(media, "comment_count") or 0),
        location_name=_opt_str(_opt(location, "name")) if location is not None else None,
        location_pk=_opt_str(_opt(location, "pk")) if location is not None else None,
        hashtags=_hashtags(caption),
        mentions=_mentions(caption),
        media_urls=_post_media_urls(media, media_type),
        thumbnail_url=_opt_str(_opt(media, "thumbnail_url")),
        owner_pk=_opt_str(_opt(user, "pk")) if user is not None else None,
        owner_username=_opt_str(_opt(user, "username")) if user is not None else None,
    )


# ---- comment ---------------------------------------------------------------


def map_comment(comment: Any, *, media_pk: str) -> Comment:
    """Map an aiograpi `Comment` to a `Comment` DTO. `media_pk` is supplied
    by the caller because aiograpi's comment objects don't carry the parent
    media reference inline."""
    pk = _require(comment, "pk", "comment")
    text = _require(comment, "text", "comment")
    created = _to_unix(
        _require(comment, "created_at_utc", "comment"),
        endpoint="comment",
        field="created_at_utc",
    )
    user = _require(comment, "user", "comment")
    return Comment(
        pk=str(pk),
        media_pk=str(media_pk),
        user_pk=str(_require(user, "pk", "comment")),
        user_username=str(_require(user, "username", "comment")),
        text=str(text),
        created_at=created,
        like_count=int(_opt(comment, "like_count") or 0),
        reply_to_pk=_opt_str(_opt(comment, "replied_to_comment_id")),
    )


# ---- story -----------------------------------------------------------------


def map_story(story: Any) -> Story:
    """Map an aiograpi `Story` to a `Story` DTO.

    aiograpi's `Story` does not expose `expiring_at`. Instagram's
    documented TTL is 24h, so we synthesise `taken_at + 86400` to keep
    the DTO shape consistent with `_hiker_map.map_story`.
    """
    pk = _require(story, "pk", "story")
    taken_at = _to_unix(_require(story, "taken_at", "story"), endpoint="story", field="taken_at")
    raw_type = _require(story, "media_type", "story")
    media_type = _MEDIA_TYPE_STORY.get(int(raw_type))
    if media_type is None:
        raise SchemaDrift("story", "media_type")
    media_url = (
        (_opt(story, "video_url") if media_type == "video" else _opt(story, "thumbnail_url"))
        or _opt(story, "thumbnail_url")
        or ""
    )
    user = _opt(story, "user")
    return Story(
        pk=str(pk),
        taken_at=taken_at,
        expires_at=taken_at + 86400,
        media_type=media_type,  # type: ignore[arg-type]
        media_url=str(media_url),
        thumbnail_url=_opt_str(_opt(story, "thumbnail_url")),
        owner_pk=_opt_str(_opt(user, "pk")) if user is not None else None,
        owner_username=_opt_str(_opt(user, "username")) if user is not None else None,
    )


# ---- highlight -------------------------------------------------------------


def _highlight_cover_url(cover_media: Any) -> str | None:
    """`cover_media` is a `dict` in aiograpi, not a Pydantic model. Walk it
    defensively for the cropped image URL."""
    if not isinstance(cover_media, dict):
        return None
    cropped = cover_media.get("cropped_image_version")
    if isinstance(cropped, dict):
        url = cropped.get("url")
        if url:
            return str(url)
    direct = cover_media.get("cover_url") or cover_media.get("cover_image")
    if direct:
        return str(direct)
    return None


def map_highlight(highlight: Any) -> Highlight:
    """Map an aiograpi `Highlight` to a `Highlight` DTO. Items are not
    populated here — fetch them via `iter_highlight_items`."""
    pk = _require(highlight, "pk", "highlight")
    title = _require(highlight, "title", "highlight")
    user = _opt(highlight, "user")
    return Highlight(
        pk=str(pk),
        title=str(title),
        cover_url=_highlight_cover_url(_opt(highlight, "cover_media")),
        item_count=int(_opt(highlight, "media_count") or 0),
        owner_pk=_opt_str(_opt(user, "pk")) if user is not None else None,
        owner_username=_opt_str(_opt(user, "username")) if user is not None else None,
    )


def map_highlight_item(story: Any, *, highlight_pk: str) -> HighlightItem:
    """A highlight item is a preserved `Story`. The mapper mirrors
    `map_story` but emits a `HighlightItem` DTO scoped to its parent."""
    pk = _require(story, "pk", "highlight_item")
    taken_at = _to_unix(
        _require(story, "taken_at", "highlight_item"),
        endpoint="highlight_item",
        field="taken_at",
    )
    raw_type = _require(story, "media_type", "highlight_item")
    media_type = _MEDIA_TYPE_STORY.get(int(raw_type))
    if media_type is None:
        raise SchemaDrift("highlight_item", "media_type")
    media_url = (
        (_opt(story, "video_url") if media_type == "video" else _opt(story, "thumbnail_url"))
        or _opt(story, "thumbnail_url")
        or ""
    )
    return HighlightItem(
        pk=str(pk),
        highlight_pk=str(highlight_pk),
        taken_at=taken_at,
        media_type=media_type,  # type: ignore[arg-type]
        media_url=str(media_url),
        thumbnail_url=_opt_str(_opt(story, "thumbnail_url")),
    )


# ---- about-payload ---------------------------------------------------------


def about_payload(user: Any) -> dict[str, Any]:
    """Synthesise the `user_about_v1`-style dict from the same User model.

    The hiker `user_about_v1` endpoint returns extra public-profile
    metadata; aiograpi exposes most of the same fields directly on the
    `User` object. The dict shape is intentionally limited so command-
    layer code can read it without caring which backend produced it.
    """
    return {
        "pk": str(_opt(user, "pk") or ""),
        "username": str(_opt(user, "username") or ""),
        "category": _opt_str(
            _opt(user, "category_name")
            or _opt(user, "business_category_name")
            or _opt(user, "category")
        ),
        "biography": str(_opt(user, "biography") or ""),
        "external_url": _opt_str(_opt(user, "external_url")),
        "public_email": _opt_str(_opt(user, "public_email")),
        "public_phone_country_code": _opt_str(_opt(user, "public_phone_country_code")),
        "public_phone_number": _opt_str(_opt(user, "public_phone_number")),
        "contact_phone_number": _opt_str(_opt(user, "contact_phone_number")),
        "address_street": _opt_str(_opt(user, "address_street")),
        "city_name": _opt_str(_opt(user, "city_name")),
        "zip": _opt_str(_opt(user, "zip")),
        "is_business": bool(_opt(user, "is_business")),
        "is_verified": bool(_opt(user, "is_verified")),
        "is_private": bool(_opt(user, "is_private")),
    }
