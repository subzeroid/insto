"""In-process fake backend used by E2E tests via `INSTO_BACKEND=fake`.

Selecting this backend at the factory level (rather than monkey-patching)
keeps the same code path the real `insto` CLI / REPL goes through; the
only difference is the data source.

The fake ships with one canonical user (`alice`) plus a couple of posts so
the smoke flow `/target → /info → /posts → /watch` works without any
network. Tests that need richer data can point `INSTO_FAKE_FIXTURE` at a
JSON file with the same shape (see `_load_fixture`).
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from insto.backends._base import OSINTBackend
from insto.exceptions import ProfileNotFound
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

FAKE_FIXTURE_ENV = "INSTO_FAKE_FIXTURE"

_DEFAULT_PROFILE = Profile(
    pk="1001",
    username="alice",
    access="public",
    full_name="Alice Example",
    biography="fake bio for e2e tests",
    follower_count=2048,
    following_count=512,
    media_count=2,
    is_verified=True,
)
_DEFAULT_ABOUT: dict[str, Any] = {
    "joined": "2018-01-01",
    "is_eligible_to_show_email": False,
}
_DEFAULT_POSTS: list[Post] = [
    Post(
        pk="p1",
        code="ABC123",
        taken_at=1_700_000_000,
        media_type="image",
        caption="hello world #fake",
        like_count=10,
        comment_count=2,
        owner_username="alice",
        owner_pk="1001",
    ),
    Post(
        pk="p2",
        code="ABC456",
        taken_at=1_700_010_000,
        media_type="video",
        caption="another #fake post",
        like_count=20,
        comment_count=4,
        owner_username="alice",
        owner_pk="1001",
    ),
]


def _load_fixture() -> tuple[dict[str, Profile], dict[str, dict[str, Any]], dict[str, list[Post]]]:
    """Load profiles/abouts/posts from `INSTO_FAKE_FIXTURE` JSON, or defaults."""
    raw_path = os.environ.get(FAKE_FIXTURE_ENV)
    if not raw_path:
        return (
            {_DEFAULT_PROFILE.pk: _DEFAULT_PROFILE},
            {_DEFAULT_PROFILE.pk: _DEFAULT_ABOUT},
            {_DEFAULT_PROFILE.pk: list(_DEFAULT_POSTS)},
        )
    data = json.loads(Path(raw_path).read_text(encoding="utf-8"))
    profiles = {item["pk"]: Profile(**item) for item in data.get("profiles", [])}
    abouts = {pk: dict(payload) for pk, payload in data.get("abouts", {}).items()}
    posts = {pk: [Post(**item) for item in items] for pk, items in data.get("posts", {}).items()}
    return profiles, abouts, posts


class FakeBackendProd(OSINTBackend):
    """`OSINTBackend` impl backed by hardcoded data; opt-in via env.

    Distinct class name from `tests.fakes.FakeBackend` to avoid confusion —
    that one is a programmatic test double; this one is a self-contained
    backend selectable through the public factory.
    """

    def __init__(self, **_opts: Any) -> None:
        self._profiles, self._abouts, self._posts = _load_fixture()
        self._quota = Quota(remaining=999, limit=1000)

    # ----------------------------------------------------------------- target

    async def resolve_target(self, username: str) -> str:
        cleaned = username.lstrip("@")
        for pk, profile in self._profiles.items():
            if profile.username == cleaned:
                return pk
        raise ProfileNotFound(cleaned)

    async def get_profile(self, pk: str) -> Profile:
        profile = self._profiles.get(pk)
        if profile is None:
            raise ProfileNotFound(pk)
        return profile

    async def get_user_about(self, pk: str) -> dict[str, Any]:
        return dict(self._abouts.get(pk, {}))

    # ------------------------------------------------------------------ feed

    async def iter_user_posts(self, pk: str, *, limit: int | None = None) -> AsyncIterator[Post]:
        items = self._posts.get(pk, [])
        for i, post in enumerate(items):
            if limit is not None and i >= limit:
                return
            yield post

    async def iter_user_followers(
        self, pk: str, *, limit: int | None = None
    ) -> AsyncIterator[User]:
        if False:  # pragma: no cover - empty generator
            yield User(pk="", username="")

    async def iter_user_following(
        self, pk: str, *, limit: int | None = None
    ) -> AsyncIterator[User]:
        if False:  # pragma: no cover - empty generator
            yield User(pk="", username="")

    async def iter_user_tagged(self, pk: str, *, limit: int | None = None) -> AsyncIterator[Post]:
        if False:  # pragma: no cover - empty generator
            yield Post(pk="", code="", taken_at=0, media_type="image")

    async def iter_user_highlights(
        self, pk: str, *, limit: int | None = None
    ) -> AsyncIterator[Highlight]:
        if False:  # pragma: no cover - empty generator
            yield Highlight(pk="", title="")

    async def iter_highlight_items(
        self, highlight_id: str, *, limit: int | None = None
    ) -> AsyncIterator[HighlightItem]:
        if False:  # pragma: no cover - empty generator
            yield HighlightItem(
                pk="", highlight_pk="", taken_at=0, media_type="image", media_url=""
            )

    async def iter_post_comments(
        self, media_pk: str, *, limit: int | None = None
    ) -> AsyncIterator[Comment]:
        if False:  # pragma: no cover - empty generator
            yield Comment(pk="", media_pk="", user_pk="", user_username="", text="", created_at=0)

    async def iter_post_likers(
        self, media_pk: str, *, limit: int | None = None
    ) -> AsyncIterator[User]:
        if False:  # pragma: no cover - empty generator
            yield User(pk="", username="")

    async def iter_user_stories(self, pk: str, *, limit: int | None = None) -> AsyncIterator[Story]:
        if False:  # pragma: no cover - empty generator
            yield Story(pk="", taken_at=0, expires_at=0, media_type="image", media_url="")

    async def get_suggested(self, pk: str) -> list[User]:
        return []

    async def iter_hashtag_posts(
        self, tag: str, *, limit: int | None = None
    ) -> AsyncIterator[Post]:
        if False:  # pragma: no cover - empty generator
            yield Post(pk="", code="", taken_at=0, media_type="image")

    # ------------------------------------------------------------------- ops

    def get_quota(self) -> Quota:
        return self._quota

    def get_last_error(self) -> BaseException | None:
        return None


__all__ = ["FAKE_FIXTURE_ENV", "FakeBackendProd"]
