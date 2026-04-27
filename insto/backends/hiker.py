"""HikerAPI backend — concrete `OSINTBackend` over `hikerapi.AsyncClient`.

Wraps the SDK so that:

- HTTP errors propagate as typed `BackendError` subclasses (the SDK itself
  swallows non-2xx responses; we install an httpx response hook that calls
  `raise_for_status()` and an error translator that maps the httpx exception
  to our taxonomy).
- Quota headers (``x-quota-*`` / ``x-ratelimit-*``) are captured into
  ``Quota`` on every response.
- All SDK calls are wrapped with ``with_retry`` so ``RateLimited`` /
  ``Transient`` are retried with backoff before propagating.
- Each ``iter_*`` method is bounded by ``max_pages`` (default 1000) — a
  defensive cap against unterminated cursors.
- ``proxy`` is validated and threaded into a freshly-built
  ``httpx.AsyncClient`` (the SDK's stock client has no proxy parameter).

`hikerapi` is imported at module top-level — laziness is enforced one layer
up, in `insto.backends.__init__.make_backend`, which only imports this
module when `name == "hiker"`.
"""

from __future__ import annotations

import urllib.parse
from collections.abc import AsyncIterator, Awaitable, Callable
from functools import partial
from typing import Any, NoReturn, TypeVar, cast

import hikerapi
import httpx

from insto.backends._base import OSINTBackend
from insto.backends._hiker_map import (
    map_comment,
    map_highlight,
    map_highlight_item,
    map_post,
    map_profile,
    map_story,
    map_user,
)
from insto.backends._retry import with_retry
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

T = TypeVar("T")

DEFAULT_MAX_PAGES = 1000

_QUOTA_REMAINING_HEADERS: tuple[str, ...] = ("x-quota-remaining", "x-ratelimit-remaining")
_QUOTA_LIMIT_HEADERS: tuple[str, ...] = ("x-quota-limit", "x-ratelimit-limit")
_QUOTA_RESET_HEADERS: tuple[str, ...] = ("x-quota-reset", "x-ratelimit-reset")
_RETRY_AFTER_HEADERS: tuple[str, ...] = ("retry-after", "x-ratelimit-reset", "x-quota-reset")

_VALID_PROXY_SCHEMES: frozenset[str] = frozenset({"http", "https", "socks5", "socks5h"})


class _NotFoundError(BackendError):
    """Internal 404 sentinel.

    Re-raised at each public method as ``ProfileNotFound`` / ``PostNotFound``
    with the right context (username / ref). Never escapes this module.
    """

    def __init__(self) -> None:
        super().__init__("not found")


def _validate_proxy_url(url: str) -> None:
    """Reject malformed proxy URLs *before* the SDK is ever constructed."""

    try:
        parsed = urllib.parse.urlparse(url)
    except (ValueError, TypeError) as exc:  # pragma: no cover - urlparse is robust
        raise BackendError(f"invalid proxy URL: {url!r}") from exc
    if parsed.scheme not in _VALID_PROXY_SCHEMES:
        allowed = sorted(_VALID_PROXY_SCHEMES)
        raise BackendError(f"invalid proxy URL {url!r}: scheme must be one of {allowed}")
    if not parsed.hostname:
        raise BackendError(f"invalid proxy URL {url!r}: missing host")


def _parse_int_header(headers: httpx.Headers, names: tuple[str, ...]) -> int | None:
    for name in names:
        raw = headers.get(name)
        if raw is None:
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            continue
    return None


def _parse_retry_after(headers: httpx.Headers) -> float:
    for name in _RETRY_AFTER_HEADERS:
        raw = headers.get(name)
        if raw is None:
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return 60.0


def _translate_http_status(exc: httpx.HTTPStatusError) -> BackendError:
    status = exc.response.status_code
    if status == 401:
        return AuthInvalid("HikerAPI rejected the access token")
    if status == 402:
        return QuotaExhausted("HikerAPI quota exhausted")
    if status == 403:
        return Banned("HikerAPI access forbidden (account suspended?)")
    if status == 404:
        return _NotFoundError()
    if status == 429:
        return RateLimited(retry_after=_parse_retry_after(exc.response.headers))
    if 500 <= status < 600:
        return Transient(f"HikerAPI server error {status}")
    return BackendError(f"unexpected HikerAPI status {status}")


def _extract_chunk(payload: Any) -> tuple[list[Any], str | None]:
    """Return ``(items, next_cursor)`` from a HikerAPI chunk-endpoint response.

    Hiker's chunk endpoints come back in one of these shapes (the SDK's own
    paging helper handles all three, so we mirror it):

    - ``[items, next_cursor]`` — a list of length 2.
    - ``{"response": {"users"|"items"|"comments": [...], "next_max_id": ...}}``
    - flat ``{"users"|"items"|"comments": [...], "next_max_id"|...: ...}``
    """

    if isinstance(payload, list) and len(payload) == 2:
        raw_items, raw_cursor = payload
        items = list(raw_items) if isinstance(raw_items, list) else []
        cursor = _normalise_cursor(raw_cursor)
        return items, cursor

    if isinstance(payload, dict):
        wrapped = payload.get("response")
        inner: dict[str, Any] = wrapped if isinstance(wrapped, dict) else payload
        items_out: list[Any] = []
        for key in ("users", "items", "comments"):
            candidate = inner.get(key)
            if isinstance(candidate, list):
                items_out = candidate
                break
        cursor_out: str | None = None
        for key in ("next_max_id", "next_page_id", "end_cursor", "next_min_id"):
            value = inner.get(key)
            if value is None:
                value = payload.get(key)
            cursor_out = _normalise_cursor(value)
            if cursor_out is not None:
                break
        return items_out, cursor_out

    return [], None


def _normalise_cursor(value: Any) -> str | None:
    """Return `value` as a non-empty cursor string, or None.

    Treats `None` and the empty string as "no more pages", but preserves
    the integer `0` (a legitimate first-page cursor on some endpoints) —
    a plain truthiness check would silently terminate pagination there.
    """

    if value is None:
        return None
    text = str(value)
    if text == "":
        return None
    return text


def _extract_single_list(payload: Any, *, keys: tuple[str, ...]) -> list[Any]:
    """Return the items list from a non-paginated response.

    The shape is normally one of:

    - flat list ``[item, item, ...]``
    - dict with one of ``keys`` mapping to the list
    - dict wrapped under ``response``
    """

    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        wrapped = payload.get("response")
        inner: dict[str, Any] = wrapped if isinstance(wrapped, dict) else payload
        for key in keys:
            value = inner.get(key)
            if isinstance(value, list):
                return value
    return []


class HikerBackend(OSINTBackend):
    """`OSINTBackend` backed by the HikerAPI SDK.

    Tests inject a pre-built ``client`` — a ``hikerapi.AsyncClient`` whose
    underlying ``httpx.AsyncClient`` uses ``MockTransport``. Production code
    constructs its own from ``token`` (and optional ``proxy``).
    """

    def __init__(
        self,
        *,
        token: str | None = None,
        timeout: float = 10.0,
        proxy: str | None = None,
        client: hikerapi.AsyncClient | None = None,
        max_pages: int = DEFAULT_MAX_PAGES,
        retry_decorator: Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]
        | None = None,
    ) -> None:
        if proxy is not None:
            _validate_proxy_url(proxy)
        self._proxy = proxy
        self._max_pages = max_pages

        # When a proxy is configured we replace the SDK's auto-created
        # `httpx.AsyncClient` with a proxied one. The original instance has
        # never been used for a request, but its connection pool / file
        # descriptors still need to be closed — track it so `aclose()` can
        # release it deterministically.
        self._discarded_client: httpx.AsyncClient | None = None
        if client is None:
            sdk = hikerapi.AsyncClient(token=token, timeout=timeout)
            if proxy is not None:
                proxied = httpx.AsyncClient(base_url=sdk._url, timeout=timeout, proxy=proxy)
                proxied.headers.update(sdk._headers)
                self._discarded_client = sdk._client
                sdk._client = proxied
            self._client: hikerapi.AsyncClient = sdk
        else:
            self._client = client

        hooks = self._client._client.event_hooks.setdefault("response", [])
        hooks.append(self._on_response)

        self._apply_retry = retry_decorator if retry_decorator is not None else with_retry()
        self._quota: Quota = Quota.unknown()
        self._last_error: BaseException | None = None

    # ------------------------------------------------------------------ hooks

    async def _on_response(self, response: httpx.Response) -> None:
        rem = _parse_int_header(response.headers, _QUOTA_REMAINING_HEADERS)
        if rem is not None:
            limit = _parse_int_header(response.headers, _QUOTA_LIMIT_HEADERS)
            reset = _parse_int_header(response.headers, _QUOTA_RESET_HEADERS)
            self._quota = Quota.with_remaining(rem, limit=limit, reset_at=reset)
        if response.is_error:
            await response.aread()
            response.raise_for_status()

    # ------------------------------------------------------------------ call

    async def _call(self, factory: Callable[[], Awaitable[T]]) -> T:
        """Invoke a single SDK call with retry + error translation.

        Each invocation builds a fresh ``attempt`` so the retry state is
        per-call, not shared across the backend.
        """

        @self._apply_retry
        async def attempt() -> T:
            try:
                return await factory()
            except httpx.HTTPStatusError as exc:
                raise _translate_http_status(exc) from exc
            except httpx.RequestError as exc:
                raise Transient(f"HikerAPI network error: {exc}") from exc

        try:
            # `_apply_retry` is constructor-injected and erases its argument's
            # generic to `Any`; cast back to `T` since `attempt`'s body is
            # statically `Awaitable[T]`.
            return cast(T, await attempt())
        except _NotFoundError:
            # The internal sentinel — let the caller translate to a typed
            # ProfileNotFound / PostNotFound *with context* and record that
            # final error rather than the internal placeholder.
            raise
        except BackendError as exc:
            self._last_error = exc
            raise

    def _raise_not_found(self, mapped: BackendError, original: BaseException) -> NoReturn:
        """Map an internal `_NotFoundError` to a public typed error and remember it."""

        self._last_error = mapped
        raise mapped from original

    # ---------------------------------------------------------------- profile

    async def resolve_target(self, username: str) -> str:
        try:
            payload = await self._call(lambda: self._client.user_by_username_v2(username=username))
        except _NotFoundError as exc:
            self._raise_not_found(ProfileNotFound(username), exc)
        user = self._unwrap_user(payload, endpoint="user_by_username_v2")
        pk = user.get("pk") or user.get("pk_id")
        if not pk:
            raise SchemaDrift("user_by_username_v2", "pk")
        return str(pk)

    async def get_profile(self, pk: str) -> Profile:
        try:
            payload = await self._call(lambda: self._client.user_by_id_v2(id=pk))
        except _NotFoundError as exc:
            self._raise_not_found(ProfileNotFound(pk), exc)
        user = self._unwrap_user(payload, endpoint="user_by_id_v2")
        return map_profile(user)

    async def get_user_about(self, pk: str) -> dict[str, Any]:
        try:
            payload = await self._call(lambda: self._client.user_about_v1(id=pk))
        except _NotFoundError as exc:
            self._raise_not_found(ProfileNotFound(pk), exc)
        if not isinstance(payload, dict):
            raise SchemaDrift("user_about_v1", "user")
        return payload

    @staticmethod
    def _unwrap_user(payload: Any, *, endpoint: str) -> dict[str, Any]:
        if isinstance(payload, dict):
            inner = payload.get("user")
            if isinstance(inner, dict):
                return inner
            return payload
        raise SchemaDrift(endpoint, "user")

    # ----------------------------------------------------------- iter helpers

    async def _iter_chunks(
        self,
        fetch: Callable[[str | None], Awaitable[Any]],
        *,
        endpoint: str,
        limit: int | None,
        mapper: Callable[[dict[str, Any]], T],
    ) -> AsyncIterator[T]:
        cursor: str | None = None
        pages = 0
        yielded = 0
        while True:
            if pages >= self._max_pages:
                raise BackendError(
                    f"{endpoint}: cursor did not terminate after {self._max_pages} pages"
                )
            payload = await self._call(partial(fetch, cursor))
            pages += 1
            items, next_cursor = _extract_chunk(payload)
            for raw in items:
                if not isinstance(raw, dict):
                    raise SchemaDrift(endpoint, "item")
                yield mapper(raw)
                yielded += 1
                if limit is not None and yielded >= limit:
                    return
            if not next_cursor:
                return
            cursor = next_cursor

    async def _iter_single_page(
        self,
        fetch: Callable[[], Awaitable[Any]],
        *,
        endpoint: str,
        limit: int | None,
        list_keys: tuple[str, ...],
        mapper: Callable[[dict[str, Any]], T],
    ) -> AsyncIterator[T]:
        payload = await self._call(fetch)
        items = _extract_single_list(payload, keys=list_keys)
        for index, raw in enumerate(items):
            if not isinstance(raw, dict):
                raise SchemaDrift(endpoint, "item")
            yield mapper(raw)
            if limit is not None and index + 1 >= limit:
                return

    # ------------------------------------------------------------- iter_posts

    async def iter_user_posts(self, pk: str, *, limit: int | None = None) -> AsyncIterator[Post]:
        async def fetch(cursor: str | None) -> Any:
            return await self._client.user_medias_chunk_v1(user_id=pk, end_cursor=cursor)

        try:
            async for post in self._iter_chunks(
                fetch, endpoint="user_medias_chunk_v1", limit=limit, mapper=map_post
            ):
                yield post
        except _NotFoundError as exc:
            self._raise_not_found(ProfileNotFound(pk), exc)

    async def iter_user_followers(
        self, pk: str, *, limit: int | None = None
    ) -> AsyncIterator[User]:
        async def fetch(cursor: str | None) -> Any:
            return await self._client.user_followers_chunk_v1(user_id=pk, max_id=cursor)

        try:
            async for user in self._iter_chunks(
                fetch, endpoint="user_followers_chunk_v1", limit=limit, mapper=map_user
            ):
                yield user
        except _NotFoundError as exc:
            self._raise_not_found(ProfileNotFound(pk), exc)

    async def iter_user_following(
        self, pk: str, *, limit: int | None = None
    ) -> AsyncIterator[User]:
        async def fetch(cursor: str | None) -> Any:
            return await self._client.user_following_chunk_v1(user_id=pk, max_id=cursor)

        try:
            async for user in self._iter_chunks(
                fetch, endpoint="user_following_chunk_v1", limit=limit, mapper=map_user
            ):
                yield user
        except _NotFoundError as exc:
            self._raise_not_found(ProfileNotFound(pk), exc)

    async def iter_user_tagged(self, pk: str, *, limit: int | None = None) -> AsyncIterator[Post]:
        async def fetch(cursor: str | None) -> Any:
            return await self._client.user_tag_medias_chunk_v1(user_id=pk, max_id=cursor)

        try:
            async for post in self._iter_chunks(
                fetch, endpoint="user_tag_medias_chunk_v1", limit=limit, mapper=map_post
            ):
                yield post
        except _NotFoundError as exc:
            self._raise_not_found(ProfileNotFound(pk), exc)

    async def iter_user_highlights(
        self, pk: str, *, limit: int | None = None
    ) -> AsyncIterator[Highlight]:
        try:
            async for highlight in self._iter_single_page(
                fetch=lambda: self._client.user_highlights_v2(user_id=pk),
                endpoint="user_highlights_v2",
                limit=limit,
                list_keys=("highlights", "items", "tray"),
                mapper=map_highlight,
            ):
                yield highlight
        except _NotFoundError as exc:
            self._raise_not_found(ProfileNotFound(pk), exc)

    async def iter_highlight_items(
        self, highlight_id: str, *, limit: int | None = None
    ) -> AsyncIterator[HighlightItem]:
        try:
            payload = await self._call(lambda: self._client.highlight_by_id_v2(id=highlight_id))
        except _NotFoundError as exc:
            self._raise_not_found(PostNotFound(highlight_id), exc)

        body: Any = payload
        if isinstance(payload, dict):
            inner = payload.get("highlight")
            if isinstance(inner, dict):
                body = inner
        if not isinstance(body, dict):
            raise SchemaDrift("highlight_by_id_v2", "highlight")
        items = body.get("items")
        if not isinstance(items, list):
            raise SchemaDrift("highlight_by_id_v2", "items")

        mapper = partial(map_highlight_item, highlight_pk=str(highlight_id))
        for index, raw in enumerate(items):
            if not isinstance(raw, dict):
                raise SchemaDrift("highlight_by_id_v2", "item")
            yield mapper(raw)
            if limit is not None and index + 1 >= limit:
                return

    async def iter_post_comments(
        self, media_pk: str, *, limit: int | None = None
    ) -> AsyncIterator[Comment]:
        async def fetch(cursor: str | None) -> Any:
            return await self._client.media_comments_chunk_v1(id=media_pk, max_id=cursor)

        mapper = partial(map_comment, media_pk=str(media_pk))
        try:
            async for comment in self._iter_chunks(
                fetch, endpoint="media_comments_chunk_v1", limit=limit, mapper=mapper
            ):
                yield comment
        except _NotFoundError as exc:
            self._raise_not_found(PostNotFound(media_pk), exc)

    async def iter_post_likers(
        self, media_pk: str, *, limit: int | None = None
    ) -> AsyncIterator[User]:
        try:
            async for user in self._iter_single_page(
                fetch=lambda: self._client.media_likers_v1(id=media_pk),
                endpoint="media_likers_v1",
                limit=limit,
                list_keys=("users", "items", "likers"),
                mapper=map_user,
            ):
                yield user
        except _NotFoundError as exc:
            self._raise_not_found(PostNotFound(media_pk), exc)

    async def iter_user_stories(self, pk: str, *, limit: int | None = None) -> AsyncIterator[Story]:
        try:
            async for story in self._iter_single_page(
                fetch=lambda: self._client.user_stories_v2(user_id=pk),
                endpoint="user_stories_v2",
                limit=limit,
                list_keys=("stories", "items", "reels"),
                mapper=map_story,
            ):
                yield story
        except _NotFoundError as exc:
            self._raise_not_found(ProfileNotFound(pk), exc)

    async def get_suggested(self, pk: str) -> list[User]:
        try:
            payload = await self._call(lambda: self._client.user_suggested_profiles_v2(user_id=pk))
        except _NotFoundError as exc:
            self._raise_not_found(ProfileNotFound(pk), exc)
        items = _extract_single_list(payload, keys=("suggested", "users", "items"))
        return [map_user(raw) for raw in items if isinstance(raw, dict)]

    async def iter_hashtag_posts(
        self, tag: str, *, limit: int | None = None
    ) -> AsyncIterator[Post]:
        async def fetch(cursor: str | None) -> Any:
            return await self._client.hashtag_medias_recent_v2(name=tag, page_id=cursor)

        try:
            async for post in self._iter_chunks(
                fetch, endpoint="hashtag_medias_recent_v2", limit=limit, mapper=map_post
            ):
                yield post
        except _NotFoundError as exc:
            mapped = BackendError(f"hashtag not found: #{tag}")
            self._last_error = mapped
            raise mapped from exc

    # -------------------------------------------------------------- bookkeeping

    def get_quota(self) -> Quota:
        return self._quota

    def get_last_error(self) -> BaseException | None:
        return self._last_error

    async def aclose(self) -> None:
        """Close the underlying SDK client."""

        await self._client.aclose()
        if self._discarded_client is not None:
            await self._discarded_client.aclose()
            self._discarded_client = None
