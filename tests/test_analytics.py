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
    FanRow,
    LikesStats,
    MutualsResult,
    TopList,
    aggregate_likes,
    compute_mutuals,
    count_fans,
    count_wcommented,
    count_wliked,
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
    location_pk: str | None = None,
    location_lat: float | None = None,
    location_lng: float | None = None,
    like_count: int = 0,
    owner_username: str | None = None,
    taken_at: int = 1700000000,
) -> Post:
    return Post(
        pk=pk,
        code=code or f"C{pk}",
        taken_at=taken_at,
        media_type="image",
        like_count=like_count,
        hashtags=list(hashtags or []),
        mentions=list(mentions or []),
        location_name=location_name,
        location_pk=location_pk,
        location_lat=location_lat,
        location_lng=location_lng,
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
    res = extract_hashtags([_post("1", hashtags=["x"])], target="alice", limit=50)
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
        followers,
        following,
        target="alice",
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
        followers,
        following,
        target="alice",
        follower_limit=100,
        following_limit=50,
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


def test_compute_timeline_buckets_by_hour_and_weekday() -> None:
    """Bucket counts come out as expected for known UTC timestamps."""
    from insto.service.analytics import compute_timeline

    # 2025-01-13 (Monday) 14:00 UTC, 15:00 UTC, 16:00 UTC
    # 2025-01-15 (Wednesday) 14:00 UTC
    posts = [
        _post("a", taken_at=1736776800),  # Mon 14:00 UTC
        _post("b", taken_at=1736780400),  # Mon 15:00 UTC
        _post("c", taken_at=1736784000),  # Mon 16:00 UTC
        _post("d", taken_at=1736949600),  # Wed 14:00 UTC
    ]
    res = compute_timeline(posts, target="x", limit=50)
    assert res.analyzed == 4
    assert res.hour_of_day[14] == 2
    assert res.hour_of_day[15] == 1
    assert res.hour_of_day[16] == 1
    # Mon=0, Wed=2
    assert res.day_of_week[0] == 3
    assert res.day_of_week[2] == 1
    assert res.empty is False
    assert res.first_post_ts == 1736776800
    assert res.last_post_ts == 1736949600


def test_compute_timeline_skips_zero_or_negative_timestamps() -> None:
    """Defensive: posts with `taken_at = 0` (legacy fixture data) are dropped
    instead of contributing to the unix-epoch bucket."""
    from insto.service.analytics import compute_timeline

    posts = [_post("a", taken_at=0), _post("b", taken_at=-1), _post("c", taken_at=1736776800)]
    res = compute_timeline(posts, target="x", limit=50)
    assert res.analyzed == 3  # window respects all three (analyzed = inspected)
    assert sum(res.hour_of_day) == 1  # but only the valid one bucketed
    assert sum(res.day_of_week) == 1


def test_compute_timeline_empty() -> None:
    from insto.service.analytics import compute_timeline

    res = compute_timeline([], target="x", limit=50)
    assert res.empty is True
    assert sum(res.hour_of_day) == 0
    assert res.first_post_ts is None


def test_compute_intersection_returns_users_in_both_lists() -> None:
    """Cross-target overlap: users appearing in both follower windows."""
    from insto.service.analytics import compute_intersection

    a = [_user("alice"), _user("bob"), _user("carol")]
    b = [_user("bob"), _user("carol"), _user("dave")]
    res = compute_intersection(a, b, target_a="ferrari", target_b="mclaren")
    names = [u.username for u in res.items]
    assert names == ["bob", "carol"]
    assert res.target_a == "ferrari"
    assert res.target_b == "mclaren"
    assert res.empty is False


def test_compute_intersection_dedup_by_pk() -> None:
    """Same pk in side_a twice (or via duplicate source) is collapsed."""
    from insto.service.analytics import compute_intersection

    bob = _user("bob")
    a = [bob, bob, _user("carol")]
    b = [bob]
    res = compute_intersection(a, b, target_a="x", target_b="y")
    assert len(res.items) == 1
    assert res.items[0].username == "bob"


def test_compute_intersection_empty_when_either_side_blank() -> None:
    from insto.service.analytics import compute_intersection

    res = compute_intersection([], [_user("bob")], target_a="x", target_b="y")
    assert res.empty is True
    assert res.items == []


def test_compute_intersection_window_caps_each_side() -> None:
    from insto.service.analytics import compute_intersection

    big = [_user(f"u{i}", pk=f"p{i}") for i in range(2000)]
    res = compute_intersection(big, big, target_a="x", target_b="y", window=50)
    assert res.a_analyzed == 50
    assert res.b_analyzed == 50
    # Both windows are the first 50 users — full overlap.
    assert len(res.items) == 50


def test_count_wliked_aggregates_repeats() -> None:
    """Same user liking M of N inspected posts contributes M to its count."""
    likers = [
        _user("bob"),
        _user("bob"),
        _user("carol"),
        _user("dave"),
        _user("bob"),
    ]
    res = count_wliked(likers, target="alice", limit=50)
    assert dict(res.items) == {"bob": 3, "carol": 1, "dave": 1}
    assert res.items[0] == ("bob", 3)
    assert res.kind == "wliked"


def test_count_wliked_skips_blank_usernames() -> None:
    """Empty / whitespace-only usernames must not pollute the counter."""
    likers = [_user("bob"), _user(""), _user("   "), _user("bob")]
    res = count_wliked(likers, target="alice", limit=50)
    assert dict(res.items) == {"bob": 2}


def test_count_wliked_empty_input() -> None:
    res = count_wliked([], target="alice", limit=50)
    assert res.empty is True
    assert res.items == []


def test_count_fans_combines_likes_and_comments_with_weight() -> None:
    """score = likes + 3 * comments by default; ties broken by username asc."""
    likers = [
        _user("bob"),  # bob: 2L
        _user("bob"),
        _user("carol"),  # carol: 1L
        _user("dave"),  # dave: 1L
    ]
    comments = [
        _comment("bob"),  # bob: 1C → score = 2 + 3*1 = 5
        _comment("eve"),  # eve: 1C → score = 0 + 3*1 = 3
    ]
    res = count_fans(
        likers,
        comments,
        target="alice",
        limit=50,
        analyzed_posts=10,
    )
    by_user = {row.username: row for row in res.items}
    assert by_user["bob"].score == 5
    assert by_user["bob"].likes == 2 and by_user["bob"].comments == 1
    assert by_user["eve"].score == 3
    assert by_user["eve"].likes == 0 and by_user["eve"].comments == 1
    # Bob is top of the ranking (score 5 > everyone else)
    assert res.items[0].username == "bob"
    assert res.window == 50
    assert res.analyzed_posts == 10
    assert res.comment_weight == 3


def test_count_fans_custom_weight() -> None:
    """Setting comment_weight=1 makes likes and comments equal-weighted."""
    likers = [_user("bob")]  # bob: 1L
    comments = [_comment("carol")]  # carol: 1C
    res = count_fans(likers, comments, target="alice", limit=50, analyzed_posts=1, comment_weight=1)
    by_user = {row.username: row.score for row in res.items}
    assert by_user["bob"] == 1
    assert by_user["carol"] == 1


def test_count_fans_negative_weight_rejected() -> None:
    with pytest.raises(ValueError, match="comment_weight"):
        count_fans([], [], target="alice", limit=50, analyzed_posts=0, comment_weight=-1)


def test_count_fans_top_caps_results() -> None:
    likers = [_user(f"u{i}") for i in range(50)]
    res = count_fans(likers, [], target="alice", limit=50, analyzed_posts=50, top=5)
    assert len(res.items) == 5
    assert all(isinstance(r, FanRow) for r in res.items)


def test_count_fans_empty_when_no_posts_analyzed() -> None:
    res = count_fans([], [], target="alice", limit=50, analyzed_posts=0)
    assert res.empty is True
    assert res.items == []


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


# ---------------------------------------------------------------------------
# /where — geo fingerprint
# ---------------------------------------------------------------------------


def test_compute_geo_fingerprint_anchor_centroid_radius() -> None:
    """Three Maranello posts + one Niseko: anchor = Maranello, radius
    spans the Maranello -> Niseko great-circle (~9000+ km)."""
    from insto.service.analytics import compute_geo_fingerprint

    posts = [
        _post("1", location_pk="223393054", location_name="Maranello",
              location_lat=44.5256, location_lng=10.8664),
        _post("2", location_pk="223393054", location_name="Maranello",
              location_lat=44.5256, location_lng=10.8664),
        _post("3", location_pk="223393054", location_name="Maranello",
              location_lat=44.5256, location_lng=10.8664),
        _post("4", location_pk="206404230", location_name="Niseko, Japan",
              location_lat=42.8591, location_lng=140.7053),
    ]
    res = compute_geo_fingerprint(posts, target="ferrari", limit=50)
    assert res.geotagged == 4
    assert res.analyzed == 4
    assert res.anchor is not None
    assert res.anchor.name == "Maranello"
    assert res.anchor.count == 3
    # Centroid is biased toward the Maranello cluster (3-of-4 weight).
    assert res.centroid_lng is not None and 30 < res.centroid_lng < 60
    # Radius spans the Niseko outlier — must be at least 7000 km.
    assert res.radius_km is not None and res.radius_km > 7000


def test_compute_geo_fingerprint_skips_posts_without_gps() -> None:
    """Posts with `location_name` but no lat/lng don't contribute."""
    from insto.service.analytics import compute_geo_fingerprint

    posts = [
        _post("1", location_name="Mystery Place"),  # no lat/lng
        _post("2", location_pk="X", location_name="Real",
              location_lat=10.0, location_lng=20.0),
    ]
    res = compute_geo_fingerprint(posts, target="x", limit=50)
    assert res.analyzed == 2
    assert res.geotagged == 1
    assert res.anchor is not None and res.anchor.name == "Real"


def test_compute_geo_fingerprint_empty() -> None:
    """No geotags at all — empty=True only when the window itself is empty."""
    from insto.service.analytics import compute_geo_fingerprint

    res = compute_geo_fingerprint([], target="x", limit=50)
    assert res.empty is True
    assert res.anchor is None
    res2 = compute_geo_fingerprint(
        [_post("1", location_name="No GPS")], target="x", limit=50
    )
    # Window non-empty but no GPS points — empty stays False (we *did*
    # analyse posts), but anchor / centroid are None.
    assert res2.empty is False
    assert res2.geotagged == 0
    assert res2.anchor is None


def test_compute_geo_fingerprint_top_caps_places() -> None:
    """`top=N` caps the rendered places list to N entries."""
    from insto.service.analytics import compute_geo_fingerprint

    posts = [
        _post(
            str(i),
            location_pk=f"loc{i}",
            location_name=f"Place {i}",
            location_lat=10.0 + i,
            location_lng=20.0 + i,
        )
        for i in range(15)
    ]
    res = compute_geo_fingerprint(posts, target="x", limit=50, top=5)
    assert len(res.places) == 5
    # Anchor count is 1 (every place appears once); the top-5 ordering is
    # stable but not meaningful, just don't overflow.
    assert res.geotagged == 15
