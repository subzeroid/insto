"""`AiograpiBackend` — `OSINTBackend` over [aiograpi](https://github.com/subzeroid/aiograpi).

Optional dependency: this module is imported only inside
`make_backend("aiograpi", ...)`. If the user did not install
`insto[aiograpi]`, the import there fails fast with a friendly
"`pip install insto[aiograpi]`" hint.

Login is *lazy*: the constructor stores credentials but does not hit
Instagram. The first command that needs the network triggers
`_ensure_logged_in`, which:

  1. tries to load a persisted session from `session_path` (default
     `~/.insto/aiograpi.session.json`, mode 0600);
  2. falls back to `client.login(username, password, totp_seed=...)`
     and dumps the fresh session for next launch.

aiograpi raises a wide spectrum of exceptions. `_translate` collapses
them into the insto taxonomy in `insto/exceptions.py` so the command
layer never sees a raw aiograpi exception. New aiograpi exception
classes are caught by the `ClientError` fallback as `Transient`.

All `OSINTBackend` methods are implemented. `get_suggested(pk)` and
`iter_user_tagged(pk)` need aiograpi ≥ 0.8.0 (the release that added
`chaining` / `fetch_suggestion_details` and exposed `usertag_medias_v1`).
The `[aiograpi]` extra in `pyproject.toml` enforces this minimum.
"""

from __future__ import annotations

import contextlib
import logging
import os
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from insto.backends._base import OSINTBackend
from insto.exceptions import (
    AuthInvalid,
    BackendError,
    Banned,
    PostNotFound,
    ProfileNotFound,
    ProfilePrivate,
    RateLimited,
    SchemaDrift,
    Transient,
)
from insto.models import Comment, Highlight, HighlightItem, Post, Profile, Quota, Story, User
from insto.service.metrics import Metrics, MetricsSnapshot

log = logging.getLogger("insto.backends.aiograpi")


def _ensure_secure_perms(path: Path) -> None:
    """Make sure the saved session file ends up `0600`. Failure is fatal —
    we don't want a world-readable cookie cache."""
    try:
        os.chmod(path, 0o600)
    except OSError as exc:  # pragma: no cover — perms vary across filesystems
        raise BackendError(f"could not lock down session file {path}: {exc}") from exc


def _translate(exc: BaseException) -> BackendError:
    """Map an aiograpi exception to the insto taxonomy."""
    # Late import: aiograpi.exceptions is only available when the optional
    # dependency is installed. We're already inside `make_backend("aiograpi")`
    # by the time this runs, so the import always succeeds here — but keep
    # it inside the function so the module-level import of `aiograpi.py`
    # itself can be tested without the package.
    from aiograpi import exceptions as ae

    if isinstance(exc, ae.UserNotFound | ae.ClientNotFoundError):
        # aiograpi.exceptions.UserNotFound carries the username on `.username`.
        username = getattr(exc, "username", None) or str(exc)
        return ProfileNotFound(str(username))
    if isinstance(exc, ae.MediaNotFound):
        return PostNotFound(str(exc))
    if isinstance(exc, ae.PrivateAccount):
        username = getattr(exc, "username", None) or str(exc)
        return ProfilePrivate(str(username))
    if isinstance(exc, ae.InvalidTargetUser):
        # Raised by `chaining()` when Instagram says "Not eligible for
        # chaining." — a per-target permanent answer, not a transient.
        # We expose it as a plain BackendError so the command layer
        # surfaces a clear "no suggestions available" instead of
        # treating it as a generic schema/transient failure.
        return BackendError(f"target not eligible: {exc}")
    if isinstance(exc, ae.BadPassword | ae.BadCredentials):
        return AuthInvalid("aiograpi rejected the credentials")
    if isinstance(
        exc,
        ae.ChallengeRequired
        | ae.CheckpointRequired
        | ae.CaptchaChallengeRequired
        | ae.ClientLoginRequired
        | ae.LoginRequired
        | ae.ReloginAttemptExceeded,
    ):
        return AuthInvalid(f"aiograpi auth challenge: {type(exc).__name__}")
    if isinstance(exc, ae.AccountSuspended | ae.FeedbackRequired):
        return Banned(f"aiograpi: {type(exc).__name__} ({exc})")
    if isinstance(
        exc,
        ae.RateLimitError | ae.PleaseWaitFewMinutes | ae.ClientThrottledError,
    ):
        # aiograpi's PleaseWaitFewMinutes wraps Instagram's "wait a few
        # minutes before you try again" — treat as 60s minimum retry.
        return RateLimited(retry_after=getattr(exc, "retry_after", None) or 60.0)
    if isinstance(exc, ae.ClientForbiddenError):
        return Banned(
            "Instagram returned 403 (forbidden). Likely login-walled or "
            "target-restricted. Other commands should still work."
        )
    if isinstance(
        exc,
        ae.ClientConnectionError
        | ae.ClientRequestTimeout
        | ae.ClientIncompleteReadError
        | ae.ClientJSONDecodeError,
    ):
        return Transient(f"aiograpi network: {type(exc).__name__}")
    if isinstance(exc, ae.ClientError):
        return Transient(f"aiograpi: {type(exc).__name__}: {exc}")
    if isinstance(exc, SchemaDrift):
        return exc
    if isinstance(exc, BackendError):
        return exc
    return BackendError(f"aiograpi: {type(exc).__name__}: {exc}")


# ----------------------------------------------------------------------------


class AiograpiBackend(OSINTBackend):
    """`OSINTBackend` over `aiograpi.Client`.

    Constructed lazily via `make_backend("aiograpi", ...)`; the actual
    Instagram login fires on the first command that needs it.
    """

    name = "aiograpi"

    def __init__(
        self,
        *,
        username: str,
        password: str,
        totp_seed: str | None = None,
        session_path: Path | None = None,
        proxy: str | None = None,
    ) -> None:
        # Late import: the whole module already requires aiograpi to be
        # installed (we're called only from make_backend), so this is a
        # straight `import`, not a try/except gate.
        from aiograpi import Client

        self._username = username
        self._password = password
        self._totp_seed = totp_seed
        self._session_path = session_path
        self._proxy = proxy

        self._client: Client = Client()
        if proxy:
            self._client.set_proxy(proxy)

        self._logged_in = False
        self._last_error: BaseException | None = None
        self._drift_count = 0
        self._metrics = Metrics()

    # ------------------------------------------------------------------ auth

    async def _ensure_logged_in(self) -> None:
        """Load saved session if available, else log in fresh."""
        if self._logged_in:
            return
        if self._session_path is not None and self._session_path.exists():
            try:
                self._client.load_settings(self._session_path)
                # `load_settings` does not validate the session — touch a
                # cheap endpoint to confirm we're still authenticated.
                # `account_info()` is the conventional "am I logged in?".
                # Failure falls through to fresh login.
                await self._client.account_info()
                self._logged_in = True
                return
            except Exception as exc:
                log.info("aiograpi: stale session, logging in fresh: %s", exc)
        try:
            await self._client.login(
                self._username,
                self._password,
                verification_code=self._totp_seed or "",
            )
        except Exception as exc:  # pragma: no cover — network/credential dependent
            self._last_error = exc
            raise _translate(exc) from exc
        self._logged_in = True
        if self._session_path is not None:
            self._session_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._client.dump_settings(self._session_path)
            _ensure_secure_perms(self._session_path)

    # ------------------------------------------------------------------ exec

    async def _call(self, factory: Any) -> Any:
        """Single-shot wrapper: ensure login, run, translate any error.

        Records latency and error type into ``self._metrics`` so /health
        can show backend health without a separate logging path. The
        ``_ensure_logged_in`` step is *outside* the timer — login latency
        is a one-time setup cost we don't want skewing the per-call p50.
        """
        await self._ensure_logged_in()
        start = time.monotonic()
        try:
            result = await factory()
        except BackendError as exc:
            self._metrics.record((time.monotonic() - start) * 1000.0, error=exc)
            self._last_error = exc
            raise
        except Exception as exc:
            mapped = _translate(exc)
            self._metrics.record((time.monotonic() - start) * 1000.0, error=mapped)
            self._last_error = exc
            raise mapped from exc
        self._metrics.record((time.monotonic() - start) * 1000.0, error=None)
        return result

    # ------------------------------------------------------------------ resolve

    async def resolve_target(self, username: str) -> str:
        """Resolve `@username` to its stable pk.

        Two-stage chain: aiograpi's stock `user_id_from_username` first
        (public-host, sometimes returns HTML when IG decides to challenge
        the unauthenticated request), then `user_web_profile_info_v1` —
        a private-host route that carries the logged-in session and
        is meaningfully more reliable when the public path JSON-decode
        fails. Both surfaces return the same canonical pk.
        """
        try:
            pk = await self._call(lambda: self._client.user_id_from_username(username))
            return str(pk)
        except ProfileNotFound:
            raise
        except BackendError:
            data = await self._call(lambda: self._client.user_web_profile_info_v1(username))
            user = data.get("user") if isinstance(data, dict) else None
            pk = (user or {}).get("id") or (user or {}).get("pk")
            if not pk:
                raise
            return str(pk)

    # ------------------------------------------------------------------ profile

    async def get_profile(self, pk: str) -> Profile:
        from insto.backends._aiograpi_map import map_profile

        user = await self._call(lambda: self._client.user_info(pk))
        return map_profile(user)

    async def get_user_about(self, pk: str) -> dict[str, Any]:
        from insto.backends._aiograpi_map import about_payload

        user = await self._call(lambda: self._client.user_info(pk))
        return about_payload(user)

    # ------------------------------------------------------------------ posts

    async def iter_user_posts(self, pk: str, *, limit: int | None = None) -> AsyncIterator[Post]:
        from insto.backends._aiograpi_map import map_post

        amount = int(limit) if limit else 0  # 0 = "as many as the API will give"
        items = await self._call(lambda: self._client.user_medias(int(pk), amount=amount))
        for raw in items:
            try:
                yield map_post(raw)
            except SchemaDrift as drift:
                self._drift_count += 1
                self._last_error = drift
                raise

    async def iter_user_tagged(self, pk: str, *, limit: int | None = None) -> AsyncIterator[Post]:
        from insto.backends._aiograpi_map import map_post

        amount = int(limit) if limit else 0
        items = await self._call(lambda: self._client.usertag_medias_v1(int(pk), amount=amount))
        for raw in items:
            try:
                yield map_post(raw)
            except SchemaDrift as drift:
                self._drift_count += 1
                self._last_error = drift
                raise

    # ------------------------------------------------------------------ stories

    async def iter_user_stories(self, pk: str, *, limit: int | None = None) -> AsyncIterator[Story]:
        from insto.backends._aiograpi_map import map_story

        items = await self._call(lambda: self._client.user_stories(str(pk)))
        if limit:
            items = items[: int(limit)]
        for raw in items:
            yield map_story(raw)

    # ------------------------------------------------------------------ highlights

    async def iter_user_highlights(
        self, pk: str, *, limit: int | None = None
    ) -> AsyncIterator[Highlight]:
        from insto.backends._aiograpi_map import map_highlight

        items = await self._call(lambda: self._client.user_highlights(int(pk)))
        if limit:
            items = items[: int(limit)]
        for raw in items:
            yield map_highlight(raw)

    async def iter_highlight_items(
        self, highlight_id: str, *, limit: int | None = None
    ) -> AsyncIterator[HighlightItem]:
        from insto.backends._aiograpi_map import map_highlight_item

        info = await self._call(lambda: self._client.highlight_info(highlight_id))
        items = getattr(info, "items", None) or []
        if limit:
            items = items[: int(limit)]
        for raw in items:
            yield map_highlight_item(raw, highlight_pk=str(highlight_id))

    # ------------------------------------------------------------------ network

    async def iter_user_followers(
        self, pk: str, *, limit: int | None = None
    ) -> AsyncIterator[User]:
        from insto.backends._aiograpi_map import map_user_short

        amount = int(limit) if limit else 0
        users = await self._call(lambda: self._client.user_followers(str(pk), amount=amount))
        # aiograpi returns a {pk → UserShort} dict for paginated endpoints.
        items = users.values() if isinstance(users, dict) else users
        for raw in items:
            yield map_user_short(raw)

    async def iter_user_following(
        self, pk: str, *, limit: int | None = None
    ) -> AsyncIterator[User]:
        from insto.backends._aiograpi_map import map_user_short

        amount = int(limit) if limit else 0
        users = await self._call(lambda: self._client.user_following(str(pk), amount=amount))
        items = users.values() if isinstance(users, dict) else users
        for raw in items:
            yield map_user_short(raw)

    async def get_suggested(self, pk: str) -> list[User]:
        """Two-stage chaining: private `chaining()` first, then public-graphql
        `user_related_profiles_gql` as a fallback.

        Why both: Instagram refuses the private `chaining` endpoint
        (``InvalidTargetUser`` "Not eligible for chaining.") for many
        high-profile / locked-down targets — that's the path the IG
        app uses, and IG actively limits third-party scraping of it.
        The public graphql ``edge_chaining`` field still works for many
        of those same targets, just at a less reliable rate-limited
        surface. Trying both gives us a meaningfully higher hit rate
        for `/similar` than either alone.

        Order matters: private first because it's the canonical
        surface and returns richer rows (more fields the user table
        knows how to render). Public-graphql second because it's
        less reliable.
        """
        # `chaining()` returns raw IG dicts; aiograpi's `extract_user_short`
        # reconciles the `id` / `pk_id` / `pk` aliases. `user_related_profiles_gql`
        # already returns proper `UserShort` models so it skips that wrap.
        # The `Any` cast keeps mypy quiet on both CI (where aiograpi is not
        # installed and the import returns `Any`) and locally (where the
        # extractor is real but untyped, triggering `no-untyped-call`).
        from typing import Any as _Any

        from aiograpi.extractors import extract_user_short as _extract_user_short

        from insto.backends._aiograpi_map import map_user_short

        extract_user_short: _Any = _extract_user_short

        # --- private chaining (preferred) ---
        try:
            payload = await self._call(lambda: self._client.chaining(str(pk)))
        except BackendError as primary:
            # InvalidTargetUser is the typed signal for "Not eligible
            # for chaining" — try the public graphql path instead.
            # Banned (403) is also a per-target IG refusal, same fallback.
            if not isinstance(primary, (Banned,)) and "not eligible" not in str(primary).lower():
                raise
            return await self._suggested_via_graphql(pk, primary)

        if isinstance(payload, dict):
            users = payload.get("users") or []
            if users:
                out: list[User] = []
                for raw in users:
                    try:
                        out.append(map_user_short(extract_user_short(raw)))
                    except SchemaDrift as drift:
                        self._drift_count += 1
                        self._last_error = drift
                        raise
                return out

        # Empty private result — fall through to graphql before giving up.
        return await self._suggested_via_graphql(pk, None)

    async def _suggested_via_graphql(self, pk: str, primary: BackendError | None) -> list[User]:
        """Public-graphql `edge_chaining` fallback for `/similar`.

        `user_related_profiles_gql` returns `List[UserShort]` (already
        Pydantic), so no `extract_user_short` wrap is needed here.
        Returns whatever the graphql edge gives us; on its own failure
        we propagate `primary` (the original chaining error) if any,
        so the caller gets the *first* rejection signal — that's the
        more actionable error 90% of the time.
        """
        from insto.backends._aiograpi_map import map_user_short

        try:
            shorts = await self._call(lambda: self._client.user_related_profiles_gql(str(pk)))
        except BackendError as fallback_exc:
            if primary is not None:
                raise primary from fallback_exc
            raise
        out: list[User] = []
        for raw in shorts or []:
            try:
                out.append(map_user_short(raw))
            except SchemaDrift as drift:
                self._drift_count += 1
                self._last_error = drift
                raise
        return out

    # ------------------------------------------------------------------ comments + likers

    async def iter_post_comments(
        self, media_pk: str, *, limit: int | None = None
    ) -> AsyncIterator[Comment]:
        from insto.backends._aiograpi_map import map_comment

        amount = int(limit) if limit else 0
        items = await self._call(lambda: self._client.media_comments(str(media_pk), amount=amount))
        for raw in items:
            yield map_comment(raw, media_pk=str(media_pk))

    async def iter_post_likers(
        self, media_pk: str, *, limit: int | None = None
    ) -> AsyncIterator[User]:
        from insto.backends._aiograpi_map import map_user_short

        users = await self._call(lambda: self._client.media_likers(str(media_pk)))
        if limit:
            users = users[: int(limit)]
        for raw in users:
            yield map_user_short(raw)

    # ------------------------------------------------------------------ hashtag

    async def iter_hashtag_posts(
        self, tag: str, *, limit: int | None = None
    ) -> AsyncIterator[Post]:
        from insto.backends._aiograpi_map import map_post

        amount = int(limit) if limit else 30
        items = await self._call(lambda: self._client.hashtag_medias_recent(tag, amount=amount))
        for raw in items:
            yield map_post(raw)

    # ------------------------------------------------------------------ search

    async def iter_search_users(
        self, query: str, *, limit: int | None = None
    ) -> AsyncIterator[User]:
        # `fbsearch_accounts_v2` returns raw IG dicts (not Pydantic models),
        # so route them through aiograpi's own `extract_user_short` first —
        # it handles the `id` / `pk_id` → `pk` reconciliation that the SERP
        # response uses, then yields a `UserShort` Pydantic model that
        # `map_user_short` can read via attribute access. Skipping this
        # wrapping makes `map_user_short` raise SchemaDrift on `pk`.
        # `Any` cast: see `get_suggested` for the rationale.
        from typing import Any as _Any

        from aiograpi.extractors import extract_user_short as _extract_user_short

        from insto.backends._aiograpi_map import map_user_short

        extract_user_short: _Any = _extract_user_short

        if limit is not None and limit <= 0:
            limit = None
        cursor: str | None = None
        yielded = 0
        while True:

            async def fetch(c: str | None = cursor) -> Any:
                return await self._client.fbsearch_accounts_v2(query=query, page_token=c)

            payload = await self._call(fetch)
            if not isinstance(payload, dict):
                return
            users = payload.get("users") or []
            for raw in users:
                yield map_user_short(extract_user_short(raw))
                yielded += 1
                if limit is not None and yielded >= limit:
                    return
            if not payload.get("has_more"):
                return
            next_cursor = payload.get("page_token") or payload.get("next_page_token")
            if not next_cursor:
                return
            cursor = str(next_cursor)

    # ------------------------------------------------------------------ bookkeeping

    def get_quota(self) -> Quota:
        # aiograpi has no quota concept — the cost is account-ban risk.
        return Quota.unknown()

    def get_last_error(self) -> BaseException | None:
        return self._last_error

    def get_schema_drift_count(self) -> int:
        return self._drift_count

    def get_metrics(self) -> MetricsSnapshot:
        return self._metrics.snapshot()

    async def aclose(self) -> None:
        """Close the underlying httpx client.

        aiograpi reuses one `httpx.AsyncClient` for the lifetime of the
        `Client` instance. Closing it here releases the connection pool
        cleanly so a long-running REPL doesn't leak sockets.
        """
        client = getattr(self._client, "private", None) or getattr(self._client, "_session", None)
        if client is None:
            return
        aclose = getattr(client, "aclose", None)
        if aclose is not None:
            with contextlib.suppress(Exception):  # pragma: no cover — best effort cleanup
                await aclose()
