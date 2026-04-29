"""Aggregation helpers over bounded windows of `Post` / `Comment` / `User`.

Every function in this module is pure: it takes already-fetched DTOs, applies
a bounded window, and returns a small dataclass that carries enough header
context for the command layer to print human output without re-deriving
state. Two contracts are honored everywhere:

- **bounded window** — every function accepts `limit: int = 50`, which caps
  how many input items are inspected. Callers that want a different window
  pass an explicit `limit`. Negative or zero `limit` raises `ValueError`.
- **explicit emptiness** — when the input window contains zero items, the
  result carries `empty=True` and an empty `items` list. The command layer
  uses that flag to print `no posts to analyze for @<user>` instead of
  rendering a silent empty table.

Functions never call the network. They never log or print. They are the
analytical core that powers `/locations`, `/hashtags`, `/mentions`,
`/mutuals`, `/wcommented`, `/wtagged`, and `/likes`.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TypeVar

from insto.models import Comment, Post, User

_T = TypeVar("_T")


def _check_limit(limit: int) -> None:
    if limit <= 0:
        raise ValueError(f"limit must be positive, got {limit}")


def _take(items: Iterable[_T], limit: int) -> list[_T]:
    """Return at most `limit` items from `items` as a list."""
    out: list[_T] = []
    for i, item in enumerate(items):
        if i >= limit:
            break
        out.append(item)
    return out


@dataclass(slots=True)
class TopList:
    """Counted items from a bounded window, ordered by count desc.

    `kind` names what is being counted (`"hashtags"`, `"locations"`,
    `"mentions"`, `"wcommented"`, `"wtagged"`). `window` is the requested
    cap; `analyzed` is how many input items were actually inspected
    (may be less than `window` if input was smaller). `items` is sorted
    by count desc; ties are broken by key asc for stable output.
    """

    target: str
    kind: str
    window: int
    analyzed: int
    items: list[tuple[str, int]] = field(default_factory=list)
    empty: bool = False


@dataclass(slots=True)
class MutualsResult:
    """Intersection of `followers` and `following` for a target.

    `follower_window` and `following_window` mirror the caps applied to
    each side; the intersection itself can be at most `min(follower_window,
    following_window)` users.
    """

    target: str
    follower_window: int
    following_window: int
    follower_analyzed: int
    following_analyzed: int
    items: list[User] = field(default_factory=list)
    empty: bool = False


@dataclass(slots=True, frozen=True)
class GeoPlace:
    """One row in :class:`GeoFingerprintResult.places`."""

    name: str
    pk: str | None
    lat: float
    lng: float
    count: int


@dataclass(slots=True)
class GeoFingerprintResult:
    """Where-does-this-target-live summary over a bounded post window.

    The output answers two questions an OSINT operator usually has:

    1. **Where's the anchor?** — the place the target geotags most
       often. Strong proxy for "where they live or work".
    2. **How wide is the spread?** — centroid + max radius from
       centroid. A 50 km radius means metro-area-bound; 5000 km
       means international travel.

    ``analyzed`` is the total post window inspected (capped at the
    requested ``window``); ``geotagged`` is how many of those carried
    GPS coordinates. Both are surfaced so the renderer can show the
    confidence ratio ("38 of 50 posts geotagged").
    """

    target: str
    window: int
    analyzed: int
    geotagged: int
    anchor: GeoPlace | None = None
    centroid_lat: float | None = None
    centroid_lng: float | None = None
    radius_km: float | None = None
    places: list[GeoPlace] = field(default_factory=list)
    empty: bool = False


@dataclass(slots=True)
class TimelineResult:
    """Posting cadence histogram across a bounded post window.

    Two complementary histograms over the same posts:

    - ``hour_of_day``: 24-bucket UTC histogram. The shape often
      reveals the target's timezone (sleeping hours = empty buckets)
      and whether posting is human-paced or scheduler-driven.
    - ``day_of_week``: 7-bucket histogram (Monday=0, Sunday=6). Shape
      reveals weekday vs weekend rhythm.

    ``first_post_ts`` / ``last_post_ts`` are unix seconds — the
    renderer prints them as ISO so the operator sees the actual span
    the histogram is summarising.
    """

    target: str
    window: int
    analyzed: int
    hour_of_day: list[int] = field(default_factory=lambda: [0] * 24)
    day_of_week: list[int] = field(default_factory=lambda: [0] * 7)
    first_post_ts: int | None = None
    last_post_ts: int | None = None
    empty: bool = False


@dataclass(slots=True)
class IntersectionResult:
    """Cross-target intersection — users that follow *both* @a and @b.

    Different from :class:`MutualsResult`: mutuals is one target's
    followers ∩ following (an internal property). Intersection is two
    different targets' follower lists. The OSINT signal is "shared
    audience" — overlap reveals shared communities, staff, family,
    PR networks. Both follower windows are capped at fetch time;
    ``empty`` is True when either side analyzed zero followers.
    """

    target_a: str
    target_b: str
    window: int
    a_analyzed: int
    b_analyzed: int
    items: list[User] = field(default_factory=list)
    empty: bool = False


@dataclass(slots=True)
class LikesStats:
    """Aggregate `like_count` stats over a bounded window of posts.

    `total_likes` is the sum across analyzed posts; `avg_likes` is the
    mean (0.0 when `analyzed == 0`). `top_posts` is `(post_code, likes)`
    pairs for the most-liked posts in the window, ordered desc.
    """

    target: str
    window: int
    analyzed: int
    total_likes: int = 0
    avg_likes: float = 0.0
    top_posts: list[tuple[str, int]] = field(default_factory=list)
    empty: bool = False


def _top_from_counter(counter: Counter[str], top: int | None) -> list[tuple[str, int]]:
    """Sort `counter` by (count desc, key asc) and take `top` if given."""
    ordered = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    if top is not None and top > 0:
        ordered = ordered[:top]
    return ordered


def extract_hashtags(
    posts: Iterable[Post],
    *,
    target: str,
    limit: int = 50,
    top: int | None = 20,
) -> TopList:
    """Count hashtags across the first `limit` posts (case-insensitive)."""
    _check_limit(limit)
    window = _take(posts, limit)
    counter: Counter[str] = Counter()
    for post in window:
        for tag in post.hashtags:
            cleaned = tag.lstrip("#").strip().lower()
            if cleaned:
                counter[cleaned] += 1
    return TopList(
        target=target,
        kind="hashtags",
        window=limit,
        analyzed=len(window),
        items=_top_from_counter(counter, top),
        empty=len(window) == 0,
    )


def extract_mentions(
    posts: Iterable[Post],
    *,
    target: str,
    limit: int = 50,
    top: int | None = 20,
) -> TopList:
    """Count @mentions across the first `limit` posts (case-insensitive)."""
    _check_limit(limit)
    window = _take(posts, limit)
    counter: Counter[str] = Counter()
    for post in window:
        for mention in post.mentions:
            cleaned = mention.lstrip("@").strip().lower()
            if cleaned:
                counter[cleaned] += 1
    return TopList(
        target=target,
        kind="mentions",
        window=limit,
        analyzed=len(window),
        items=_top_from_counter(counter, top),
        empty=len(window) == 0,
    )


def extract_locations(
    posts: Iterable[Post],
    *,
    target: str,
    limit: int = 50,
    top: int | None = 20,
) -> TopList:
    """Count location names across the first `limit` posts.

    Posts without a location are skipped; they do not pollute the top with
    a synthetic "(no location)" bucket but they still consume a slot of the
    window (so `analyzed` reflects the post count, not the geo-tagged subset).
    """
    _check_limit(limit)
    window = _take(posts, limit)
    counter: Counter[str] = Counter()
    for post in window:
        name = post.location_name
        if name and name.strip():
            counter[name.strip()] += 1
    return TopList(
        target=target,
        kind="locations",
        window=limit,
        analyzed=len(window),
        items=_top_from_counter(counter, top),
        empty=len(window) == 0,
    )


def compute_mutuals(
    followers: Iterable[User],
    following: Iterable[User],
    *,
    target: str,
    follower_limit: int = 1000,
    following_limit: int = 1000,
) -> MutualsResult:
    """Return users that appear in both bounded follower / following windows.

    The defaults mirror the `/mutuals` safety cap (1000 each side) so that
    a fresh `compute_mutuals(...)` cannot blow up on a 1M-follower target.
    Output is ordered by username asc for stable rendering and dedup'd by
    `pk`.
    """
    if follower_limit <= 0:
        raise ValueError(f"follower_limit must be positive, got {follower_limit}")
    if following_limit <= 0:
        raise ValueError(f"following_limit must be positive, got {following_limit}")

    foll_window: list[User] = []
    for i, user in enumerate(followers):
        if i >= follower_limit:
            break
        foll_window.append(user)

    by_pk: dict[str, User] = {}
    for i, user in enumerate(following):
        if i >= following_limit:
            break
        by_pk[user.pk] = user
    following_analyzed = len(by_pk)

    seen: set[str] = set()
    intersection: list[User] = []
    for user in foll_window:
        if user.pk in by_pk and user.pk not in seen:
            seen.add(user.pk)
            intersection.append(user)

    intersection.sort(key=lambda u: (u.username.lower(), u.pk))
    empty = len(foll_window) == 0 or following_analyzed == 0
    return MutualsResult(
        target=target,
        follower_window=follower_limit,
        following_window=following_limit,
        follower_analyzed=len(foll_window),
        following_analyzed=following_analyzed,
        items=intersection,
        empty=empty,
    )


def count_wcommented(
    comments: Iterable[Comment],
    *,
    target: str,
    limit: int = 50,
    top: int | None = 20,
) -> TopList:
    """Count distinct commenters across `comments`.

    Maps `count[user_username] += 1` for each comment. If the same user
    comments multiple times, they accumulate. The caller is expected to
    pre-merge comments across the last `limit` posts of the target;
    `limit` here is only the post-window label surfaced via `window`.
    """
    _check_limit(limit)
    materialised = list(comments)
    counter: Counter[str] = Counter()
    for comment in materialised:
        username = comment.user_username.strip()
        if username:
            counter[username] += 1
    return TopList(
        target=target,
        kind="wcommented",
        window=limit,
        analyzed=len(materialised),
        items=_top_from_counter(counter, top),
        empty=len(materialised) == 0,
    )


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance between two GPS points in kilometres.

    Standard haversine formula on a 6371 km sphere — accurate to <1%
    over typical IG geotag spreads. Good enough for "metro area vs
    cross-country" classification; not a survey-grade tool.
    """
    import math

    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def compute_geo_fingerprint(
    posts: Iterable[Post],
    *,
    target: str,
    limit: int = 50,
    top: int | None = 10,
) -> GeoFingerprintResult:
    """Summarise posting locations into anchor + centroid + top-N places.

    Posts without GPS (``location_lat is None``) are counted toward
    ``analyzed`` but not ``geotagged`` — they don't contribute to the
    geometry. Two passes:

    1. Bucket by ``location_pk`` (or ``location_name`` if pk is
       missing) to count occurrences and remember the place metadata.
    2. Compute the arithmetic mean of (lat, lng) as the centroid and
       the max haversine distance from centroid as the spread radius.

    Arithmetic-mean centroid is wrong for points spanning the date
    line or the poles, but right for the 99% of OSINT cases where the
    target stays within one or two continents. The radius makes any
    such pathology visible (an anomalous 19000 km radius signals the
    centroid is meaningless).
    """
    _check_limit(limit)
    window = _take(posts, limit)
    counter: Counter[str] = Counter()
    place_meta: dict[str, GeoPlace] = {}
    geotagged_count = 0
    total_lat = 0.0
    total_lng = 0.0
    geotag_points: list[tuple[float, float]] = []

    for post in window:
        if post.location_lat is None or post.location_lng is None:
            continue
        # Prefer pk as the bucket key so different posts at the
        # exact same place coalesce. Fall through to name when pk
        # is absent (older posts, IG quirks).
        key = post.location_pk or post.location_name or ""
        if not key:
            continue
        counter[key] += 1
        if key not in place_meta:
            place_meta[key] = GeoPlace(
                name=post.location_name or "",
                pk=post.location_pk,
                lat=post.location_lat,
                lng=post.location_lng,
                count=0,
            )
        geotagged_count += 1
        total_lat += post.location_lat
        total_lng += post.location_lng
        geotag_points.append((post.location_lat, post.location_lng))

    if geotagged_count == 0:
        return GeoFingerprintResult(
            target=target,
            window=limit,
            analyzed=len(window),
            geotagged=0,
            empty=len(window) == 0,
        )

    # Materialise ordered places list with final counts.
    places = [
        GeoPlace(
            name=place_meta[key].name,
            pk=place_meta[key].pk,
            lat=place_meta[key].lat,
            lng=place_meta[key].lng,
            count=count,
        )
        for key, count in counter.most_common(top if top else None)
    ]
    anchor = places[0] if places else None

    centroid_lat = total_lat / geotagged_count
    centroid_lng = total_lng / geotagged_count
    radius_km = max(
        _haversine_km(centroid_lat, centroid_lng, lat, lng) for lat, lng in geotag_points
    )

    return GeoFingerprintResult(
        target=target,
        window=limit,
        analyzed=len(window),
        geotagged=geotagged_count,
        anchor=anchor,
        centroid_lat=centroid_lat,
        centroid_lng=centroid_lng,
        radius_km=radius_km,
        places=places,
        empty=False,
    )


def compute_timeline(
    posts: Iterable[Post],
    *,
    target: str,
    limit: int = 50,
) -> TimelineResult:
    """Bucket post timestamps into hour-of-day + day-of-week histograms.

    Uses UTC. Posts with non-positive ``taken_at`` are skipped silently
    (defensive: some legacy fixture posts have 0 / negative timestamps
    that would fall back to the unix epoch and skew the histogram).
    """
    from datetime import UTC, datetime

    _check_limit(limit)
    window = _take(posts, limit)
    hours = [0] * 24
    weekdays = [0] * 7
    timestamps: list[int] = []
    for post in window:
        ts = post.taken_at
        if ts is None or ts <= 0:
            continue
        dt = datetime.fromtimestamp(ts, tz=UTC)
        hours[dt.hour] += 1
        weekdays[dt.weekday()] += 1
        timestamps.append(ts)
    return TimelineResult(
        target=target,
        window=limit,
        analyzed=len(window),
        hour_of_day=hours,
        day_of_week=weekdays,
        first_post_ts=min(timestamps) if timestamps else None,
        last_post_ts=max(timestamps) if timestamps else None,
        empty=not timestamps,
    )


def compute_intersection(
    side_a: Iterable[User],
    side_b: Iterable[User],
    *,
    target_a: str,
    target_b: str,
    window: int = 1000,
) -> IntersectionResult:
    """Return users who appear in *both* bounded follower windows.

    ``window`` caps each side identically (defaults to the same 1000
    that :func:`compute_mutuals` uses). Output is dedup'd by ``pk`` and
    sorted by ``username`` asc for stable rendering. Empty when either
    side analyzed zero followers.
    """
    if window <= 0:
        raise ValueError(f"window must be positive, got {window}")

    a_window: list[User] = []
    for i, user in enumerate(side_a):
        if i >= window:
            break
        a_window.append(user)

    by_pk_b: dict[str, User] = {}
    for i, user in enumerate(side_b):
        if i >= window:
            break
        by_pk_b[user.pk] = user
    b_analyzed = len(by_pk_b)

    seen: set[str] = set()
    intersection: list[User] = []
    for user in a_window:
        if user.pk in by_pk_b and user.pk not in seen:
            seen.add(user.pk)
            # Prefer the b-side record (richer fields when sources disagree).
            intersection.append(by_pk_b[user.pk])

    intersection.sort(key=lambda u: (u.username.lower(), u.pk))
    empty = len(a_window) == 0 or b_analyzed == 0
    return IntersectionResult(
        target_a=target_a,
        target_b=target_b,
        window=window,
        a_analyzed=len(a_window),
        b_analyzed=b_analyzed,
        items=intersection,
        empty=empty,
    )


def count_wliked(
    likers: Iterable[User],
    *,
    target: str,
    limit: int = 50,
    top: int | None = 20,
) -> TopList:
    """Count distinct likers across `likers` (likers list of N posts merged).

    Symmetric to :func:`count_wcommented`: maps
    ``count[user.username] += 1`` for each occurrence. The same user
    liking M of the inspected N posts contributes M to its count, which
    is exactly the "superfan" signal we want — recurring engagement
    across a recent posting window.

    The caller pre-merges likers across the last ``limit`` posts;
    ``limit`` here is only the post-window label surfaced via
    ``window``.
    """
    _check_limit(limit)
    materialised = list(likers)
    counter: Counter[str] = Counter()
    for user in materialised:
        username = (user.username or "").strip()
        if username:
            counter[username] += 1
    return TopList(
        target=target,
        kind="wliked",
        window=limit,
        analyzed=len(materialised),
        items=_top_from_counter(counter, top),
        empty=len(materialised) == 0,
    )


@dataclass(slots=True, frozen=True)
class FanRow:
    """One row in :class:`FansResult.items`."""

    username: str
    likes: int
    comments: int
    score: int


@dataclass(slots=True)
class FansResult:
    """Per-user engagement breakdown across a bounded post window.

    A composite of :func:`count_wliked` and :func:`count_wcommented` —
    ranks users by a weighted ``score = likes + comment_weight *
    comments``. The default weight (3) reflects that a comment is a
    higher-effort signal than a like.

    ``items`` is sorted by ``score`` desc with ties broken by
    ``username`` asc for stable output. Each entry exposes its
    breakdown so the renderer (and the Maltego export) can carry both
    the headline score and the per-channel counts.
    """

    target: str
    window: int
    analyzed_posts: int
    comment_weight: int
    items: list[FanRow] = field(default_factory=list)
    empty: bool = False


def count_fans(
    likers: Iterable[User],
    comments: Iterable[Comment],
    *,
    target: str,
    limit: int,
    analyzed_posts: int,
    comment_weight: int = 3,
    top: int | None = 20,
) -> FansResult:
    """Aggregate likers + commenters into a single ranked "fans" list.

    ``score = likes + comment_weight * comments``. Default weight 3
    matches the rough engagement-effort ratio (writing a comment costs
    ~3x more attention than tapping a heart).

    ``analyzed_posts`` is the actual count of posts inspected — kept
    separate from ``limit`` (the requested cap) so the renderer can
    show "analysed N of M posts" when the target had fewer posts than
    the window.
    """
    _check_limit(limit)
    if comment_weight < 0:
        raise ValueError(f"comment_weight must be >= 0, got {comment_weight}")

    like_counter: Counter[str] = Counter()
    for user in likers:
        username = (user.username or "").strip()
        if username:
            like_counter[username] += 1

    comment_counter: Counter[str] = Counter()
    for comment in comments:
        username = (comment.user_username or "").strip()
        if username:
            comment_counter[username] += 1

    everyone = set(like_counter) | set(comment_counter)
    rows: list[FanRow] = []
    for username in everyone:
        likes = like_counter[username]
        comments_count = comment_counter[username]
        score = likes + comment_weight * comments_count
        rows.append(FanRow(username=username, likes=likes, comments=comments_count, score=score))
    rows.sort(key=lambda r: (-r.score, r.username))
    if top is not None and top > 0:
        rows = rows[:top]
    return FansResult(
        target=target,
        window=limit,
        analyzed_posts=analyzed_posts,
        comment_weight=comment_weight,
        items=rows,
        empty=analyzed_posts == 0,
    )


def count_wtagged(
    tagged_posts: Iterable[Post],
    *,
    target: str,
    limit: int = 50,
    top: int | None = 20,
) -> TopList:
    """Count owners across the first `limit` posts where target is tagged.

    Input is the iterator of posts in which `@target` was tagged
    (`backend.iter_user_tagged(...)`). The function attributes each post to
    `owner_username`; posts without an owner are skipped silently.
    """
    _check_limit(limit)
    window = _take(tagged_posts, limit)
    counter: Counter[str] = Counter()
    for post in window:
        owner = post.owner_username
        if owner and owner.strip():
            counter[owner.strip()] += 1
    return TopList(
        target=target,
        kind="wtagged",
        window=limit,
        analyzed=len(window),
        items=_top_from_counter(counter, top),
        empty=len(window) == 0,
    )


def aggregate_likes(
    posts: Iterable[Post],
    *,
    target: str,
    limit: int = 50,
    top: int | None = 5,
) -> LikesStats:
    """Aggregate `like_count` across the first `limit` posts.

    Returns total / average and a top-N most-liked posts list keyed by
    `code` for human-readable links. The /likes command (top likers by
    user) is not this function — for that, the facade composes
    `iter_post_likers` per post and uses `count_wcommented`-style
    aggregation directly.
    """
    _check_limit(limit)
    window = _take(posts, limit)
    if not window:
        return LikesStats(
            target=target,
            window=limit,
            analyzed=0,
            empty=True,
        )
    total = sum(p.like_count for p in window)
    avg = total / len(window)
    by_likes = sorted(window, key=lambda p: (-p.like_count, p.code))
    if top is not None and top > 0:
        by_likes = by_likes[:top]
    top_posts = [(p.code, p.like_count) for p in by_likes]
    return LikesStats(
        target=target,
        window=limit,
        analyzed=len(window),
        total_likes=total,
        avg_likes=avg,
        top_posts=top_posts,
        empty=False,
    )
