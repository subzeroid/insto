"""DTO models for insto domain objects.

Every model is a `@dataclass(slots=True)` so that:

- Fields are explicit and typo-resistant (extra attribute assignment raises
  `AttributeError`, which catches mapper bugs early).
- Memory footprint stays small even for long iterators (followers / posts).
- `dataclasses.asdict(...)` produces a stable dict for JSON export.

Backend mappers (`backends/_hiker_map.py` and future aiograpi mapper) are
the *only* code allowed to construct these DTOs from raw provider payloads;
above the backend layer, code consumes DTOs and never sees raw dicts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

ProfileAccess = Literal["public", "followed", "private", "blocked", "deleted"]
WatchStatus = Literal["active", "paused"]


@dataclass(slots=True)
class Profile:
    """Instagram profile, as returned by `backend.get_profile(pk)`.

    `pk` is the stable identity (HikerAPI `pk_id`, aiograpi `pk`). `username`
    is mutable metadata: when a rename is detected during snapshot diff, the
    old value is appended to `previous_usernames`.

    `access` reports visibility from the backend's vantage point. v0.1
    (hiker) only ever returns `public | private | deleted`; v0.2 (aiograpi)
    adds `followed | blocked`. `requires_followed=True` means the command
    that produced this DTO needs a logged-in / following session — used by
    commands that gate on follower-only content.
    """

    pk: str
    username: str
    access: ProfileAccess
    full_name: str = ""
    biography: str = ""
    external_url: str | None = None
    is_verified: bool = False
    is_business: bool = False
    is_private: bool = False
    requires_followed: bool = False
    public_email: str | None = None
    public_phone: str | None = None
    business_category: str | None = None
    follower_count: int = 0
    following_count: int = 0
    media_count: int = 0
    avatar_url: str | None = None
    avatar_url_hash: str | None = None
    banner_url: str | None = None
    banner_url_hash: str | None = None
    previous_usernames: list[str] = field(default_factory=list)


@dataclass(slots=True)
class User:
    """Lightweight user reference (followers, following, likers, mentions).

    Carries only what list endpoints reliably return; full profile data is
    fetched separately via `backend.get_profile(pk)`.
    """

    pk: str
    username: str
    full_name: str = ""
    is_private: bool = False
    is_verified: bool = False


@dataclass(slots=True)
class Post:
    """Instagram media item (image, video, carousel)."""

    pk: str
    code: str
    taken_at: int
    media_type: Literal["image", "video", "carousel"]
    caption: str = ""
    like_count: int = 0
    comment_count: int = 0
    location_name: str | None = None
    location_pk: str | None = None
    hashtags: list[str] = field(default_factory=list)
    mentions: list[str] = field(default_factory=list)
    media_urls: list[str] = field(default_factory=list)
    thumbnail_url: str | None = None
    owner_pk: str | None = None
    owner_username: str | None = None


@dataclass(slots=True)
class Comment:
    """Comment on a post."""

    pk: str
    media_pk: str
    user_pk: str
    user_username: str
    text: str
    created_at: int
    like_count: int = 0
    reply_to_pk: str | None = None


@dataclass(slots=True)
class Story:
    """Story item, valid for ~24h from `taken_at`."""

    pk: str
    taken_at: int
    expires_at: int
    media_type: Literal["image", "video"]
    media_url: str
    thumbnail_url: str | None = None
    owner_pk: str | None = None
    owner_username: str | None = None


@dataclass(slots=True)
class Highlight:
    """Highlight reel (album of preserved stories)."""

    pk: str
    title: str
    cover_url: str | None = None
    item_count: int = 0
    owner_pk: str | None = None
    owner_username: str | None = None


@dataclass(slots=True)
class HighlightItem:
    """Single item inside a highlight."""

    pk: str
    highlight_pk: str
    taken_at: int
    media_type: Literal["image", "video"]
    media_url: str
    thumbnail_url: str | None = None


@dataclass(slots=True)
class Quota:
    """Backend quota snapshot.

    HikerAPI is pay-per-call, so `remaining` is "requests left on plan",
    `limit` is unused, and `rate` / `amount` / `currency` come from the
    `/sys/balance` endpoint (USD balance and per-second rate cap).
    """

    remaining: int | None
    limit: int | None = None
    reset_at: int | None = None
    rate: int | None = None
    amount: float | None = None
    currency: str | None = None

    @classmethod
    def with_remaining(
        cls,
        remaining: int,
        *,
        limit: int | None = None,
        reset_at: int | None = None,
        rate: int | None = None,
        amount: float | None = None,
        currency: str | None = None,
    ) -> Quota:
        """Build a Quota from a parsed header response or `/sys/balance` payload."""
        return cls(
            remaining=remaining,
            limit=limit,
            reset_at=reset_at,
            rate=rate,
            amount=amount,
            currency=currency,
        )

    @classmethod
    def unknown(cls) -> Quota:
        """Backend that does not expose quota (aiograpi v0.2)."""
        return cls(remaining=None, limit=None, reset_at=None)


@dataclass(slots=True)
class WatchSpec:
    """A `/watch` registration. Lives in the `watches` sqlite table.

    `interval_seconds` is enforced ≥ 300 (5 minutes) by the watch scheduler,
    not by the dataclass — the DTO only carries state.
    """

    user: str
    interval_seconds: int
    last_ok: int | None = None
    last_error: str | None = None
    status: WatchStatus = "active"


@dataclass(slots=True)
class Snapshot:
    """Profile snapshot for diffing across runs.

    `profile_fields` is a flat dict of the watched scalar fields on `Profile`
    at capture time (full_name, biography, follower_count, etc.). Avatar and
    banner URLs are stored as sha256 hashes only — diffing checks hash
    inequality, not URL identity.
    """

    target_pk: str
    captured_at: int
    profile_fields: dict[str, object] = field(default_factory=dict)
    last_post_pks: list[str] = field(default_factory=list)
    avatar_url_hash: str | None = None
    banner_url_hash: str | None = None
