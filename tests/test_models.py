"""Tests for DTO models."""

from __future__ import annotations

from dataclasses import asdict, fields, is_dataclass

import pytest

from insto.models import (
    Comment,
    Highlight,
    HighlightItem,
    Post,
    Profile,
    Quota,
    Snapshot,
    Story,
    User,
    WatchSpec,
)

ALL_MODELS = [
    Profile,
    User,
    Post,
    Comment,
    Story,
    Highlight,
    HighlightItem,
    Quota,
    WatchSpec,
    Snapshot,
]


@pytest.mark.parametrize("cls", ALL_MODELS)
def test_is_slotted_dataclass(cls: type) -> None:
    assert is_dataclass(cls)
    assert "__slots__" in cls.__dict__, f"{cls.__name__} must use slots=True"


@pytest.mark.parametrize("cls", ALL_MODELS)
def test_no_dict_attribute(cls: type) -> None:
    """Slotted dataclasses must not allow arbitrary attribute assignment."""
    sample = _make_sample(cls)
    with pytest.raises(AttributeError):
        sample.nonexistent_field = "x"  # type: ignore[attr-defined]


def test_profile_construction_minimal() -> None:
    p = Profile(pk="123", username="alice", access="public")
    assert p.pk == "123"
    assert p.username == "alice"
    assert p.access == "public"
    assert p.full_name == ""
    assert p.previous_usernames == []
    assert p.follower_count == 0
    assert p.avatar_url is None


def test_profile_previous_usernames_isolated_per_instance() -> None:
    """Default factory must produce independent lists, not a shared one."""
    a = Profile(pk="1", username="a", access="public")
    b = Profile(pk="2", username="b", access="public")
    a.previous_usernames.append("old_a")
    assert b.previous_usernames == []


def test_profile_asdict_round_trip() -> None:
    p = Profile(
        pk="123",
        username="alice",
        access="public",
        full_name="Alice A.",
        biography="bio",
        follower_count=42,
        previous_usernames=["alyce"],
    )
    d = asdict(p)
    assert d["pk"] == "123"
    assert d["full_name"] == "Alice A."
    assert d["follower_count"] == 42
    assert d["previous_usernames"] == ["alyce"]
    assert set(d.keys()) == {f.name for f in fields(Profile)}


def test_quota_factory_with_remaining() -> None:
    q = Quota.with_remaining(42, limit=1000, reset_at=1700000000)
    assert q.remaining == 42
    assert q.limit == 1000
    assert q.reset_at == 1700000000


def test_quota_factory_unknown() -> None:
    q = Quota.unknown()
    assert q.remaining is None
    assert q.limit is None
    assert q.reset_at is None


def test_watchspec_defaults_to_active() -> None:
    w = WatchSpec(user="alice", interval_seconds=600)
    assert w.status == "active"
    assert w.last_ok is None
    assert w.last_error is None


def test_snapshot_defaults() -> None:
    s = Snapshot(target_pk="123", captured_at=1700000000)
    assert s.profile_fields == {}
    assert s.last_post_pks == []
    assert s.avatar_url_hash is None


def test_snapshot_default_factories_isolated() -> None:
    a = Snapshot(target_pk="1", captured_at=1)
    b = Snapshot(target_pk="2", captured_at=2)
    a.profile_fields["k"] = "v"
    a.last_post_pks.append("p")
    assert b.profile_fields == {}
    assert b.last_post_pks == []


def test_post_construction() -> None:
    p = Post(pk="m1", code="ABC", taken_at=1700000000, media_type="image")
    assert p.media_type == "image"
    assert p.hashtags == []
    assert p.media_urls == []
    assert p.location_name is None


def test_comment_construction() -> None:
    c = Comment(
        pk="c1",
        media_pk="m1",
        user_pk="u1",
        user_username="bob",
        text="nice",
        created_at=1700000000,
    )
    assert c.like_count == 0
    assert c.reply_to_pk is None


def test_story_and_highlight_construction() -> None:
    s = Story(
        pk="s1",
        taken_at=1700000000,
        expires_at=1700086400,
        media_type="video",
        media_url="https://example/x.mp4",
    )
    assert s.thumbnail_url is None

    h = Highlight(pk="h1", title="trips")
    assert h.item_count == 0

    item = HighlightItem(
        pk="hi1",
        highlight_pk="h1",
        taken_at=1700000000,
        media_type="image",
        media_url="https://example/x.jpg",
    )
    assert item.thumbnail_url is None


def test_user_construction() -> None:
    u = User(pk="u1", username="bob")
    assert u.full_name == ""
    assert u.is_private is False
    assert u.is_verified is False


def _make_sample(cls: type) -> object:
    """Build a minimum-viable instance of any DTO for slot-attribute checks."""
    if cls is Profile:
        return Profile(pk="1", username="a", access="public")
    if cls is User:
        return User(pk="1", username="a")
    if cls is Post:
        return Post(pk="1", code="A", taken_at=0, media_type="image")
    if cls is Comment:
        return Comment(pk="1", media_pk="m", user_pk="u", user_username="x", text="t",
                       created_at=0)
    if cls is Story:
        return Story(pk="1", taken_at=0, expires_at=1, media_type="image", media_url="u")
    if cls is Highlight:
        return Highlight(pk="1", title="t")
    if cls is HighlightItem:
        return HighlightItem(pk="1", highlight_pk="h", taken_at=0, media_type="image",
                             media_url="u")
    if cls is Quota:
        return Quota(remaining=None)
    if cls is WatchSpec:
        return WatchSpec(user="a", interval_seconds=600)
    if cls is Snapshot:
        return Snapshot(target_pk="1", captured_at=0)
    raise AssertionError(f"no sample factory for {cls!r}")
