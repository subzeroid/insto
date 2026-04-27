"""Tests for `insto.service.analytics`.

Covers each top-N extractor over a bounded window, the mutuals intersection,
the `/wcommented` and `/wtagged` counters, and the `aggregate_likes` summary.
Empty inputs are exercised on every function — they must surface
`empty=True` and an empty `items`/`top_posts` list so the command layer can
print an explicit "no data to analyze" rather than a silent empty table.
"""

from __future__ import annotations

import pytest

from insto.models import Comment, Post, User
from insto.service.analytics import (
    LikesStats,
    MutualsResult,
    TopList,
    aggregate_likes,
    compute_mutuals,
    count_wcommented,
    count_wtagged,
    extract_hashtags,
    extract_locations,
    extract_mentions,
)


def _post(
    pk: str = "1",
    code: str | None = None,
    *,
    hashtags: list[str] | None = None,
    mentions: list[str] | None = None,
    location_name: str | None = None,
    like_count: int = 0,
    owner_username: str | None = None,
) -> Post:
    return Post(
        pk=pk,
        code=code or f"C{pk}",
        taken_at=1700000000,
        media_type="image",
        like_count=like_count,
        hashtags=list(hashtags or []),
        mentions=list(mentions or []),
        location_name=location_name,
        owner_username=owner_username,
    )


def _comment(user: str, pk: str = "1") -> Comment:
    return Comment(
        pk=pk,
        media_pk="m1",
        user_pk=f"u-{user}",
        user_username=user,
        text="hi",
        created_at=1700000000,
    )


def _user(username: str, pk: str | None = None) -> User:
    return User(pk=pk or f"u-{username}", username=username)


def test_extract_hashtags_top_and_window() -> None:
    posts = [
        _post("1", hashtags=["#python", "Coding"]),
        _post("2", hashtags=["python", "#osint"]),
        _post("3", hashtags=["OSINT", "Coding"]),
    ]
    res = extract_hashtags(posts, target="alice", limit=50, top=10)
    assert isinstance(res, TopList)
    assert res.kind == "hashtags"
    assert res.window == 50
    assert res.analyzed == 3
    assert res.empty is False
    items_dict = dict(res.items)
    assert items_dict["python"] == 2
    assert items_dict["coding"] == 2
    assert items_dict["osint"] == 2
    keys = [k for k, _ in res.items]
    assert keys == sorted(keys, key=lambda k: (-items_dict[k], k))


def test_extract_hashtags_window_caps_input() -> None:
    posts = [_post(str(i), hashtags=[f"tag{i}"]) for i in range(20)]
    res = extract_hashtags(posts, target="alice", limit=5)
    assert res.window == 5
    assert res.analyzed == 5
    assert {k for k, _ in res.items} == {f"tag{i}" for i in range(5)}


def test_extract_mentions_lowercases_and_strips_at() -> None:
    posts = [
        _post("1", mentions=["@Bob", "carol"]),
        _post("2", mentions=["bob", "@DAVE"]),
    ]
    res = extract_mentions(posts, target="alice")
    counts = dict(res.items)
    assert counts == {"bob": 2, "carol": 1, "dave": 1}
    assert res.kind == "mentions"


def test_extract_locations_skips_empty_and_keeps_window() -> None:
    posts = [
        _post("1", location_name="Berlin"),
        _post("2", location_name=None),
        _post("3", location_name="Berlin"),
        _post("4", location_name="  "),
        _post("5", location_name="Tbilisi"),
    ]
    res = extract_locations(posts, target="alice", limit=50)
    assert res.analyzed == 5
    assert dict(res.items) == {"Berlin": 2, "Tbilisi": 1}
    assert res.empty is False


def test_window_header_carries_target_and_window() -> None:
    res = extract_hashtags(
        [_post("1", hashtags=["x"])], target="alice", limit=50
    )
    header = f"Hashtags from @{res.target} (last {res.window} posts):"
    assert header == "Hashtags from @alice (last 50 posts):"


def test_empty_input_marks_empty_true() -> None:
    res = extract_hashtags([], target="alice", limit=50)
    assert res.empty is True
    assert res.analyzed == 0
    assert res.items == []
    assert extract_mentions([], target="alice").empty is True
    assert extract_locations([], target="alice").empty is True
    assert count_wcommented([], target="alice").empty is True
    assert count_wtagged([], target="alice").empty is True


def test_invalid_limit_raises() -> None:
    with pytest.raises(ValueError):
        extract_hashtags([], target="alice", limit=0)
    with pytest.raises(ValueError):
        extract_mentions([], target="alice", limit=-1)
    with pytest.raises(ValueError):
        compute_mutuals([], [], target="alice", follower_limit=0)
    with pytest.raises(ValueError):
        compute_mutuals([], [], target="alice", following_limit=-3)


def test_compute_mutuals_intersection_sorted() -> None:
    followers = [_user("bob"), _user("carol"), _user("dave"), _user("eve")]
    following = [_user("dave"), _user("Bob"), _user("frank")]
    res = compute_mutuals(
        followers, following, target="alice",
    )
    assert isinstance(res, MutualsResult)
    usernames = [u.username for u in res.items]
    assert usernames == ["dave"]
    bob_user = User(pk="u-bob", username="bob")
    res2 = compute_mutuals(
        [bob_user, _user("carol")],
        [bob_user, _user("dave")],
        target="alice",
    )
    assert [u.username for u in res2.items] == ["bob"]


def test_compute_mutuals_dedup_by_pk() -> None:
    bob = _user("bob")
    bob_dup = User(pk="u-bob", username="bob")
    followers = [bob, bob_dup]
    following = [bob]
    res = compute_mutuals(followers, following, target="alice")
    assert len(res.items) == 1


def test_compute_mutuals_window_caps_each_side() -> None:
    followers = [_user(f"u{i}", pk=f"p{i}") for i in range(2000)]
    following = [_user(f"u{i}", pk=f"p{i}") for i in range(2000)]
    res = compute_mutuals(
        followers, following, target="alice",
        follower_limit=100, following_limit=50,
    )
    assert res.follower_analyzed == 100
    assert res.following_analyzed == 50
    assert len(res.items) == 50


def test_compute_mutuals_empty() -> None:
    res = compute_mutuals([], [_user("bob")], target="alice")
    assert res.empty is True
    assert res.items == []
    res2 = compute_mutuals([_user("bob")], [], target="alice")
    assert res2.empty is True


def test_count_wcommented_aggregates_repeats() -> None:
    comments = [
        _comment("bob", "1"),
        _comment("bob", "2"),
        _comment("carol", "3"),
        _comment("dave", "4"),
        _comment("bob", "5"),
    ]
    res = count_wcommented(comments, target="alice", limit=50)
    assert dict(res.items) == {"bob": 3, "carol": 1, "dave": 1}
    assert res.items[0] == ("bob", 3)


def test_count_wtagged_groups_by_owner() -> None:
    tagged = [
        _post("1", owner_username="bob"),
        _post("2", owner_username="carol"),
        _post("3", owner_username="bob"),
        _post("4", owner_username=None),
        _post("5", owner_username="bob"),
    ]
    res = count_wtagged(tagged, target="alice", limit=50)
    assert dict(res.items) == {"bob": 3, "carol": 1}
    assert res.items[0] == ("bob", 3)


def test_aggregate_likes_total_avg_top() -> None:
    posts = [
        _post("1", code="A", like_count=100),
        _post("2", code="B", like_count=50),
        _post("3", code="C", like_count=200),
    ]
    res = aggregate_likes(posts, target="alice", limit=50, top=2)
    assert isinstance(res, LikesStats)
    assert res.total_likes == 350
    assert res.avg_likes == pytest.approx(350 / 3)
    assert res.top_posts == [("C", 200), ("A", 100)]
    assert res.empty is False


def test_aggregate_likes_empty() -> None:
    res = aggregate_likes([], target="alice", limit=50)
    assert res.empty is True
    assert res.total_likes == 0
    assert res.avg_likes == 0.0
    assert res.top_posts == []


def test_aggregate_likes_window_caps() -> None:
    posts = [_post(str(i), like_count=i) for i in range(100)]
    res = aggregate_likes(posts, target="alice", limit=10, top=3)
    assert res.analyzed == 10
    assert res.window == 10
    assert res.total_likes == sum(range(10))
    assert res.top_posts[0] == ("C9", 9)


def test_top_none_returns_all_items() -> None:
    posts = [_post(str(i), hashtags=[f"t{i}"]) for i in range(15)]
    res = extract_hashtags(posts, target="alice", limit=50, top=None)
    assert len(res.items) == 15


def test_hashtag_input_strips_hashes_and_blanks() -> None:
    posts = [
        _post("1", hashtags=["##doublehash", "  ", ""]),
        _post("2", hashtags=["#doublehash"]),
    ]
    res = extract_hashtags(posts, target="alice")
    assert dict(res.items) == {"doublehash": 2}
