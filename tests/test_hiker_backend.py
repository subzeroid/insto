"""Unit tests for `insto.backends.hiker.HikerBackend`.

The HikerAPI SDK builds an `httpx.AsyncClient` internally. To exercise the
backend without hitting the network we:

1. Construct a real `hikerapi.AsyncClient(token="test")` so the token assert
   passes and the SDK methods are wired up.
2. Replace its private `_client` with a fresh `httpx.AsyncClient` whose
   transport is an `httpx.MockTransport`.
3. Hand the SDK to `HikerBackend(client=...)` so the `__init__` skips its
   own SDK construction and installs the response hook on our test client.

This means the *real* error path (httpx hook → `raise_for_status` →
translator → typed exception) is exercised end-to-end. We patch
`with_retry` to a single-attempt no-sleep variant on the backend so retriable
errors propagate immediately rather than burning seconds on backoff.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import hikerapi
import httpx
import pytest

from insto.backends._retry import with_retry
from insto.backends.hiker import (
    DEFAULT_MAX_PAGES,
    HikerBackend,
    _extract_chunk,
    _normalise_cursor,
    _validate_proxy_url,
)
from insto.exceptions import (
    AuthInvalid,
    BackendError,
    Banned,
    PostNotFound,
    ProfileNotFound,
    QuotaExhausted,
    RateLimited,
    SchemaDrift,
    Transient,
)
from insto.models import Post, Profile, User

FIXTURES = Path(__file__).parent / "fixtures" / "hiker"


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def _no_retry() -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    async def _instant_sleep(_: float) -> None:  # pragma: no cover - never sleeps
        return None

    return with_retry(max_attempts=1, sleep=_instant_sleep)


def _make_backend(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> HikerBackend:
    sdk = hikerapi.AsyncClient(token="test", timeout=5.0)
    transport = httpx.MockTransport(handler)
    new_client = httpx.AsyncClient(base_url=sdk._url, transport=transport)
    new_client.headers.update(sdk._headers)
    sdk._client = new_client
    return HikerBackend(client=sdk, max_pages=max_pages, retry_decorator=_no_retry())


# ---------------------------------------------------------------- proxy URL


def test_validate_proxy_url_accepts_http_https_socks() -> None:
    for url in (
        "http://127.0.0.1:8080",
        "https://proxy.example:443",
        "socks5://127.0.0.1:1080",
        "socks5h://127.0.0.1:9050",
    ):
        _validate_proxy_url(url)  # should not raise


@pytest.mark.parametrize(
    "url",
    [
        "not-a-url",
        "gopher://example.com:70",
        "ftp://localhost",
        "://no-scheme",
        "http://",
        # netloc='@' with no host — must be rejected before httpx ever sees it.
        "http://@/",
        "http://user:pass@",
    ],
)
def test_validate_proxy_url_rejects_garbage(url: str) -> None:
    with pytest.raises(BackendError, match="invalid proxy URL"):
        _validate_proxy_url(url)


def test_constructor_rejects_invalid_proxy_before_sdk() -> None:
    """A bad proxy must error out without ever touching the SDK / token env."""

    with pytest.raises(BackendError, match="invalid proxy URL"):
        HikerBackend(token="test", proxy="not-a-url")


def test_constructor_threads_proxy_into_httpx_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """When a valid proxy is given, httpx.AsyncClient is rebuilt with `proxy=`."""

    captured: dict[str, Any] = {}
    real_async_client = httpx.AsyncClient

    def spy(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        if "proxy" in kwargs:
            captured.update(kwargs)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("insto.backends.hiker.httpx.AsyncClient", spy)
    backend = HikerBackend(token="test", proxy="http://127.0.0.1:8080")
    assert captured.get("proxy") == "http://127.0.0.1:8080"
    assert backend._proxy == "http://127.0.0.1:8080"


# ------------------------------------------------------------ chunk extractor


def test_extract_chunk_list_form() -> None:
    items, cursor = _extract_chunk([[{"pk": "1"}, {"pk": "2"}], "next-x"])
    assert [i["pk"] for i in items] == ["1", "2"]
    assert cursor == "next-x"


def test_extract_chunk_dict_with_response_wrapper() -> None:
    payload = {"response": {"users": [{"pk": "9"}], "next_max_id": "abc"}}
    items, cursor = _extract_chunk(payload)
    assert items == [{"pk": "9"}]
    assert cursor == "abc"


def test_extract_chunk_flat_dict_end_cursor() -> None:
    payload = {"items": [{"pk": "10"}], "end_cursor": "ec1"}
    items, cursor = _extract_chunk(payload)
    assert items == [{"pk": "10"}]
    assert cursor == "ec1"


def test_extract_chunk_terminates_when_cursor_falsy() -> None:
    items, cursor = _extract_chunk([[{"pk": "1"}], None])
    assert items == [{"pk": "1"}]
    assert cursor is None


def test_normalise_cursor_preserves_integer_zero() -> None:
    """Integer `0` is a legitimate first-page cursor on some
    endpoints; a plain truthiness check would terminate pagination
    on the first page. Regression test for the int-cursor fix."""
    assert _normalise_cursor(0) == "0"


def test_normalise_cursor_treats_false_as_terminal() -> None:
    """`False` must signal end-of-pagination. Without an explicit
    `is False` check, `str(False) == "False"` would be re-fed as a
    literal cursor and loop until the safety cap aborts."""
    assert _normalise_cursor(False) is None


def test_normalise_cursor_handles_none_and_empty_string() -> None:
    assert _normalise_cursor(None) is None
    assert _normalise_cursor("") is None


def test_normalise_cursor_returns_string_form_for_real_cursors() -> None:
    assert _normalise_cursor("abc") == "abc"
    assert _normalise_cursor(42) == "42"


# ------------------------------------------------------------ resolve_target


@pytest.mark.asyncio
async def test_resolve_target_returns_pk_from_payload() -> None:
    profile = _load("profile_public")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/user/by/username"
        assert request.url.params["username"] == "alice_public"
        return httpx.Response(200, json={"user": profile})

    backend = _make_backend(handler)
    pk = await backend.resolve_target("alice_public")
    assert pk == "12345678"


@pytest.mark.asyncio
async def test_resolve_target_404_maps_to_profile_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "not found"})

    backend = _make_backend(handler)
    with pytest.raises(ProfileNotFound) as exc:
        await backend.resolve_target("ghost")
    assert exc.value.username == "ghost"
    assert isinstance(backend.get_last_error(), ProfileNotFound)


@pytest.mark.asyncio
async def test_resolve_target_401_maps_to_auth_invalid() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "bad token"})

    backend = _make_backend(handler)
    with pytest.raises(AuthInvalid):
        await backend.resolve_target("anyone")


@pytest.mark.asyncio
async def test_resolve_target_402_maps_to_quota_exhausted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, json={"detail": "no credit"})

    backend = _make_backend(handler)
    with pytest.raises(QuotaExhausted):
        await backend.resolve_target("anyone")


@pytest.mark.asyncio
async def test_resolve_target_403_maps_to_banned() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": "suspended"})

    backend = _make_backend(handler)
    with pytest.raises(Banned):
        await backend.resolve_target("anyone")


@pytest.mark.asyncio
async def test_resolve_target_429_maps_to_rate_limited_with_retry_after() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"detail": "slow down"}, headers={"retry-after": "12"})

    backend = _make_backend(handler)
    with pytest.raises(RateLimited) as exc:
        await backend.resolve_target("anyone")
    assert exc.value.retry_after == pytest.approx(12.0)


@pytest.mark.asyncio
async def test_resolve_target_500_maps_to_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "boom"})

    backend = _make_backend(handler)
    with pytest.raises(Transient):
        await backend.resolve_target("anyone")


@pytest.mark.asyncio
async def test_network_error_maps_to_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("conn refused")

    backend = _make_backend(handler)
    with pytest.raises(Transient, match="network error"):
        await backend.resolve_target("anyone")


@pytest.mark.asyncio
async def test_resolve_target_schema_drift_when_pk_missing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"user": {"username": "x"}})

    backend = _make_backend(handler)
    with pytest.raises(SchemaDrift) as exc:
        await backend.resolve_target("x")
    assert exc.value.endpoint == "user_by_username_v2"
    assert exc.value.missing_field == "pk"


# ----------------------------------------------------------------- get_profile


@pytest.mark.asyncio
async def test_get_profile_maps_dto() -> None:
    profile = _load("profile_public")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/user/by/id"
        return httpx.Response(200, json={"user": profile})

    backend = _make_backend(handler)
    dto = await backend.get_profile("12345678")
    assert isinstance(dto, Profile)
    assert dto.username == "alice_public"
    assert dto.follower_count == 12500
    assert dto.access == "public"


@pytest.mark.asyncio
async def test_get_user_about_returns_payload_dict() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"verified": True})

    backend = _make_backend(handler)
    payload = await backend.get_user_about("12345678")
    assert payload == {"verified": True}


# ---------------------------------------------------------------- quota header


@pytest.mark.asyncio
async def test_quota_header_parsed_from_response() -> None:
    profile = _load("profile_public")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"user": profile},
            headers={
                "x-quota-remaining": "42",
                "x-quota-limit": "100",
                "x-quota-reset": "1714210800",
            },
        )

    backend = _make_backend(handler)
    assert backend.get_quota().remaining is None  # before any call
    await backend.resolve_target("alice_public")
    quota = backend.get_quota()
    assert quota.remaining == 42
    assert quota.limit == 100
    assert quota.reset_at == 1714210800


@pytest.mark.asyncio
async def test_quota_header_parsed_from_ratelimit_aliases() -> None:
    profile = _load("profile_public")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"user": profile},
            headers={"x-ratelimit-remaining": "7"},
        )

    backend = _make_backend(handler)
    await backend.resolve_target("alice_public")
    assert backend.get_quota().remaining == 7


# -------------------------------------------------------------- iter cursoring


@pytest.mark.asyncio
async def test_iter_user_posts_advances_cursor_and_terminates() -> None:
    """Two pages of 12 then an empty page → 24 items, cursor flow honoured."""

    page_a = [{"pk": str(i), "code": f"c{i}", "taken_at": 1, "media_type": 1} for i in range(12)]
    page_b = [
        {"pk": str(i), "code": f"c{i}", "taken_at": 1, "media_type": 1} for i in range(12, 24)
    ]
    cursors_seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        cursor = request.url.params.get("end_cursor")
        cursors_seen.append(cursor or None)
        if not cursor:
            return httpx.Response(200, json=[page_a, "cursor-1"])
        if cursor == "cursor-1":
            return httpx.Response(200, json=[page_b, "cursor-2"])
        return httpx.Response(200, json=[[], None])

    backend = _make_backend(handler)
    posts = [p async for p in backend.iter_user_posts("42")]
    assert [p.pk for p in posts] == [str(i) for i in range(24)]
    assert cursors_seen == [None, "cursor-1", "cursor-2"]


@pytest.mark.asyncio
async def test_iter_user_posts_respects_limit_and_does_not_overfetch() -> None:
    """`limit=15` should fetch page 1 (12 items) + page 2 (3 items needed) only."""

    page_a = [{"pk": str(i), "code": f"c{i}", "taken_at": 1, "media_type": 1} for i in range(12)]
    page_b = [
        {"pk": str(i), "code": f"c{i}", "taken_at": 1, "media_type": 1} for i in range(12, 24)
    ]
    page_c = [
        {"pk": str(i), "code": f"c{i}", "taken_at": 1, "media_type": 1} for i in range(24, 36)
    ]
    requests_made = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests_made
        requests_made += 1
        cursor = request.url.params.get("end_cursor")
        if not cursor:
            return httpx.Response(200, json=[page_a, "cursor-1"])
        if cursor == "cursor-1":
            return httpx.Response(200, json=[page_b, "cursor-2"])
        return httpx.Response(200, json=[page_c, None])

    backend = _make_backend(handler)
    posts = [p async for p in backend.iter_user_posts("42", limit=15)]
    assert len(posts) == 15
    assert posts[-1].pk == "14"
    assert requests_made == 2  # never paged into "cursor-2"


@pytest.mark.asyncio
async def test_iter_user_posts_safety_cap_aborts_unterminated_cursor() -> None:
    """If the cursor never empties, the safety cap raises BackendError."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                [{"pk": "x", "code": "c", "taken_at": 1, "media_type": 1}],
                "always-more",
            ],
        )

    backend = _make_backend(handler, max_pages=3)
    with pytest.raises(BackendError, match="cursor did not terminate"):
        async for _ in backend.iter_user_posts("42"):
            pass


@pytest.mark.asyncio
async def test_iter_user_followers_uses_max_id_cursor() -> None:
    user_short = _load("user_short")
    cursors_seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        cursors_seen.append(request.url.params.get("max_id") or None)
        if cursors_seen[-1] is None:
            return httpx.Response(200, json={"users": [user_short], "next_max_id": "next-1"})
        return httpx.Response(200, json={"users": [], "next_max_id": None})

    backend = _make_backend(handler)
    users = [u async for u in backend.iter_user_followers("12345678")]
    assert len(users) == 1
    assert isinstance(users[0], User)
    assert users[0].username == "follower_one"
    assert cursors_seen == [None, "next-1"]


@pytest.mark.asyncio
async def test_iter_post_comments_translates_404_to_post_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "gone"})

    backend = _make_backend(handler)
    with pytest.raises(PostNotFound) as exc:
        async for _ in backend.iter_post_comments("999"):
            pass
    assert exc.value.ref == "999"


@pytest.mark.asyncio
async def test_iter_post_likers_single_page_maps_users() -> None:
    user_short = _load("user_short")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/media/likers"
        return httpx.Response(200, json={"users": [user_short]})

    backend = _make_backend(handler)
    likers = [u async for u in backend.iter_post_likers("3000000000000000001")]
    assert len(likers) == 1
    assert likers[0].pk == "55555555"


@pytest.mark.asyncio
async def test_get_suggested_returns_user_list() -> None:
    user_short = _load("user_short")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"users": [user_short, user_short]})

    backend = _make_backend(handler)
    suggested = await backend.get_suggested("12345678")
    assert len(suggested) == 2
    assert all(u.username == "follower_one" for u in suggested)


@pytest.mark.asyncio
async def test_iter_chunks_skips_non_dict_item_with_schema_drift() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[["not-a-dict"], None])

    backend = _make_backend(handler)
    with pytest.raises(SchemaDrift) as exc:
        async for _ in backend.iter_user_posts("42"):
            pass
    assert exc.value.endpoint == "user_medias_chunk_v1"


# ---------------------------------------------------------------- post mapping


@pytest.mark.asyncio
async def test_iter_user_posts_maps_post_dto_correctly() -> None:
    post_image = _load("post_image")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[[post_image], None])

    backend = _make_backend(handler)
    posts = [p async for p in backend.iter_user_posts("12345678")]
    assert len(posts) == 1
    assert isinstance(posts[0], Post)
    assert posts[0].media_type == "image"
    assert posts[0].pk == post_image["pk"]


# --------------------------------------------------------------- last_error


@pytest.mark.asyncio
async def test_last_error_records_taxonomy_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "no"})

    backend = _make_backend(handler)
    assert backend.get_last_error() is None
    with pytest.raises(AuthInvalid):
        await backend.resolve_target("anyone")
    err = backend.get_last_error()
    assert isinstance(err, AuthInvalid)


# --------------------------------------------------------------- aclose


@pytest.mark.asyncio
async def test_aclose_closes_underlying_client() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"user": _load("profile_public")})

    backend = _make_backend(handler)
    await backend.aclose()
    # Subsequent SDK call should fail because client is closed.
    with pytest.raises((RuntimeError, BackendError, httpx.HTTPError)):
        await backend.resolve_target("alice_public")


# ----------------------------------------------------- non-blocking concurrency


@pytest.mark.asyncio
async def test_concurrent_calls_share_quota_state() -> None:
    """Two concurrent calls each receive a quota header — last write wins."""

    profile = _load("profile_public")
    counter = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        counter["n"] += 1
        return httpx.Response(
            200,
            json={"user": profile},
            headers={"x-quota-remaining": str(100 - counter["n"])},
        )

    backend = _make_backend(handler)
    await asyncio.gather(
        backend.resolve_target("alice_public"),
        backend.resolve_target("alice_public"),
    )
    assert backend.get_quota().remaining is not None
    assert backend.get_quota().remaining <= 99
