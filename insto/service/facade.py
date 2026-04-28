"""`OsintFacade` — single entry point for the command layer.

Commands talk only to the facade; the facade composes backend +
analytics + exporter + history + CDN-streamer into thin per-command
methods. Each method here is intentionally 5-15 lines: if a method
grows past that, the business logic belongs in `analytics.py` or
`history.py`, not in the facade.

Two kinds of state live on the facade for the lifetime of a session:

- `backend`, `history`, `config` — concrete dependencies wired by the
  caller (CLI / REPL bootstrap).
- `_pk_cache` — `username -> pk` cache so a single REPL session never
  re-resolves the active target. The cache is cleared by `clear_target`
  (or by handing in a different `target`).

CDN downloads (`download_propic`, `download_post_media`,
`download_story`, `download_highlight_item`) live on the facade — the
command layer never imports `_cdn` directly. Each download method
returns the path actually written so the command can render it.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import sqlite3
from pathlib import Path
from typing import IO, Any

import httpx

from insto.backends._base import OSINTBackend
from insto.backends._cdn import DEFAULT_BYTE_BUDGET as CDN_PER_RESOURCE_BUDGET
from insto.backends._cdn import stream_to_file
from insto.config import Config
from insto.exceptions import BackendError
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
from insto.service import analytics
from insto.service.exporter import (
    default_export_path,
    to_csv,
    to_json,
    to_maltego_csv,
)
from insto.service.history import HistoryStore
from insto.service.watch import WatchManager


class OsintFacade:
    """Stateful facade composing backend + service helpers for one session."""

    # Spec §12: 5 GB cap on the total bytes a single command run may stream
    # from the CDN (on top of the per-resource 500 MB budget enforced inside
    # `_cdn.stream_to_file`). Reset by `dispatch()` before each command.
    DEFAULT_COMMAND_BYTE_BUDGET: int = 5 * 1024 * 1024 * 1024

    def __init__(
        self,
        *,
        backend: OSINTBackend,
        history: HistoryStore,
        config: Config,
        cdn_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.backend = backend
        self.history = history
        self.config = config
        self._cdn_client = cdn_client
        self._pk_cache: dict[str, str] = {}
        self.watches = WatchManager()
        self._command_byte_budget: int = self.DEFAULT_COMMAND_BYTE_BUDGET
        self._command_bytes_used: int = 0
        self._budget_lock = asyncio.Lock()

    def reset_command_budget(self, total: int | None = None) -> None:
        """Start a fresh per-command CDN byte budget.

        Called from `dispatch()` once per command. Subsequent CDN downloads
        are tracked against this budget; exceeding it raises `BackendError`
        from the next `_stream` call.
        """
        self._command_byte_budget = total if total is not None else self.DEFAULT_COMMAND_BYTE_BUDGET
        self._command_bytes_used = 0

    @property
    def command_bytes_remaining(self) -> int:
        return max(0, self._command_byte_budget - self._command_bytes_used)

    @property
    def db_connection(self) -> sqlite3.Connection:
        """Underlying sqlite connection — exposed for `/health` only."""
        return self.history._conn  # by design, single-conn-per-session

    # --------------------------------------------------------------- target

    async def resolve_pk(self, username: str) -> str:
        """Return `pk` for `username`, caching for the session."""
        cleaned = username.lstrip("@")
        cached = self._pk_cache.get(cleaned)
        if cached is not None:
            return cached
        pk = await self.backend.resolve_target(cleaned)
        self._pk_cache[cleaned] = pk
        return pk

    def clear_target_cache(self, username: str | None = None) -> None:
        """Drop `username` from the pk cache (or the whole cache if None)."""
        if username is None:
            self._pk_cache.clear()
        else:
            self._pk_cache.pop(username.lstrip("@"), None)

    # --------------------------------------------------------------- profile

    async def profile_info(self, username: str) -> tuple[Profile, dict[str, Any]]:
        """Fetch full `Profile` plus `user_about` payload."""
        pk = await self.resolve_pk(username)
        profile = await self.backend.get_profile(pk)
        about = await self.backend.get_user_about(pk)
        return profile, about

    async def profile(self, username: str) -> Profile:
        """Fetch only the `Profile` DTO (no `user_about`)."""
        pk = await self.resolve_pk(username)
        return await self.backend.get_profile(pk)

    # ----------------------------------------------------------------- media

    async def user_posts(self, username: str, *, limit: int = 12) -> list[Post]:
        pk = await self.resolve_pk(username)
        return [p async for p in self.backend.iter_user_posts(pk, limit=limit)]

    async def user_tagged(self, username: str, *, limit: int = 12) -> list[Post]:
        pk = await self.resolve_pk(username)
        return [p async for p in self.backend.iter_user_tagged(pk, limit=limit)]

    async def user_stories(self, username: str, *, limit: int | None = None) -> list[Story]:
        pk = await self.resolve_pk(username)
        return [s async for s in self.backend.iter_user_stories(pk, limit=limit)]

    async def user_highlights(self, username: str, *, limit: int | None = None) -> list[Highlight]:
        pk = await self.resolve_pk(username)
        return [h async for h in self.backend.iter_user_highlights(pk, limit=limit)]

    async def highlight_items(
        self, highlight_id: str, *, limit: int | None = None
    ) -> list[HighlightItem]:
        return [i async for i in self.backend.iter_highlight_items(highlight_id, limit=limit)]

    # --------------------------------------------------------------- network

    async def followers(self, username: str, *, limit: int = 50) -> list[User]:
        pk = await self.resolve_pk(username)
        return [u async for u in self.backend.iter_user_followers(pk, limit=limit)]

    async def followings(self, username: str, *, limit: int = 50) -> list[User]:
        pk = await self.resolve_pk(username)
        return [u async for u in self.backend.iter_user_following(pk, limit=limit)]

    async def similar(self, username: str) -> list[User]:
        pk = await self.resolve_pk(username)
        return await self.backend.get_suggested(pk)

    async def search_users(self, query: str, *, limit: int = 50) -> list[User]:
        """Free-text user search. Empty query is rejected upstream."""
        return [u async for u in self.backend.iter_search_users(query, limit=limit)]

    async def mutuals(
        self, username: str, *, follower_limit: int = 1000, following_limit: int = 1000
    ) -> analytics.MutualsResult:
        pk = await self.resolve_pk(username)
        followers = [u async for u in self.backend.iter_user_followers(pk, limit=follower_limit)]
        followings = [u async for u in self.backend.iter_user_following(pk, limit=following_limit)]
        return analytics.compute_mutuals(
            followers,
            followings,
            target=username,
            follower_limit=follower_limit,
            following_limit=following_limit,
        )

    # ------------------------------------------------------------- analytics

    async def hashtags(self, username: str, *, limit: int = 50) -> analytics.TopList:
        posts = await self.user_posts(username, limit=limit)
        return analytics.extract_hashtags(posts, target=username, limit=limit)

    async def mentions(self, username: str, *, limit: int = 50) -> analytics.TopList:
        posts = await self.user_posts(username, limit=limit)
        return analytics.extract_mentions(posts, target=username, limit=limit)

    async def locations(self, username: str, *, limit: int = 50) -> analytics.TopList:
        posts = await self.user_posts(username, limit=limit)
        return analytics.extract_locations(posts, target=username, limit=limit)

    async def likes(self, username: str, *, limit: int = 50) -> analytics.LikesStats:
        posts = await self.user_posts(username, limit=limit)
        return analytics.aggregate_likes(posts, target=username, limit=limit)

    async def wcommented(self, username: str, *, limit: int = 50) -> analytics.TopList:
        # `limit` is the *post* window only. Per-post comments are bounded by
        # the facade default (50/post) — the same cap `/comments` aggregate
        # mode uses — so the spec §9 bounded-window guarantee holds without
        # `--limit` doubling as a per-post comment cap.
        posts = await self.user_posts(username, limit=limit)
        merged: list[Comment] = []
        for post in posts:
            merged.extend(await self.post_comments(post.pk))
        return analytics.count_wcommented(merged, target=username, limit=limit)

    async def wtagged(self, username: str, *, limit: int = 50) -> analytics.TopList:
        pk = await self.resolve_pk(username)
        tagged = [p async for p in self.backend.iter_user_tagged(pk, limit=limit)]
        return analytics.count_wtagged(tagged, target=username, limit=limit)

    # ---------------------------------------------------------- interactions

    async def post_comments(self, media_pk: str, *, limit: int = 50) -> list[Comment]:
        return [c async for c in self.backend.iter_post_comments(media_pk, limit=limit)]

    async def post_likers(self, media_pk: str, *, limit: int = 50) -> list[User]:
        return [u async for u in self.backend.iter_post_likers(media_pk, limit=limit)]

    # --------------------------------------------------------------- hashtags

    async def hashtag_posts(self, tag: str, *, limit: int = 50) -> list[Post]:
        cleaned = tag.lstrip("#")
        return [p async for p in self.backend.iter_hashtag_posts(cleaned, limit=limit)]

    # ----------------------------------------------------------------- watch

    async def snapshot(self, username: str, *, post_limit: int = 12) -> Profile:
        """Capture a fresh snapshot for `username` and persist it."""
        profile = await self.profile(username)
        await self._persist_snapshot(profile, post_limit=post_limit)
        return profile

    async def diff(self, username: str) -> dict[str, Any]:
        """Compare the current profile of `username` against last snapshot."""
        profile = await self.profile(username)
        return self.history.diff(profile.pk, profile)

    async def diff_and_snapshot(self, username: str, *, post_limit: int = 12) -> dict[str, Any]:
        """One-pass watch tick: fetch profile once, diff, then persist snapshot."""
        profile = await self.profile(username)
        diff = self.history.diff(profile.pk, profile)
        await self._persist_snapshot(profile, post_limit=post_limit)
        return diff

    async def _persist_snapshot(self, profile: Profile, *, post_limit: int) -> None:
        posts = await self.user_posts(profile.username, limit=post_limit)
        snap = self.history.snapshot_from_profile(profile, [p.pk for p in posts])
        await self.history.add_snapshot_async(snap)

    # ------------------------------------------------------------------ ops

    def quota(self) -> Quota:
        return self.backend.get_quota()

    def last_error(self) -> BaseException | None:
        return self.backend.get_last_error()

    # --------------------------------------------------------------- exports

    def export_json(
        self,
        payload: Any,
        *,
        command: str,
        target: str | None,
        dest: Path | IO[bytes] | None = None,
    ) -> Path | None:
        """Write a versioned JSON envelope. `dest=None` → default path under `output_dir`."""
        target_dest = (
            dest
            if dest is not None
            else default_export_path(
                command=command, target=target, ext="json", output_dir=self.config.output_dir
            )
        )
        return to_json(payload, command=command, target=target, dest=target_dest)

    def export_csv(
        self,
        rows: list[dict[str, Any]],
        *,
        command: str,
        target: str | None,
        dest: Path | IO[bytes] | None = None,
    ) -> Path | None:
        target_dest = (
            dest
            if dest is not None
            else default_export_path(
                command=command, target=target, ext="csv", output_dir=self.config.output_dir
            )
        )
        return to_csv(rows, command=command, target=target, dest=target_dest)

    def export_maltego(
        self,
        rows: list[dict[str, Any]],
        *,
        command: str,
        entity_type: str,
        target: str | None,
        dest: Path | IO[bytes] | None = None,
    ) -> Path | None:
        """Write Maltego entity-import CSV. Default path: `<cmd>.maltego.csv`."""
        target_dest = (
            dest
            if dest is not None
            else default_export_path(
                command=command,
                target=target,
                ext="maltego.csv",
                output_dir=self.config.output_dir,
            )
        )
        return to_maltego_csv(rows, entity_type=entity_type, dest=target_dest)

    # -------------------------------------------------------------- downloads

    async def download_propic(self, profile: Profile) -> Path | None:
        """Download a profile's avatar into `<output>/<user>/propic/<pk>.<ext>`."""
        if not profile.avatar_url:
            return None
        dest_dir = self._media_dir(profile.username, "propic")
        return await self._stream(profile.avatar_url, dest_dir / _safe_pk(profile.pk))

    async def download_post_media(self, post: Post) -> list[Path]:
        """Download every media URL of `post` into `<output>/<owner>/posts/`."""
        owner = post.owner_username or "_"
        dest_dir = self._media_dir(owner, "posts")
        pk = _safe_pk(post.pk)
        out: list[Path] = []
        for idx, url in enumerate(post.media_urls):
            base = dest_dir / (pk if idx == 0 else f"{pk}_{idx}")
            out.append(await self._stream(url, base, taken_at=post.taken_at))
        return out

    async def download_story(self, story: Story) -> Path:
        """Download a story into `<output>/<owner>/stories/`."""
        owner = story.owner_username or "_"
        dest_dir = self._media_dir(owner, "stories")
        return await self._stream(
            story.media_url, dest_dir / _safe_pk(story.pk), taken_at=story.taken_at
        )

    async def download_highlight_item(self, item: HighlightItem, *, owner_username: str) -> Path:
        """Download a highlight item into `<output>/<owner>/highlights/`."""
        dest_dir = self._media_dir(owner_username, "highlights")
        return await self._stream(
            item.media_url, dest_dir / _safe_pk(item.pk), taken_at=item.taken_at
        )

    def _media_dir(self, username: str, kind: str) -> Path:
        cleaned = _safe_path_segment(username.lstrip("@")) or "_"
        path = self.config.output_dir / cleaned / kind
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def _stream(
        self,
        url: str,
        dest: Path,
        *,
        taken_at: float | int | None = None,
    ) -> Path:
        # Per-command byte budget (spec §12: 5 GB / command). The
        # per-resource 500 MB cap still lives inside `_cdn.stream_to_file`.
        # Concurrent `_stream` calls (e.g. /batch fan-out) race on the
        # counter, so reserve pessimistically before the await and
        # reconcile after — protected by a lock so two callers can't
        # observe the same `remaining` and double-spend.
        async with self._budget_lock:
            remaining = self._command_byte_budget - self._command_bytes_used
            if remaining <= 0:
                raise BackendError(
                    "command exceeded byte budget "
                    f"{self._command_byte_budget} (used {self._command_bytes_used})"
                )
            reservation = min(CDN_PER_RESOURCE_BUDGET, remaining)
            self._command_bytes_used += reservation
        try:
            path = await stream_to_file(
                url,
                dest,
                taken_at=taken_at,
                client=self._cdn_client,
                byte_budget=reservation,
            )
        except BaseException:
            async with self._budget_lock:
                self._command_bytes_used -= reservation
            raise
        # Default to the full reservation if stat() fails: bytes were
        # written (stream_to_file returned a path), so refunding to 0
        # would silently disable the per-command byte budget across
        # repeated stat failures. Pessimistic accounting is correct here.
        actual = reservation
        with contextlib.suppress(OSError):
            actual = path.stat().st_size
        async with self._budget_lock:
            # actual ≤ reservation by construction — the streamer enforces
            # `byte_budget=reservation` so anything larger would have raised.
            self._command_bytes_used += actual - reservation
        return path

    # ------------------------------------------------------------------- log

    async def record_command(self, cmd: str, target: str | None) -> None:
        """Persist a single REPL/CLI invocation in the history table."""
        await self.history.record_command_async(cmd, target)

    async def aclose(self) -> None:
        """Release backend / cdn / watch resources (history is owned by the caller)."""
        await self.watches.cancel_all()
        if self._cdn_client is not None:
            await self._cdn_client.aclose()
            self._cdn_client = None
        with contextlib.suppress(Exception):
            await self.backend.aclose()


_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _safe_path_segment(value: str) -> str:
    """Return `value` if it is safe to use as a single filesystem path segment.

    Defense-in-depth: backend DTO fields (e.g. `Profile.username`,
    `Post.owner_username`, `Post.pk`) flow into `<output>/<user>/...`
    paths. Instagram constrains usernames server-side, but the rest of the
    codebase rejects path-meta characters at the user-input boundary; we
    apply the same guard at the backend boundary so a hostile / drifted
    payload can never escape `output_dir`. Returns `""` if the value is not
    safe — the caller should substitute `_` in that case.
    """
    if not value or value in (".", ".."):
        return ""
    if not _SAFE_SEGMENT_RE.fullmatch(value):
        return ""
    return value


def _safe_pk(value: str) -> str:
    """Return a filesystem-safe pk segment, substituting `_` if drifted."""
    return _safe_path_segment(value) or "_"
