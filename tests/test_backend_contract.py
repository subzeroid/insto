"""Contract tests for `insto.backends.OSINTBackend`.

These tests pin behaviour every backend implementation must satisfy:

- Pagination respects `limit`: yielding stops at the requested count, and the
  backend does not fetch a page beyond what is needed to satisfy the limit.
- The `make_backend` factory imports concrete-backend modules lazily — pulling
  in `insto.backends` does not drag in `hikerapi` (or any other SDK).
- Error injection covers the full taxonomy: every `BackendError` subclass
  surfaces unmodified through the fake.
"""

from __future__ import annotations

import importlib
import sys

import pytest

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
from insto.models import Post, Profile
from tests.fakes import FakeBackend, FakeErrors


def _make_post(pk: str) -> Post:
    return Post(pk=pk, code=f"c{pk}", taken_at=0, media_type="image")


@pytest.mark.asyncio
async def test_iter_user_posts_respects_limit_and_stops_early() -> None:
    """500 posts, limit=25 → exactly 25 emitted, only ⌈25/12⌉ pages fetched."""
    backend = FakeBackend(
        profiles={"42": Profile(pk="42", username="alice", access="public")},
        posts={"42": [_make_post(str(i)) for i in range(500)]},
        page_size=12,
    )

    collected: list[Post] = []
    async for post in backend.iter_user_posts("42", limit=25):
        collected.append(post)

    assert len(collected) == 25
    assert collected[0].pk == "0"
    assert collected[-1].pk == "24"
    # 25 / 12 = 2.08 → 3 pages fetched. Asserting upper bound: must not have
    # paged into the 5th page worth of posts.
    assert backend.page_requests["iter_user_posts"] == 3


@pytest.mark.asyncio
async def test_iter_user_posts_unbounded_when_limit_none() -> None:
    backend = FakeBackend(
        profiles={"42": Profile(pk="42", username="alice", access="public")},
        posts={"42": [_make_post(str(i)) for i in range(30)]},
        page_size=10,
    )

    collected = [p async for p in backend.iter_user_posts("42")]
    assert len(collected) == 30
    assert backend.page_requests["iter_user_posts"] == 3


@pytest.mark.asyncio
async def test_resolve_target_unknown_username_raises_profile_not_found() -> None:
    backend = FakeBackend()
    with pytest.raises(ProfileNotFound):
        await backend.resolve_target("ghost")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        ProfileNotFound("u"),
        ProfilePrivate("u"),
        ProfileBlocked("u"),
        ProfileDeleted("u"),
        PostNotFound("p"),
        PostPrivate("p"),
        AuthInvalid("nope"),
        QuotaExhausted("done"),
        RateLimited(retry_after=1.0),
        SchemaDrift(endpoint="user/info", missing_field="pk"),
        Transient("blip"),
        Banned("suspended"),
    ],
)
async def test_error_injection_propagates_unmodified(error: BackendError) -> None:
    """Each taxonomy error injected on `get_profile` surfaces unmodified."""
    backend = FakeBackend(
        profiles={"1": Profile(pk="1", username="u", access="public")},
        errors=FakeErrors(get_profile=error),
    )

    with pytest.raises(type(error)) as exc:
        await backend.get_profile("1")
    assert exc.value is error
    # Once consumed, the next call succeeds.
    profile = await backend.get_profile("1")
    assert profile.pk == "1"
    assert backend.get_last_error() is error


@pytest.mark.asyncio
async def test_error_injection_on_iterator_raises_on_iteration() -> None:
    """An error on an `iter_*` slot should propagate when iteration starts."""
    backend = FakeBackend(
        posts={"1": [_make_post("a")]},
        errors=FakeErrors(iter_user_posts=Transient("blip")),
    )

    gen = backend.iter_user_posts("1")
    with pytest.raises(Transient):
        await gen.__anext__()


def test_import_insto_backends_does_not_import_hikerapi() -> None:
    """Lazy import contract: `import insto.backends` must not pull `hikerapi`.

    We pop any cached `hikerapi` and `insto.backends` modules first to force
    a fresh import, then assert that after re-importing `insto.backends` the
    `hikerapi` module is NOT in `sys.modules`. This pins the structural rule
    that `make_backend` defers the SDK import to its function body.
    """
    for mod in list(sys.modules):
        if mod == "hikerapi" or mod.startswith("hikerapi."):
            del sys.modules[mod]
        if mod == "insto.backends" or mod.startswith("insto.backends."):
            del sys.modules[mod]

    importlib.import_module("insto.backends")

    assert "hikerapi" not in sys.modules


def test_make_backend_unknown_name_raises_value_error() -> None:
    from insto.backends import make_backend

    with pytest.raises(ValueError, match="unknown backend"):
        make_backend("does-not-exist")
