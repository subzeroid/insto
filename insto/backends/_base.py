"""Abstract OSINT backend interface.

`OSINTBackend` is the contract every backend (HikerAPI v0.1, aiograpi v0.2,
future TikTok / Bluesky / Threads providers) must implement. The command and
service layers depend on this ABC, never on a concrete backend — that is what
keeps v0.2 a pure addition.

All collection-returning methods are async generators (`AsyncIterator[T]`)
with an optional `limit: int | None` parameter. Cursors / page tokens are an
internal implementation detail of each backend and never leak above this
layer.

The methods raise exceptions from `insto.exceptions` exclusively; raw HTTP /
SDK errors must be mapped to the taxonomy by the backend itself.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any

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


class OSINTBackend(ABC):
    """Async OSINT data source for one social platform.

    Implementations are expected to be safe for concurrent use within a single
    asyncio event loop (the REPL drives one loop and may dispatch watch tasks
    in parallel). They are NOT required to be process-safe.
    """

    # Capability tokens this backend exposes. Commands declare what they need
    # via `@command(..., requires=("followed",))`; the dispatcher rejects the
    # call when the active backend does not advertise the required tokens.
    # HikerAPI exposes only public OSINT, so the default is empty; an
    # `aiograpi` backend would extend this with `{"followed", ...}`.
    capabilities: frozenset[str] = frozenset()

    @abstractmethod
    async def resolve_target(self, username: str) -> str:
        """Return the stable `pk` for `username`, or raise `ProfileNotFound`."""

    @abstractmethod
    async def get_profile(self, pk: str) -> Profile:
        """Fetch the full profile DTO for `pk`."""

    @abstractmethod
    async def get_user_about(self, pk: str) -> dict[str, Any]:
        """Fetch the `user_about` payload (verification, dates, links)."""

    @abstractmethod
    def iter_user_posts(self, pk: str, *, limit: int | None = None) -> AsyncIterator[Post]:
        """Iterate the user's feed posts in reverse chronological order."""

    @abstractmethod
    def iter_user_followers(self, pk: str, *, limit: int | None = None) -> AsyncIterator[User]:
        """Iterate the user's followers."""

    @abstractmethod
    def iter_user_following(self, pk: str, *, limit: int | None = None) -> AsyncIterator[User]:
        """Iterate accounts the user is following."""

    @abstractmethod
    def iter_user_tagged(self, pk: str, *, limit: int | None = None) -> AsyncIterator[Post]:
        """Iterate posts the user is tagged in."""

    @abstractmethod
    def iter_user_highlights(
        self, pk: str, *, limit: int | None = None
    ) -> AsyncIterator[Highlight]:
        """Iterate highlight reels owned by the user."""

    @abstractmethod
    def iter_highlight_items(
        self, highlight_id: str, *, limit: int | None = None
    ) -> AsyncIterator[HighlightItem]:
        """Iterate items inside a highlight reel."""

    @abstractmethod
    def iter_post_comments(
        self, media_pk: str, *, limit: int | None = None
    ) -> AsyncIterator[Comment]:
        """Iterate comments on a post."""

    @abstractmethod
    def iter_post_likers(self, media_pk: str, *, limit: int | None = None) -> AsyncIterator[User]:
        """Iterate users who liked a post."""

    @abstractmethod
    def iter_user_stories(self, pk: str, *, limit: int | None = None) -> AsyncIterator[Story]:
        """Iterate currently-active stories of a user."""

    @abstractmethod
    async def get_suggested(self, pk: str) -> list[User]:
        """Fetch accounts suggested as similar to `pk`."""

    @abstractmethod
    def iter_hashtag_posts(self, tag: str, *, limit: int | None = None) -> AsyncIterator[Post]:
        """Iterate top / recent posts under a hashtag."""

    @abstractmethod
    def get_quota(self) -> Quota:
        """Return the last-known quota state for the backend."""

    @abstractmethod
    def get_last_error(self) -> BaseException | None:
        """Return the last exception raised by this backend, if any."""

    def get_schema_drift_count(self) -> int:
        """Return the number of `SchemaDrift` errors observed this session.

        Default 0 so simple backends (in-process fakes) need not track. Real
        backends override to expose a running counter — surfaced by `/health`
        so an operator can spot provider degradation.
        """
        return 0

    async def aclose(self) -> None:  # noqa: B027 — intentional empty default
        """Release backend-owned resources (HTTP clients, sockets, …).

        Default implementation is a no-op so simple in-memory backends (the
        test fakes, future mock backends) need not override. Real backends
        with network clients (HikerBackend) override to close them.
        """
