"""HikerAPI backend — placeholder.

The full implementation lands in Task 9 of the v0.1 plan: SDK calls wrapped
in `with_retry`, error mapping, quota parsing, proxy support, cursor safety
caps. This stub exists today only so the lazy-import path through
`make_backend("hiker")` and the `OSINTBackend` ABC contract are wired up.

`hikerapi` is imported eagerly inside this module — the laziness is at the
`insto.backends.__init__` factory level (importing `insto.backends` does not
import this module, and therefore does not import `hikerapi`).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import hikerapi  # noqa: F401  # pin the lazy-import contract; full use in Task 9.

from insto.backends._base import OSINTBackend
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


class HikerBackend(OSINTBackend):
    """Placeholder. Real implementation in Task 9."""

    def __init__(self, **opts: Any) -> None:
        self._opts = opts

    async def resolve_target(self, username: str) -> str:
        raise NotImplementedError

    async def get_profile(self, pk: str) -> Profile:
        raise NotImplementedError

    async def get_user_about(self, pk: str) -> dict[str, Any]:
        raise NotImplementedError

    async def iter_user_posts(
        self, pk: str, *, limit: int | None = None
    ) -> AsyncIterator[Post]:
        raise NotImplementedError
        yield  # pragma: no cover  # makes the function an async generator

    async def iter_user_followers(
        self, pk: str, *, limit: int | None = None
    ) -> AsyncIterator[User]:
        raise NotImplementedError
        yield  # pragma: no cover

    async def iter_user_following(
        self, pk: str, *, limit: int | None = None
    ) -> AsyncIterator[User]:
        raise NotImplementedError
        yield  # pragma: no cover

    async def iter_user_tagged(
        self, pk: str, *, limit: int | None = None
    ) -> AsyncIterator[Post]:
        raise NotImplementedError
        yield  # pragma: no cover

    async def iter_user_highlights(
        self, pk: str, *, limit: int | None = None
    ) -> AsyncIterator[Highlight]:
        raise NotImplementedError
        yield  # pragma: no cover

    async def iter_highlight_items(
        self, highlight_id: str, *, limit: int | None = None
    ) -> AsyncIterator[HighlightItem]:
        raise NotImplementedError
        yield  # pragma: no cover

    async def iter_post_comments(
        self, media_pk: str, *, limit: int | None = None
    ) -> AsyncIterator[Comment]:
        raise NotImplementedError
        yield  # pragma: no cover

    async def iter_post_likers(
        self, media_pk: str, *, limit: int | None = None
    ) -> AsyncIterator[User]:
        raise NotImplementedError
        yield  # pragma: no cover

    async def iter_user_stories(
        self, pk: str, *, limit: int | None = None
    ) -> AsyncIterator[Story]:
        raise NotImplementedError
        yield  # pragma: no cover

    async def get_suggested(self, pk: str) -> list[User]:
        raise NotImplementedError

    async def iter_hashtag_posts(
        self, tag: str, *, limit: int | None = None
    ) -> AsyncIterator[Post]:
        raise NotImplementedError
        yield  # pragma: no cover

    def get_quota(self) -> Quota:
        return Quota.unknown()

    def get_last_error(self) -> BaseException | None:
        return None
