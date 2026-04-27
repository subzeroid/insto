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

import re
import sqlite3
from pathlib import Path
from typing import IO, Any

import httpx

from insto.backends._base import OSINTBackend
from insto.backends._cdn import stream_to_file
from insto.config import Config
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
        posts = await self.user_posts(username, limit=limit)
        merged: list[Comment] = []
        for post in posts:
            async for c in self.backend.iter_post_comments(post.pk, limit=limit):
                merged.append(c)
                if len(merged) >= limit:
                    break
            if len(merged) >= limit:
                break
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
        posts = await self.user_posts(username, limit=post_limit)
        snap = self.history.snapshot_from_profile(profile, [p.pk for p in posts])
        await self.history.add_snapshot_async(snap)
        return profile

    async def diff(self, username: str) -> dict[str, Any]:
        """Compare the current profile of `username` against last snapshot."""
        profile = await self.profile(username)
        return self.history.diff(profile.pk, profile)

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
        return await stream_to_file(
            url,
            dest,
            taken_at=taken_at,
            client=self._cdn_client,
        )

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
