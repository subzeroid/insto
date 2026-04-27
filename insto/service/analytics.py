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
