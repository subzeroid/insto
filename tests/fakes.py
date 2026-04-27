"""In-memory `OSINTBackend` for unit and contract tests.

`FakeBackend` is the test double used everywhere above the backend layer.
Tests construct one with fixture data and toggle individual error variants
via the `errors` config — every `BackendError` subclass from the exception
taxonomy can be injected at any callable.

The fake is deliberately programmatic (lists of DTOs) rather than fixture-
driven; the HikerAPI JSON fixtures (`tests/fixtures/hiker/`) are consumed
by `_hiker_map.py` mappers, not by the fake.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from insto.backends._base import OSINTBackend
from insto.exceptions import (
    AuthInvalid,
    BackendError,
    Banned,
    PostNotFound,
    PostPrivate,
    ProfileBlocked,
    ProfileDeleted,
    ProfileNotFound,
    ProfilePrivate,
    QuotaExhausted,
    RateLimited,
    SchemaDrift,
    Transient,
)
from insto.models import (
    Comment,
    Highlight,
    HighlightItem,
    Post,
    Profile,
    Quota,
    Story,
    User,
)


@dataclass
class FakeErrors:
    """Per-method error injection. Set any field to raise on first call.

    Errors are consumed once: the test sets `resolve_target=ProfileNotFound("x")`
    and the next call raises it; subsequent calls behave normally. This mirrors
    real failures (a transient blip clears) and keeps tests linear.
    """

    resolve_target: BackendError | None = None
    get_profile: BackendError | None = None
    get_user_about: BackendError | None = None
    iter_user_posts: BackendError | None = None
    iter_user_followers: BackendError | None = None
    iter_user_following: BackendError | None = None
    iter_user_tagged: BackendError | None = None
    iter_user_highlights: BackendError | None = None
    iter_highlight_items: BackendError | None = None
    iter_post_comments: BackendError | None = None
    iter_post_likers: BackendError | None = None
    iter_user_stories: BackendError | None = None
    get_suggested: BackendError | None = None
    iter_hashtag_posts: BackendError | None = None


@dataclass
class FakeBackend(OSINTBackend):
    """Programmatic backend. Tests pass data directly via constructor kwargs."""

    profiles: dict[str, Profile] = field(default_factory=dict)
    """Map `pk -> Profile`. `resolve_target` looks up by `username` field."""

    abouts: dict[str, dict[str, Any]] = field(default_factory=dict)
    posts: dict[str, list[Post]] = field(default_factory=dict)
    followers: dict[str, list[User]] = field(default_factory=dict)
    following: dict[str, list[User]] = field(default_factory=dict)
    tagged: dict[str, list[Post]] = field(default_factory=dict)
    highlights: dict[str, list[Highlight]] = field(default_factory=dict)
    highlight_items: dict[str, list[HighlightItem]] = field(default_factory=dict)
    comments: dict[str, list[Comment]] = field(default_factory=dict)
    likers: dict[str, list[User]] = field(default_factory=dict)
    stories: dict[str, list[Story]] = field(default_factory=dict)
    suggested: dict[str, list[User]] = field(default_factory=dict)
    hashtag_posts: dict[str, list[Post]] = field(default_factory=dict)

    quota: Quota = field(default_factory=Quota.unknown)
    errors: FakeErrors = field(default_factory=FakeErrors)

    request_log: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)
    """Records each method call as `(name, args)` for assertion in tests."""

    page_size: int = 12
    """How many items to yield per simulated page; tests can lower this to
    make pagination behaviour observable."""

    page_requests: dict[str, int] = field(default_factory=dict)
    """Counts simulated page fetches per `iter_*` method (debug for limit tests)."""

    _last_error: BaseException | None = field(default=None, init=False)

    def _consume_error(self, slot: str) -> None:
        """If an error is queued in `self.errors.<slot>`, raise it once."""
        err = getattr(self.errors, slot)
        if err is None:
            return
        setattr(self.errors, slot, None)
        self._last_error = err
        raise err

    async def resolve_target(self, username: str) -> str:
        self.request_log.append(("resolve_target", (username,)))
        self._consume_error("resolve_target")
        for pk, profile in self.profiles.items():
            if profile.username == username:
                return pk
        raise ProfileNotFound(username)

    async def get_profile(self, pk: str) -> Profile:
        self.request_log.append(("get_profile", (pk,)))
        self._consume_error("get_profile")
        profile = self.profiles.get(pk)
        if profile is None:
            raise ProfileNotFound(pk)
        return profile

    async def get_user_about(self, pk: str) -> dict[str, Any]:
        self.request_log.append(("get_user_about", (pk,)))
        self._consume_error("get_user_about")
        return self.abouts.get(pk, {})

    async def _paged(
        self,
        slot: str,
        items: list[Any],
        limit: int | None,
    ) -> AsyncIterator[Any]:
        """Yield `items` in chunks of `self.page_size`, honouring `limit`.

        Records one page request per chunk produced. The loop breaks before
        fetching the next page once `limit` items have been yielded — this is
        what the contract test asserts (`limit=25` produces 25 items and at
        most ⌈25/page_size⌉ pages).
        """
        self.page_requests[slot] = 0
        emitted = 0
        for start in range(0, len(items), self.page_size):
            self.page_requests[slot] += 1
            page = items[start : start + self.page_size]
            for it in page:
                if limit is not None and emitted >= limit:
                    return
                yield it
                emitted += 1
            if limit is not None and emitted >= limit:
                return

    async def iter_user_posts(self, pk: str, *, limit: int | None = None) -> AsyncIterator[Post]:
        self.request_log.append(("iter_user_posts", (pk, limit)))
        self._consume_error("iter_user_posts")
        async for item in self._paged("iter_user_posts", self.posts.get(pk, []), limit):
            yield item

    async def iter_user_followers(
        self, pk: str, *, limit: int | None = None
    ) -> AsyncIterator[User]:
        self.request_log.append(("iter_user_followers", (pk, limit)))
        self._consume_error("iter_user_followers")
        async for item in self._paged("iter_user_followers", self.followers.get(pk, []), limit):
            yield item

    async def iter_user_following(
        self, pk: str, *, limit: int | None = None
    ) -> AsyncIterator[User]:
        self.request_log.append(("iter_user_following", (pk, limit)))
        self._consume_error("iter_user_following")
        async for item in self._paged("iter_user_following", self.following.get(pk, []), limit):
            yield item

    async def iter_user_tagged(self, pk: str, *, limit: int | None = None) -> AsyncIterator[Post]:
        self.request_log.append(("iter_user_tagged", (pk, limit)))
        self._consume_error("iter_user_tagged")
        async for item in self._paged("iter_user_tagged", self.tagged.get(pk, []), limit):
            yield item

    async def iter_user_highlights(
        self, pk: str, *, limit: int | None = None
    ) -> AsyncIterator[Highlight]:
        self.request_log.append(("iter_user_highlights", (pk, limit)))
        self._consume_error("iter_user_highlights")
        async for item in self._paged("iter_user_highlights", self.highlights.get(pk, []), limit):
            yield item

    async def iter_highlight_items(
        self, highlight_id: str, *, limit: int | None = None
    ) -> AsyncIterator[HighlightItem]:
        self.request_log.append(("iter_highlight_items", (highlight_id, limit)))
        self._consume_error("iter_highlight_items")
        async for item in self._paged(
            "iter_highlight_items",
            self.highlight_items.get(highlight_id, []),
            limit,
        ):
            yield item

    async def iter_post_comments(
        self, media_pk: str, *, limit: int | None = None
    ) -> AsyncIterator[Comment]:
        self.request_log.append(("iter_post_comments", (media_pk, limit)))
        self._consume_error("iter_post_comments")
        async for item in self._paged("iter_post_comments", self.comments.get(media_pk, []), limit):
            yield item

    async def iter_post_likers(
        self, media_pk: str, *, limit: int | None = None
    ) -> AsyncIterator[User]:
        self.request_log.append(("iter_post_likers", (media_pk, limit)))
        self._consume_error("iter_post_likers")
        async for item in self._paged("iter_post_likers", self.likers.get(media_pk, []), limit):
            yield item

    async def iter_user_stories(self, pk: str, *, limit: int | None = None) -> AsyncIterator[Story]:
        self.request_log.append(("iter_user_stories", (pk, limit)))
        self._consume_error("iter_user_stories")
        async for item in self._paged("iter_user_stories", self.stories.get(pk, []), limit):
            yield item

    async def get_suggested(self, pk: str) -> list[User]:
        self.request_log.append(("get_suggested", (pk,)))
        self._consume_error("get_suggested")
        return list(self.suggested.get(pk, []))

    async def iter_hashtag_posts(
        self, tag: str, *, limit: int | None = None
    ) -> AsyncIterator[Post]:
        self.request_log.append(("iter_hashtag_posts", (tag, limit)))
        self._consume_error("iter_hashtag_posts")
        async for item in self._paged("iter_hashtag_posts", self.hashtag_posts.get(tag, []), limit):
            yield item

    def get_quota(self) -> Quota:
        return self.quota

    def get_last_error(self) -> BaseException | None:
        return self._last_error


__all__ = [
    "AuthInvalid",
    "Banned",
    "FakeBackend",
    "FakeErrors",
    "PostNotFound",
    "PostPrivate",
    "ProfileBlocked",
    "ProfileDeleted",
    "ProfileNotFound",
    "ProfilePrivate",
    "QuotaExhausted",
    "RateLimited",
    "SchemaDrift",
    "Transient",
]
