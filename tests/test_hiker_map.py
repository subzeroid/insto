"""Tests for `insto.backends._hiker_map` mappers.

Each mapper is tested against the corresponding fixture in
`tests/fixtures/hiker/` plus a deliberately mutated `*_schema_drift.json`
that asserts the mapper raises `SchemaDrift(endpoint, missing_field)`
instead of `KeyError`.

The mappers are pure functions, so tests are sync.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from insto.backends._hiker_map import (
    map_comment,
    map_highlight,
    map_highlight_item,
    map_post,
    map_profile,
    map_story,
    map_user,
)
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

FIXTURES = Path(__file__).parent / "fixtures" / "hiker"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


# ---- map_profile -----------------------------------------------------------

def test_map_profile_public_full_payload() -> None:
    profile = map_profile(_load("profile_public.json"))
    assert isinstance(profile, Profile)
    assert profile.pk == "12345678"
    assert profile.username == "alice_public"
    assert profile.access == "public"
    assert profile.is_private is False
    assert profile.is_verified is True
    assert profile.full_name == "Alice Public"
    assert profile.biography.startswith("OSINT")
    assert profile.external_url == "https://alice.example"
    assert profile.public_email == "alice@example.com"
    assert profile.public_phone == "+15555550100"
    assert profile.follower_count == 12500
    assert profile.following_count == 432
    assert profile.media_count == 42
    # avatar_url prefers _hd over base.
    assert profile.avatar_url is not None and profile.avatar_url.endswith("avatar_hd.jpg")


def test_map_profile_private_sets_access_private() -> None:
    profile = map_profile(_load("profile_private.json"))
    assert profile.is_private is True
    assert profile.access == "private"
    assert profile.public_email is None
    assert profile.public_phone is None


def test_map_profile_empty_zeros_and_no_avatar() -> None:
    profile = map_profile(_load("profile_empty.json"))
    assert profile.media_count == 0
    assert profile.follower_count == 0
    assert profile.following_count == 0
    assert profile.avatar_url is None
    assert profile.biography == ""


def test_map_profile_deleted_still_maps_baseline_fields() -> None:
    """`profile_deleted.json` carries the minimal fields a backend would
    receive before deciding to raise `ProfileDeleted` upstream — the
    mapper itself stays pure and just produces the DTO."""
    profile = map_profile(_load("profile_deleted.json"))
    assert profile.username == "deactivated_user"
    assert profile.access == "public"  # is_private=false in fixture


def test_map_profile_schema_drift_missing_username() -> None:
    with pytest.raises(SchemaDrift) as exc:
        map_profile(_load("profile_schema_drift.json"))
    assert exc.value.endpoint == "user"
    assert exc.value.missing_field == "username"
    assert "user" in str(exc.value) and "username" in str(exc.value)


def test_map_profile_schema_drift_missing_pk() -> None:
    with pytest.raises(SchemaDrift) as exc:
        map_profile({"username": "x"})
    assert exc.value.endpoint == "user"
    assert exc.value.missing_field == "pk"


def test_map_profile_schema_drift_pk_explicitly_null() -> None:
    """`None` values are treated as missing — must raise SchemaDrift, not KeyError."""
    with pytest.raises(SchemaDrift):
        map_profile({"pk": None, "username": "x"})


# ---- map_user --------------------------------------------------------------

def test_map_user_short_full_payload() -> None:
    user = map_user(_load("user_short.json"))
    assert isinstance(user, User)
    assert user.pk == "55555555"
    assert user.username == "follower_one"
    assert user.full_name == "Follower One"
    assert user.is_private is False
    assert user.is_verified is False


def test_map_user_short_schema_drift_missing_username() -> None:
    with pytest.raises(SchemaDrift) as exc:
        map_user(_load("user_short_schema_drift.json"))
    assert exc.value.endpoint == "user_short"
    assert exc.value.missing_field == "username"


def test_map_user_short_missing_pk_raises_schema_drift() -> None:
    with pytest.raises(SchemaDrift) as exc:
        map_user({"username": "ghost"})
    assert exc.value.missing_field == "pk"


# ---- map_post --------------------------------------------------------------

def test_map_post_image() -> None:
    post = map_post(_load("post_image.json"))
    assert isinstance(post, Post)
    assert post.pk == "3000000000000000001"
    assert post.code == "ABCdef12345"
    assert post.media_type == "image"
    assert post.taken_at == 1714210800
    assert post.like_count == 250
    assert post.comment_count == 8
    assert post.location_name == "San Francisco, California"
    assert post.location_pk == "987654"
    assert post.hashtags == ["coffee", "sunrise"]
    assert post.mentions == ["friend"]
    assert post.media_urls == [
        "https://scontent-iad3-1.cdninstagram.com/v/post1.jpg"
    ]
    assert post.owner_pk == "12345678"
    assert post.owner_username == "alice_public"


def test_map_post_video_uses_video_url() -> None:
    post = map_post(_load("post_video.json"))
    assert post.media_type == "video"
    assert post.media_urls == [
        "https://scontent-iad3-1.cdninstagram.com/v/post2.mp4"
    ]
    assert post.thumbnail_url == "https://scontent-iad3-1.cdninstagram.com/v/post2_thumb.jpg"
    assert post.location_name is None


def test_map_post_carousel_aggregates_resource_urls() -> None:
    post = map_post(_load("post_carousel.json"))
    assert post.media_type == "carousel"
    # one URL per resource: image → thumbnail, video → video_url
    assert post.media_urls == [
        "https://scontent-iad3-1.cdninstagram.com/v/c1.jpg",
        "https://scontent-iad3-1.cdninstagram.com/v/c2.mp4",
    ]


def test_map_post_schema_drift_missing_code() -> None:
    with pytest.raises(SchemaDrift) as exc:
        map_post(_load("post_schema_drift.json"))
    assert exc.value.endpoint == "media"
    assert exc.value.missing_field == "code"


def test_map_post_unknown_media_type_raises_schema_drift() -> None:
    payload = _load("post_image.json") | {"media_type": 99}
    with pytest.raises(SchemaDrift) as exc:
        map_post(payload)
    assert exc.value.endpoint == "media"
    assert exc.value.missing_field == "media_type"


def test_map_post_caption_missing_means_empty_string_no_hashtags() -> None:
    payload = _load("post_image.json")
    del payload["caption_text"]
    post = map_post(payload)
    assert post.caption == ""
    assert post.hashtags == []
    assert post.mentions == []


# ---- map_comment -----------------------------------------------------------

def test_map_comment_basic() -> None:
    comment = map_comment(_load("comment_basic.json"), media_pk="3000000000000000001")
    assert isinstance(comment, Comment)
    assert comment.pk == "17900000000000001"
    assert comment.media_pk == "3000000000000000001"
    assert comment.user_pk == "55555555"
    assert comment.user_username == "follower_one"
    assert comment.text == "love this!"
    assert comment.created_at == 1714210900
    assert comment.like_count == 5
    assert comment.reply_to_pk is None


def test_map_comment_reply_carries_parent_pk() -> None:
    comment = map_comment(_load("comment_reply.json"), media_pk="3000000000000000001")
    assert comment.reply_to_pk == "17900000000000001"


def test_map_comment_schema_drift_missing_text() -> None:
    with pytest.raises(SchemaDrift) as exc:
        map_comment(_load("comment_schema_drift.json"), media_pk="3000000000000000001")
    assert exc.value.endpoint == "comment"
    assert exc.value.missing_field == "text"


def test_map_comment_missing_user_dict_raises_schema_drift() -> None:
    payload = _load("comment_basic.json")
    del payload["user"]
    with pytest.raises(SchemaDrift) as exc:
        map_comment(payload, media_pk="x")
    assert exc.value.endpoint == "comment"
    assert exc.value.missing_field == "user"


def test_map_comment_user_missing_username_raises_schema_drift() -> None:
    payload = _load("comment_basic.json")
    payload["user"] = {"pk": "55555555"}
    with pytest.raises(SchemaDrift) as exc:
        map_comment(payload, media_pk="x")
    assert exc.value.missing_field == "username"


# ---- map_story -------------------------------------------------------------

def test_map_story_image() -> None:
    story = map_story(_load("story_image.json"))
    assert isinstance(story, Story)
    assert story.pk == "3100000000000000001"
    assert story.taken_at == 1714210800
    assert story.expires_at == 1714297200
    assert story.media_type == "image"
    assert story.media_url == "https://scontent-iad3-1.cdninstagram.com/v/story1.jpg"
    assert story.owner_pk == "12345678"
    assert story.owner_username == "alice_public"


def test_map_story_video_uses_video_url() -> None:
    story = map_story(_load("story_video.json"))
    assert story.media_type == "video"
    assert story.media_url == "https://scontent-iad3-1.cdninstagram.com/v/story2.mp4"
    assert story.thumbnail_url == "https://scontent-iad3-1.cdninstagram.com/v/story2_thumb.jpg"


def test_map_story_default_expires_at_is_taken_at_plus_24h() -> None:
    payload = _load("story_image.json")
    del payload["expiring_at"]
    story = map_story(payload)
    assert story.expires_at == story.taken_at + 86400


def test_map_story_schema_drift_missing_taken_at() -> None:
    with pytest.raises(SchemaDrift) as exc:
        map_story(_load("story_schema_drift.json"))
    assert exc.value.endpoint == "story"
    assert exc.value.missing_field == "taken_at"


def test_map_story_unknown_media_type_raises_schema_drift() -> None:
    payload = _load("story_image.json") | {"media_type": 8}  # carousel invalid for story
    with pytest.raises(SchemaDrift) as exc:
        map_story(payload)
    assert exc.value.missing_field == "media_type"


# ---- map_highlight ---------------------------------------------------------

def test_map_highlight_basic() -> None:
    highlight = map_highlight(_load("highlight_basic.json"))
    assert isinstance(highlight, Highlight)
    assert highlight.pk == "highlight:17900000000000010"
    assert highlight.title == "Travels"
    assert highlight.item_count == 12
    assert highlight.cover_url == "https://scontent-iad3-1.cdninstagram.com/v/cover_travels.jpg"
    assert highlight.owner_pk == "12345678"
    assert highlight.owner_username == "alice_public"


def test_map_highlight_falls_back_to_flat_cover_url() -> None:
    payload = {
        "pk": "highlight:1",
        "title": "Flat",
        "media_count": 3,
        "cover_url": "https://scontent-iad3-1.cdninstagram.com/v/flat.jpg",
    }
    highlight = map_highlight(payload)
    assert highlight.cover_url == "https://scontent-iad3-1.cdninstagram.com/v/flat.jpg"


def test_map_highlight_schema_drift_missing_title() -> None:
    with pytest.raises(SchemaDrift) as exc:
        map_highlight(_load("highlight_schema_drift.json"))
    assert exc.value.endpoint == "highlight"
    assert exc.value.missing_field == "title"


def test_map_highlight_item_image() -> None:
    payload = {
        "pk": "3100000000000000010",
        "taken_at": 1714210800,
        "media_type": 1,
        "thumbnail_url": "https://scontent-iad3-1.cdninstagram.com/v/hi1.jpg",
    }
    item = map_highlight_item(payload, highlight_pk="highlight:17900000000000010")
    assert isinstance(item, HighlightItem)
    assert item.highlight_pk == "highlight:17900000000000010"
    assert item.media_type == "image"
    assert item.media_url == "https://scontent-iad3-1.cdninstagram.com/v/hi1.jpg"


def test_map_highlight_item_video_uses_video_url() -> None:
    payload = {
        "pk": "3100000000000000011",
        "taken_at": 1714210900,
        "media_type": 2,
        "thumbnail_url": "https://scontent-iad3-1.cdninstagram.com/v/hi2_thumb.jpg",
        "video_url": "https://scontent-iad3-1.cdninstagram.com/v/hi2.mp4",
    }
    item = map_highlight_item(payload, highlight_pk="highlight:1")
    assert item.media_type == "video"
    assert item.media_url == "https://scontent-iad3-1.cdninstagram.com/v/hi2.mp4"


def test_map_highlight_item_schema_drift_missing_media_type() -> None:
    payload = {
        "pk": "x",
        "taken_at": 1,
    }
    with pytest.raises(SchemaDrift) as exc:
        map_highlight_item(payload, highlight_pk="h")
    assert exc.value.endpoint == "highlight_item"
    assert exc.value.missing_field == "media_type"
