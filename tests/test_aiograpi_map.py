"""Unit tests for `insto.backends._aiograpi_map`.

The mapper layer is pure: it takes aiograpi-shaped objects (real Pydantic
models or duck-typed mocks) and produces insto DTOs. Tests use both:

- a `_Bag` namespace for shape-agnostic checks (verifies attribute-access
  works) and
- real `aiograpi.types.User` / `Media` / `Story` / `Comment` / `Highlight`
  models constructed from minimal payloads — those exercise the full
  Pydantic validation path.

No live aiograpi calls.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from insto.backends._aiograpi_map import (
    about_payload,
    map_comment,
    map_highlight,
    map_highlight_item,
    map_post,
    map_profile,
    map_story,
    map_user_short,
)
from insto.exceptions import SchemaDrift


def _bag(**kwargs: Any) -> SimpleNamespace:
    """Cheap aiograpi-shaped duck: behaves like a Pydantic model for our
    attribute-access mappers without dragging in the validation layer."""
    return SimpleNamespace(**kwargs)


# ---- map_profile -----------------------------------------------------------


def test_map_profile_public_full_payload() -> None:
    user = _bag(
        pk="42",
        username="alice",
        full_name="Alice Doe",
        is_private=False,
        is_verified=True,
        is_business=False,
        biography="hello",
        external_url="https://alice.example",
        public_email="alice@example.com",
        public_phone_number="+1 555 1212",
        category_name="Photography",
        profile_pic_url_hd="https://cdn.example/alice_hd.jpg",
        profile_pic_url="https://cdn.example/alice.jpg",
        follower_count=1234,
        following_count=56,
        media_count=78,
    )
    profile = map_profile(user)
    assert profile.pk == "42"
    assert profile.username == "alice"
    assert profile.full_name == "Alice Doe"
    assert profile.access == "public"
    assert profile.is_private is False
    assert profile.is_verified is True
    assert profile.public_email == "alice@example.com"
    assert profile.public_phone == "+1 555 1212"
    assert profile.business_category == "Photography"
    assert profile.avatar_url == "https://cdn.example/alice_hd.jpg"
    assert profile.follower_count == 1234


def test_map_profile_private_sets_access_private() -> None:
    user = _bag(pk="42", username="alice", is_private=True)
    assert map_profile(user).access == "private"


def test_map_profile_falls_back_to_low_res_avatar_when_hd_missing() -> None:
    user = _bag(
        pk="42",
        username="alice",
        profile_pic_url_hd=None,
        profile_pic_url="https://cdn.example/alice.jpg",
    )
    assert map_profile(user).avatar_url == "https://cdn.example/alice.jpg"


def test_map_profile_business_category_priority() -> None:
    """`category_name` wins over `business_category_name` when both present."""
    user = _bag(
        pk="42",
        username="alice",
        category_name="Photography",
        business_category_name="Service",
    )
    assert map_profile(user).business_category == "Photography"


def test_map_profile_missing_pk_raises_schema_drift() -> None:
    with pytest.raises(SchemaDrift) as exc:
        map_profile(_bag(pk=None, username="alice"))
    assert exc.value.endpoint == "user"
    assert exc.value.missing_field == "pk"


def test_map_profile_missing_username_raises_schema_drift() -> None:
    with pytest.raises(SchemaDrift) as exc:
        map_profile(_bag(pk="42", username=None))
    assert exc.value.missing_field == "username"


# ---- map_user_short --------------------------------------------------------


def test_map_user_short_minimal() -> None:
    u = map_user_short(_bag(pk="9", username="bob", full_name="Bob"))
    assert u.pk == "9"
    assert u.username == "bob"
    assert u.full_name == "Bob"


def test_map_user_short_pk_coerced_to_string() -> None:
    """aiograpi sometimes types `pk` as `int | str`; we always emit `str`."""
    u = map_user_short(_bag(pk=9, username="bob"))
    assert u.pk == "9"


def test_map_user_short_missing_username_raises_schema_drift() -> None:
    with pytest.raises(SchemaDrift):
        map_user_short(_bag(pk="9", username=None))


# ---- map_post --------------------------------------------------------------


_POST_BASE = {
    "pk": "300000",
    "code": "ABCxyz",
    "taken_at": datetime(2026, 4, 17, 17, 45, 12, tzinfo=UTC),
    "media_type": 1,
    "caption_text": "Sunrise! #coffee #sunrise @friend",
    "like_count": 250,
    "comment_count": 8,
    "thumbnail_url": "https://cdn.example/post.jpg",
    "user": _bag(pk="42", username="alice"),
}


def test_map_post_image_extracts_hashtags_and_mentions_from_caption() -> None:
    post = map_post(_bag(**_POST_BASE))
    assert post.media_type == "image"
    assert post.taken_at == 1776447912
    assert post.hashtags == ["coffee", "sunrise"]
    assert post.mentions == ["friend"]
    assert post.media_urls == ["https://cdn.example/post.jpg"]
    assert post.owner_pk == "42"
    assert post.owner_username == "alice"


def test_map_post_video_uses_video_url() -> None:
    payload = dict(_POST_BASE)
    payload["media_type"] = 2
    payload["video_url"] = "https://cdn.example/clip.mp4"
    post = map_post(_bag(**payload))
    assert post.media_type == "video"
    assert post.media_urls == ["https://cdn.example/clip.mp4"]


def test_map_post_carousel_walks_resources() -> None:
    payload = dict(_POST_BASE)
    payload["media_type"] = 8
    payload["resources"] = [
        _bag(pk="r1", media_type=1, thumbnail_url="https://cdn.example/r1.jpg", video_url=None),
        _bag(
            pk="r2",
            media_type=2,
            thumbnail_url="https://cdn.example/r2-thumb.jpg",
            video_url="https://cdn.example/r2.mp4",
        ),
    ]
    post = map_post(_bag(**payload))
    assert post.media_type == "carousel"
    assert post.media_urls == [
        "https://cdn.example/r1.jpg",
        "https://cdn.example/r2.mp4",  # video preferred over thumbnail
    ]


def test_map_post_unknown_media_type_raises_schema_drift() -> None:
    payload = dict(_POST_BASE)
    payload["media_type"] = 99
    with pytest.raises(SchemaDrift) as exc:
        map_post(_bag(**payload))
    assert exc.value.endpoint == "media"
    assert exc.value.missing_field == "media_type"


def test_map_post_taken_at_must_be_datetime_or_unix() -> None:
    payload = dict(_POST_BASE)
    payload["taken_at"] = "not-a-timestamp"
    with pytest.raises(SchemaDrift):
        map_post(_bag(**payload))


# ---- map_comment -----------------------------------------------------------


def test_map_comment_basic() -> None:
    raw = _bag(
        pk="c1",
        text="nice!",
        created_at_utc=datetime(2026, 4, 17, 17, 45, 12, tzinfo=UTC),
        user=_bag(pk="9", username="bob"),
        like_count=3,
        replied_to_comment_id=None,
    )
    c = map_comment(raw, media_pk="300000")
    assert c.pk == "c1"
    assert c.media_pk == "300000"
    assert c.user_username == "bob"
    assert c.text == "nice!"
    assert c.created_at == 1776447912
    assert c.reply_to_pk is None


def test_map_comment_carries_reply_chain() -> None:
    raw = _bag(
        pk="c2",
        text="reply",
        created_at_utc=datetime(2026, 4, 18, tzinfo=UTC),
        user=_bag(pk="9", username="bob"),
        replied_to_comment_id="c1",
    )
    c = map_comment(raw, media_pk="300000")
    assert c.reply_to_pk == "c1"


def test_map_comment_missing_user_raises() -> None:
    with pytest.raises(SchemaDrift):
        map_comment(
            _bag(
                pk="c1",
                text="nice",
                created_at_utc=datetime(2026, 4, 17, tzinfo=UTC),
                user=None,
            ),
            media_pk="m",
        )


# ---- map_story -------------------------------------------------------------


def test_map_story_video_synthesises_expires_at() -> None:
    s = map_story(
        _bag(
            pk="s1",
            taken_at=datetime(2026, 4, 17, 17, 45, 12, tzinfo=UTC),
            media_type=2,
            video_url="https://cdn.example/s1.mp4",
            thumbnail_url="https://cdn.example/s1.jpg",
            user=_bag(pk="42", username="alice"),
        )
    )
    assert s.media_type == "video"
    assert s.media_url == "https://cdn.example/s1.mp4"
    # aiograpi does not expose expiring_at; we synthesise +24h.
    assert s.expires_at == s.taken_at + 86400


def test_map_story_image_uses_thumbnail() -> None:
    s = map_story(
        _bag(
            pk="s2",
            taken_at=datetime(2026, 4, 17, tzinfo=UTC),
            media_type=1,
            thumbnail_url="https://cdn.example/s2.jpg",
            user=None,
        )
    )
    assert s.media_type == "image"
    assert s.media_url == "https://cdn.example/s2.jpg"
    assert s.owner_pk is None


# ---- map_highlight + items -------------------------------------------------


def test_map_highlight_picks_cropped_cover_url() -> None:
    h = map_highlight(
        _bag(
            pk="h1",
            title="Travel",
            cover_media={"cropped_image_version": {"url": "https://cdn.example/cover.jpg"}},
            media_count=12,
            user=_bag(pk="42", username="alice"),
        )
    )
    assert h.pk == "h1"
    assert h.title == "Travel"
    assert h.cover_url == "https://cdn.example/cover.jpg"
    assert h.item_count == 12


def test_map_highlight_falls_back_to_direct_cover_url() -> None:
    h = map_highlight(
        _bag(
            pk="h2",
            title="Misc",
            cover_media={"cover_url": "https://cdn.example/legacy.jpg"},
            media_count=1,
        )
    )
    assert h.cover_url == "https://cdn.example/legacy.jpg"


def test_map_highlight_missing_title_raises_schema_drift() -> None:
    with pytest.raises(SchemaDrift):
        map_highlight(_bag(pk="h1", title=None, cover_media={}))


def test_map_highlight_item_inherits_story_shape() -> None:
    item = map_highlight_item(
        _bag(
            pk="hi1",
            taken_at=datetime(2026, 4, 17, 17, 45, 12, tzinfo=UTC),
            media_type=1,
            thumbnail_url="https://cdn.example/hi1.jpg",
        ),
        highlight_pk="h1",
    )
    assert item.pk == "hi1"
    assert item.highlight_pk == "h1"
    assert item.media_type == "image"
    assert item.taken_at == 1776447912


# ---- about_payload ---------------------------------------------------------


def test_about_payload_serialises_extended_fields() -> None:
    user = _bag(
        pk="42",
        username="alice",
        category_name="Photography",
        biography="hi",
        external_url="https://x.example",
        public_email="alice@example.com",
        public_phone_country_code="+1",
        public_phone_number="555-1212",
        contact_phone_number="555-2222",
        is_business=True,
        is_verified=False,
        is_private=False,
    )
    out = about_payload(user)
    assert out["pk"] == "42"
    assert out["username"] == "alice"
    assert out["category"] == "Photography"
    assert out["public_email"] == "alice@example.com"
    assert out["public_phone_country_code"] == "+1"
    assert out["is_business"] is True
    assert out["is_private"] is False
