"""Tests for `insto.service.facade.OsintFacade`.

The facade is exercised against `FakeBackend` (programmatic OSINT data) plus
a real `HistoryStore` rooted at `tmp_path`. CDN downloads are exercised with
an `httpx.MockTransport` so the full streamer code path runs but no network
traffic leaves the process.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from insto.config import Config
from insto.models import (
    Comment,
    Highlight,
    HighlightItem,
    Post,
    Profile,
    Story,
    User,
)
from insto.service.facade import OsintFacade
from insto.service.history import HistoryStore
from tests.fakes import FakeBackend

JPEG_MAGIC = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00"
CDN_HOST = "scontent.cdninstagram.com"


def _pad(magic: bytes, total: int = 1024) -> bytes:
    return magic + b"\x00" * (total - len(magic))


def _profile(pk: str = "42", username: str = "alice", **kw: object) -> Profile:
    return Profile(pk=pk, username=username, access="public", **kw)  # type: ignore[arg-type]


def _post(
    pk: str,
    *,
    code: str | None = None,
    likes: int = 0,
    hashtags: tuple[str, ...] = (),
    mentions: tuple[str, ...] = (),
    location_name: str | None = None,
    media_urls: tuple[str, ...] = (),
    owner_username: str | None = "alice",
    taken_at: int = 1_700_000_000,
) -> Post:
    return Post(
        pk=pk,
        code=code or pk,
        taken_at=taken_at,
        media_type="image",
        like_count=likes,
        hashtags=list(hashtags),
        mentions=list(mentions),
        location_name=location_name,
        media_urls=list(media_urls),
        owner_username=owner_username,
    )


def _user(pk: str, username: str | None = None) -> User:
    return User(pk=pk, username=username or f"u{pk}")


@pytest.fixture
def history(tmp_path: Path) -> HistoryStore:
    s = HistoryStore(tmp_path / "store.db")
    yield s
    s.close()


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(output_dir=tmp_path / "output", db_path=tmp_path / "store.db")


@pytest.fixture
def backend() -> FakeBackend:
    profile = _profile(pk="42", username="alice", biography="hi", follower_count=10)
    posts = [
        _post("p1", likes=10, hashtags=("python",), location_name="Berlin"),
        _post("p2", likes=20, hashtags=("python", "osint"), mentions=("bob",)),
        _post("p3", likes=5, hashtags=("osint",), location_name="Berlin"),
    ]
    return FakeBackend(
        profiles={"42": profile},
        abouts={"42": {"is_verified": False}},
        posts={"42": posts},
        followers={"42": [_user("100", "bob"), _user("101", "carol"), _user("102", "dave")]},
        following={"42": [_user("101", "carol"), _user("103", "eve"), _user("100", "bob")]},
        suggested={"42": [_user("200", "similar")]},
        tagged={"42": [_post("t1", owner_username="bob"), _post("t2", owner_username="bob")]},
        comments={
            "p1": [
                Comment(
                    pk="c1",
                    media_pk="p1",
                    user_pk="100",
                    user_username="bob",
                    text="hi",
                    created_at=1,
                ),
                Comment(
                    pk="c2",
                    media_pk="p1",
                    user_pk="101",
                    user_username="carol",
                    text="hi",
                    created_at=2,
                ),
            ],
            "p2": [
                Comment(
                    pk="c3",
                    media_pk="p2",
                    user_pk="100",
                    user_username="bob",
                    text="ok",
                    created_at=3,
                ),
            ],
            "p3": [],
        },
        likers={"p1": [_user("100", "bob"), _user("101", "carol")]},
        stories={
            "42": [
                Story(
                    pk="s1",
                    taken_at=1_700_000_000,
                    expires_at=1_700_000_100,
                    media_type="image",
                    media_url=f"https://{CDN_HOST}/s1",
                    owner_username="alice",
                )
            ]
        },
        highlights={"42": [Highlight(pk="h1", title="Trip", item_count=2)]},
        highlight_items={
            "h1": [
                HighlightItem(
                    pk="i1",
                    highlight_pk="h1",
                    taken_at=1_700_000_000,
                    media_type="image",
                    media_url=f"https://{CDN_HOST}/i1",
                )
            ]
        },
        hashtag_posts={"python": [_post("hp1"), _post("hp2")]},
    )


@pytest.fixture
def facade(backend: FakeBackend, history: HistoryStore, config: Config) -> OsintFacade:
    return OsintFacade(backend=backend, history=history, config=config)


# ----------------------------------------------------------------- target / pk


async def test_resolve_pk_caches_for_session(facade: OsintFacade, backend: FakeBackend) -> None:
    pk1 = await facade.resolve_pk("alice")
    pk2 = await facade.resolve_pk("@alice")
    assert pk1 == pk2 == "42"
    resolves = [c for c in backend.request_log if c[0] == "resolve_target"]
    assert len(resolves) == 1


async def test_clear_target_cache_drops_entry(facade: OsintFacade, backend: FakeBackend) -> None:
    await facade.resolve_pk("alice")
    facade.clear_target_cache("@alice")
    await facade.resolve_pk("alice")
    resolves = [c for c in backend.request_log if c[0] == "resolve_target"]
    assert len(resolves) == 2

    await facade.resolve_pk("alice")  # warm again
    facade.clear_target_cache(None)  # nuke whole cache
    await facade.resolve_pk("alice")
    resolves = [c for c in backend.request_log if c[0] == "resolve_target"]
    assert len(resolves) == 3


# ----------------------------------------------------------------------- profile


async def test_profile_info_returns_profile_and_about(facade: OsintFacade) -> None:
    profile, about = await facade.profile_info("alice")
    assert profile.pk == "42"
    assert about == {"is_verified": False}


async def test_profile_returns_dto(facade: OsintFacade) -> None:
    p = await facade.profile("alice")
    assert p.username == "alice"


# ------------------------------------------------------------------------- media


async def test_user_posts_respects_limit(facade: OsintFacade) -> None:
    posts = await facade.user_posts("alice", limit=2)
    assert [p.pk for p in posts] == ["p1", "p2"]


async def test_user_stories(facade: OsintFacade) -> None:
    stories = await facade.user_stories("alice")
    assert len(stories) == 1


async def test_user_highlights_and_items(facade: OsintFacade) -> None:
    h = await facade.user_highlights("alice")
    assert h[0].pk == "h1"
    items = await facade.highlight_items("h1")
    assert items[0].pk == "i1"


# ----------------------------------------------------------------------- network


async def test_followers_followings_similar(facade: OsintFacade) -> None:
    fr = await facade.followers("alice", limit=2)
    fg = await facade.followings("alice", limit=2)
    sim = await facade.similar("alice")
    assert [u.username for u in fr] == ["bob", "carol"]
    assert [u.username for u in fg] == ["carol", "eve"]
    assert sim[0].username == "similar"


async def test_mutuals_intersects(facade: OsintFacade) -> None:
    res = await facade.mutuals("alice")
    names = {u.username for u in res.items}
    assert names == {"bob", "carol"}


# --------------------------------------------------------------------- analytics


async def test_hashtags_top(facade: OsintFacade) -> None:
    top = await facade.hashtags("alice", limit=10)
    counts = dict(top.items)
    assert counts == {"python": 2, "osint": 2}
    assert top.kind == "hashtags"


async def test_mentions_top(facade: OsintFacade) -> None:
    top = await facade.mentions("alice", limit=10)
    assert dict(top.items) == {"bob": 1}


async def test_locations_top(facade: OsintFacade) -> None:
    top = await facade.locations("alice", limit=10)
    assert dict(top.items) == {"Berlin": 2}


async def test_likes_aggregate(facade: OsintFacade) -> None:
    stats = await facade.likes("alice", limit=10)
    assert stats.total_likes == 35
    assert stats.top_posts[0] == ("p2", 20)


async def test_wcommented_merges_across_posts(facade: OsintFacade) -> None:
    top = await facade.wcommented("alice", limit=10)
    counts = dict(top.items)
    assert counts == {"bob": 2, "carol": 1}


async def test_wtagged_owners(facade: OsintFacade) -> None:
    top = await facade.wtagged("alice", limit=10)
    assert dict(top.items) == {"bob": 2}


# -------------------------------------------------------------------- post-level


async def test_post_comments_and_likers(facade: OsintFacade) -> None:
    cs = await facade.post_comments("p1", limit=10)
    ls = await facade.post_likers("p1", limit=10)
    assert {c.pk for c in cs} == {"c1", "c2"}
    assert {u.username for u in ls} == {"bob", "carol"}


async def test_hashtag_posts_strips_hash(facade: OsintFacade) -> None:
    posts = await facade.hashtag_posts("#python", limit=10)
    assert {p.pk for p in posts} == {"hp1", "hp2"}


# -------------------------------------------------------------------- snapshots


async def test_snapshot_persists_and_diff(
    facade: OsintFacade, backend: FakeBackend, history: HistoryStore
) -> None:
    await facade.snapshot("alice")
    diff = await facade.diff("alice")
    assert diff["first_seen"] is False
    assert diff["changes"] == {}

    backend.profiles["42"] = _profile(
        pk="42", username="alice", biography="updated", follower_count=11
    )
    diff = await facade.diff("alice")
    assert "biography" in diff["changes"]
    assert diff["changes"]["biography"]["new"] == "updated"


# --------------------------------------------------------------------- exports


async def test_export_json_default_path(facade: OsintFacade, config: Config) -> None:
    out = facade.export_json({"hello": "world"}, command="info", target="@alice")
    assert out == config.output_dir / "alice" / "info.json"
    assert out is not None
    payload = json.loads(out.read_text())
    assert payload["_schema"] == "insto.v1"
    assert payload["data"] == {"hello": "world"}


async def test_export_csv_default_path(facade: OsintFacade, config: Config) -> None:
    out = facade.export_csv(
        [{"pk": "1", "username": "bob"}, {"pk": "2", "username": "carol"}],
        command="followers",
        target="@alice",
    )
    assert out == config.output_dir / "alice" / "followers.csv"
    assert out is not None
    text = out.read_text()
    assert "pk,username" in text.splitlines()[0]
    assert "bob" in text


async def test_export_csv_rejects_non_flat(facade: OsintFacade) -> None:
    with pytest.raises(ValueError, match="not a flat-row command"):
        facade.export_csv([{"x": 1}], command="info", target="@alice")


# -------------------------------------------------------------------- downloads


async def test_download_propic_streams_to_facade_dir(
    backend: FakeBackend, history: HistoryStore, config: Config
) -> None:
    profile = _profile(pk="42", username="alice", avatar_url=f"https://{CDN_HOST}/a")

    body = _pad(JPEG_MAGIC)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=body)

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, follow_redirects=False)
    facade = OsintFacade(backend=backend, history=history, config=config, cdn_client=client)
    try:
        out = await facade.download_propic(profile)
    finally:
        await facade.aclose()

    assert out == config.output_dir / "alice" / "propic" / "42.jpg"
    assert out.read_bytes() == body


async def test_download_propic_no_avatar_returns_none(facade: OsintFacade) -> None:
    profile = _profile(pk="42", username="alice", avatar_url=None)
    assert await facade.download_propic(profile) is None


async def test_download_post_media_multi_url(
    backend: FakeBackend, history: HistoryStore, config: Config
) -> None:
    body = _pad(JPEG_MAGIC)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    facade = OsintFacade(backend=backend, history=history, config=config, cdn_client=client)
    post = _post(
        "p1",
        media_urls=(f"https://{CDN_HOST}/a", f"https://{CDN_HOST}/b"),
    )
    try:
        outs = await facade.download_post_media(post)
    finally:
        await facade.aclose()
    assert len(outs) == 2
    assert outs[0].name == "p1.jpg"
    assert outs[1].name == "p1_1.jpg"


# ------------------------------------------------------------------- record_log


async def test_record_command_persists_to_history(
    facade: OsintFacade, history: HistoryStore
) -> None:
    await facade.record_command("/info", "@alice")
    targets = history.recent_targets(5)
    assert "@alice" in targets


# ----------------------------------------------------------------------- quota


async def test_quota_and_last_error_passthrough(facade: OsintFacade, backend: FakeBackend) -> None:
    from insto.exceptions import RateLimited

    backend.errors.get_profile = RateLimited(1.0)
    with pytest.raises(RateLimited):
        await facade.profile("alice")
    err = facade.last_error()
    assert isinstance(err, RateLimited)
    q = facade.quota()
    assert q is backend.quota
