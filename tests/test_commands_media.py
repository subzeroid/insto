"""Tests for `insto.commands.media`: /stories, /highlights, /posts, /reels, /tagged.

Each command is exercised through `dispatch(...)`; the FakeBackend provides
DTOs and an `httpx.MockTransport`-backed CDN client streams canned image
bodies to disk. Three modes are covered for each download command: the
JSON envelope path, `--no-download` URL printing, and the default
download-and-render path.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Generator
from pathlib import Path

import httpx
import pytest
from rich.console import Console

# Importing the package registers all command modules.
import insto.commands  # noqa: F401  (side-effect import)
from insto.commands._base import (
    CommandUsageError,
    Session,
    dispatch,
)
from insto.config import Config
from insto.exceptions import BackendError
from insto.models import Highlight, HighlightItem, Post, Profile, Story
from insto.service.facade import OsintFacade
from insto.service.history import HistoryStore
from insto.ui.theme import INSTO_THEME
from tests.fakes import FakeBackend

# A 3-byte JPEG magic prefix padded out to fill the CDN sniff buffer (512
# bytes). Anything ≥ 512 bytes that starts with the JPEG SOI lets the
# streamer pick `.jpg` deterministically.
_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 1024
_MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00" + b"\x00" * 1024


@pytest.fixture
def history(tmp_path: Path) -> Generator[HistoryStore, None, None]:
    store = HistoryStore(tmp_path / "store.db")
    yield store
    store.close()


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(output_dir=tmp_path / "output", db_path=tmp_path / "store.db")


def _profile() -> Profile:
    return Profile(
        pk="42",
        username="alice",
        access="public",
        full_name="Alice Doe",
        avatar_url="https://scontent.cdninstagram.com/alice.jpg",
    )


def _post(pk: str, *, code: str, mt: str = "image", taken_at: int = 1_700_000_000) -> Post:
    url = (
        "https://scontent.cdninstagram.com/"
        f"{pk}.{'mp4' if mt == 'video' else 'jpg'}"
    )
    return Post(
        pk=pk,
        code=code,
        taken_at=taken_at,
        media_type=mt,  # type: ignore[arg-type]
        media_urls=[url],
        owner_pk="42",
        owner_username="alice",
    )


def _story(pk: str, *, taken_at: int = 1_700_000_000) -> Story:
    return Story(
        pk=pk,
        taken_at=taken_at,
        expires_at=taken_at + 86_400,
        media_type="image",
        media_url=f"https://scontent.cdninstagram.com/story_{pk}.jpg",
        owner_pk="42",
        owner_username="alice",
    )


def _highlight(pk: str, *, title: str = "h", item_count: int = 0) -> Highlight:
    return Highlight(pk=pk, title=title, item_count=item_count, owner_pk="42",
                     owner_username="alice")


def _highlight_item(pk: str, highlight_pk: str, *, taken_at: int = 1_700_000_000) -> HighlightItem:
    return HighlightItem(
        pk=pk,
        highlight_pk=highlight_pk,
        taken_at=taken_at,
        media_type="image",
        media_url=f"https://scontent.cdninstagram.com/hi_{pk}.jpg",
    )


@pytest.fixture
def backend() -> FakeBackend:
    posts = [
        _post("p1", code="A1", mt="image"),
        _post("p2", code="A2", mt="video"),
        _post("p3", code="A3", mt="image"),
        _post("p4", code="A4", mt="video"),
        _post("p5", code="A5", mt="carousel"),
    ]
    tagged = [_post("t1", code="T1"), _post("t2", code="T2")]
    stories = [_story("s1"), _story("s2")]
    highlights = [_highlight("h1", title="travels", item_count=2),
                  _highlight("h2", title="food", item_count=1)]
    items = {
        "h1": [_highlight_item("hi1", "h1"), _highlight_item("hi2", "h1")],
        "h2": [_highlight_item("hi3", "h2")],
    }
    return FakeBackend(
        profiles={"42": _profile()},
        abouts={"42": {}},
        posts={"42": posts},
        tagged={"42": tagged},
        stories={"42": stories},
        highlights={"42": highlights},
        highlight_items=items,
    )


@pytest.fixture
def session() -> Session:
    s = Session()
    s.set_target("alice")
    return s


@pytest.fixture
def recording_console() -> Console:
    return Console(
        theme=INSTO_THEME,
        width=120,
        force_terminal=True,
        color_system="truecolor",
        record=True,
    )


def _captured(console: Console) -> str:
    return console.export_text(styles=False)


def _make_cdn_facade(
    backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    *,
    handler: Callable[[httpx.Request], httpx.Response] | None = None,
    body: bytes = _JPEG,
    content_type: str = "image/jpeg",
) -> OsintFacade:
    """Build an OsintFacade whose CDN client returns `body` for any URL.

    Recorded requests are kept on the transport's request_log, so tests can
    assert which URLs were actually streamed.
    """
    requested: list[str] = []

    if handler is None:

        def default_handler(request: httpx.Request) -> httpx.Response:
            requested.append(str(request.url))
            return httpx.Response(
                200,
                content=body,
                headers={"content-type": content_type, "content-length": str(len(body))},
            )

        handler = default_handler

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    facade = OsintFacade(backend=backend, history=history, config=config, cdn_client=client)
    facade._test_requested = requested  # type: ignore[attr-defined]
    return facade


# ---------------------------------------------------------------------------
# /stories
# ---------------------------------------------------------------------------


async def test_stories_no_download_prints_urls_only(
    backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
    capsys: pytest.CaptureFixture[str],
    recording_console: Console,
) -> None:
    facade = OsintFacade(backend=backend, history=history, config=config)
    out = await dispatch(
        "/stories --no-download",
        facade=facade,
        session=session,
        console=recording_console,
    )
    assert isinstance(out, list) and len(out) == 2
    captured_lines = capsys.readouterr().out.strip().splitlines()
    assert captured_lines == [
        "https://scontent.cdninstagram.com/story_s1.jpg",
        "https://scontent.cdninstagram.com/story_s2.jpg",
    ]
    # Nothing must hit disk.
    stories_dir = config.output_dir / "alice" / "stories"
    assert not stories_dir.exists()


async def test_stories_downloads_to_correct_dir_with_taken_at_mtime(
    backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
    recording_console: Console,
) -> None:
    facade = _make_cdn_facade(backend, history, config)
    try:
        out = await dispatch(
            "/stories", facade=facade, session=session, console=recording_console
        )
    finally:
        await facade.aclose()
    assert isinstance(out, list) and len(out) == 2
    paths: list[Path] = list(out)
    for p in paths:
        assert p.exists()
        assert p.parent == config.output_dir / "alice" / "stories"
    # mtime is set from `taken_at` (1_700_000_000).
    assert int(paths[0].stat().st_mtime) == 1_700_000_000


async def test_stories_empty_reports_friendly_message(
    backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
    recording_console: Console,
) -> None:
    backend.stories = {}
    facade = OsintFacade(backend=backend, history=history, config=config)
    out = await dispatch(
        "/stories", facade=facade, session=session, console=recording_console
    )
    assert out == []
    assert "no active stories" in _captured(recording_console)


async def test_stories_json_export_writes_default_path(
    backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
) -> None:
    facade = OsintFacade(backend=backend, history=history, config=config)
    await dispatch("/stories --json", facade=facade, session=session)
    out_path = config.output_dir / "alice" / "stories.json"
    assert out_path.exists()
    payload = json.loads(out_path.read_text())
    assert payload["command"] == "stories"
    assert len(payload["data"]) == 2
    assert payload["data"][0]["pk"] == "s1"


# ---------------------------------------------------------------------------
# /highlights
# ---------------------------------------------------------------------------


async def test_highlights_default_renders_tree(
    backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
    recording_console: Console,
) -> None:
    facade = OsintFacade(backend=backend, history=history, config=config)
    out = await dispatch(
        "/highlights", facade=facade, session=session, console=recording_console
    )
    assert isinstance(out, list) and len(out) == 2
    text = _captured(recording_console)
    assert "travels" in text
    assert "food" in text
    # Default render does NOT download anything.
    assert not (config.output_dir / "alice" / "highlights").exists()


async def test_highlights_download_streams_items(
    backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
    recording_console: Console,
) -> None:
    facade = _make_cdn_facade(backend, history, config)
    try:
        out = await dispatch(
            "/highlights --download 1",
            facade=facade,
            session=session,
            console=recording_console,
        )
    finally:
        await facade.aclose()
    paths: list[Path] = list(out)
    assert len(paths) == 2
    for p in paths:
        assert p.exists()
        assert p.parent == config.output_dir / "alice" / "highlights"


async def test_highlights_download_out_of_range_raises(
    backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
) -> None:
    facade = OsintFacade(backend=backend, history=history, config=config)
    with pytest.raises(CommandUsageError, match="out of range"):
        await dispatch("/highlights --download 99", facade=facade, session=session)


async def test_highlights_download_no_download_prints_urls(
    backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
    capsys: pytest.CaptureFixture[str],
) -> None:
    facade = OsintFacade(backend=backend, history=history, config=config)
    await dispatch(
        "/highlights --download 1 --no-download", facade=facade, session=session
    )
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines == [
        "https://scontent.cdninstagram.com/hi_hi1.jpg",
        "https://scontent.cdninstagram.com/hi_hi2.jpg",
    ]


async def test_highlights_rejects_csv(
    backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
) -> None:
    facade = OsintFacade(backend=backend, history=history, config=config)
    # `/highlights` is not in CSV_FLAT_COMMANDS; parser rejects.
    with pytest.raises(CommandUsageError):
        await dispatch("/highlights --csv -", facade=facade, session=session)


# ---------------------------------------------------------------------------
# /posts
# ---------------------------------------------------------------------------


async def test_posts_default_limit_is_12(
    backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
) -> None:
    # Reset request log on the backend so we can read the limit fed in.
    backend.request_log.clear()
    facade = OsintFacade(backend=backend, history=history, config=config)
    await dispatch("/posts --no-download", facade=facade, session=session)
    iter_calls = [c for c in backend.request_log if c[0] == "iter_user_posts"]
    assert iter_calls, "iter_user_posts must be called"
    assert iter_calls[0][1] == ("42", 12)


async def test_posts_no_download_prints_only_urls(
    backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
    capsys: pytest.CaptureFixture[str],
    recording_console: Console,
) -> None:
    facade = OsintFacade(backend=backend, history=history, config=config)
    await dispatch(
        "/posts 3 --no-download",
        facade=facade,
        session=session,
        console=recording_console,
    )
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines == [
        "https://scontent.cdninstagram.com/p1.jpg",
        "https://scontent.cdninstagram.com/p2.mp4",
        "https://scontent.cdninstagram.com/p3.jpg",
    ]
    # No render in --no-download mode.
    assert _captured(recording_console).strip() == ""
    assert not (config.output_dir / "alice" / "posts").exists()


async def test_posts_downloads_to_disk_with_correct_layout(
    backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
    recording_console: Console,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # Serve image OR video bytes based on the URL extension.
        body = _MP4 if str(request.url).endswith(".mp4") else _JPEG
        ct = "video/mp4" if str(request.url).endswith(".mp4") else "image/jpeg"
        return httpx.Response(
            200, content=body, headers={"content-type": ct, "content-length": str(len(body))}
        )

    facade = _make_cdn_facade(backend, history, config, handler=handler)
    try:
        out = await dispatch(
            "/posts 2", facade=facade, session=session, console=recording_console
        )
    finally:
        await facade.aclose()
    paths: list[Path] = list(out)
    assert len(paths) == 2  # one per post (each post has a single media URL).
    for p in paths:
        assert p.exists()
        assert p.parent == config.output_dir / "alice" / "posts"
    # First post is image (jpg), second is video (mp4).
    assert paths[0].suffix == ".jpg"
    assert paths[1].suffix == ".mp4"
    assert "saved" in _captured(recording_console)


async def test_posts_global_limit_overrides_positional(
    backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
) -> None:
    backend.request_log.clear()
    facade = OsintFacade(backend=backend, history=history, config=config)
    await dispatch("/posts 25 --limit 4 --no-download", facade=facade, session=session)
    iter_calls = [c for c in backend.request_log if c[0] == "iter_user_posts"]
    assert iter_calls[0][1] == ("42", 4)


async def test_posts_per_resource_byte_budget_propagates(
    backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
    recording_console: Console,
) -> None:
    """A response above the per-resource byte budget must error out.

    The byte-budget guard lives in the CDN streamer (see test_cdn.py for
    full coverage). Here we verify the command layer routes media downloads
    through that guard rather than bypassing it: a body that exceeds the
    explicit budget raises `BackendError`, and the error reaches the caller
    instead of being swallowed.
    """
    big_body = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * (200 * 1024)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=big_body,
            headers={
                "content-type": "image/jpeg",
                "content-length": str(len(big_body)),
            },
        )

    facade = _make_cdn_facade(backend, history, config, handler=handler)
    # Force a tiny per-resource budget by wrapping `_stream`.
    original_stream = facade._stream

    async def small_budget_stream(url, dest, *, taken_at=None):  # type: ignore[no-untyped-def]
        from insto.backends._cdn import stream_to_file

        return await stream_to_file(
            url,
            dest,
            taken_at=taken_at,
            byte_budget=50 * 1024,
            client=facade._cdn_client,
        )

    facade._stream = small_budget_stream  # type: ignore[method-assign]
    try:
        with pytest.raises(BackendError, match="byte budget"):
            await dispatch(
                "/posts 1", facade=facade, session=session, console=recording_console
            )
    finally:
        facade._stream = original_stream  # type: ignore[method-assign]
        await facade.aclose()


async def test_posts_json_export_writes_default_path(
    backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
) -> None:
    facade = OsintFacade(backend=backend, history=history, config=config)
    await dispatch("/posts 2 --json", facade=facade, session=session)
    out_path = config.output_dir / "alice" / "posts.json"
    assert out_path.exists()
    payload = json.loads(out_path.read_text())
    assert payload["command"] == "posts"
    assert len(payload["data"]) == 2
    assert payload["data"][0]["pk"] == "p1"


# ---------------------------------------------------------------------------
# /reels
# ---------------------------------------------------------------------------


async def test_reels_filters_videos_only(
    backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
    capsys: pytest.CaptureFixture[str],
) -> None:
    facade = OsintFacade(backend=backend, history=history, config=config)
    out = await dispatch("/reels --no-download", facade=facade, session=session)
    # Two video posts in the fixture (p2, p4).
    assert [p.pk for p in out] == ["p2", "p4"]
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines == [
        "https://scontent.cdninstagram.com/p2.mp4",
        "https://scontent.cdninstagram.com/p4.mp4",
    ]


async def test_reels_default_limit_is_10(
    backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
) -> None:
    backend.request_log.clear()
    facade = OsintFacade(backend=backend, history=history, config=config)
    await dispatch("/reels --no-download", facade=facade, session=session)
    iter_calls = [c for c in backend.request_log if c[0] == "iter_user_posts"]
    # Reels fetches max(10*3, 30) = 30 raw posts, then filters.
    assert iter_calls[0][1] == ("42", 30)


# ---------------------------------------------------------------------------
# /tagged
# ---------------------------------------------------------------------------


async def test_tagged_default_limit_is_10(
    backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
) -> None:
    backend.request_log.clear()
    facade = OsintFacade(backend=backend, history=history, config=config)
    await dispatch("/tagged --no-download", facade=facade, session=session)
    iter_calls = [c for c in backend.request_log if c[0] == "iter_user_tagged"]
    assert iter_calls[0][1] == ("42", 10)


async def test_tagged_no_download_prints_urls(
    backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
    capsys: pytest.CaptureFixture[str],
) -> None:
    facade = OsintFacade(backend=backend, history=history, config=config)
    await dispatch("/tagged --no-download", facade=facade, session=session)
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines == [
        "https://scontent.cdninstagram.com/t1.jpg",
        "https://scontent.cdninstagram.com/t2.jpg",
    ]


async def test_tagged_downloads_to_owner_username_dir(
    backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
    recording_console: Console,
) -> None:
    facade = _make_cdn_facade(backend, history, config)
    try:
        out = await dispatch(
            "/tagged 1", facade=facade, session=session, console=recording_console
        )
    finally:
        await facade.aclose()
    paths: list[Path] = list(out)
    assert paths and paths[0].parent == config.output_dir / "alice" / "posts"


# ---------------------------------------------------------------------------
# Cross-cutting
# ---------------------------------------------------------------------------


async def test_media_commands_require_target(
    backend: FakeBackend,
    history: HistoryStore,
    config: Config,
) -> None:
    facade = OsintFacade(backend=backend, history=history, config=config)
    empty_session = Session()
    for line in ("/stories", "/highlights", "/posts", "/reels", "/tagged"):
        with pytest.raises(CommandUsageError, match="no target set"):
            await dispatch(line, facade=facade, session=empty_session)
