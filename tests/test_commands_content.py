"""Tests for `insto.commands.content`: /hashtags, /mentions, /locations,
/captions, /likes.

Every content-analysis command operates over a bounded post window with a
default of 50. Tests assert:

- the window header (`Hashtags from @alice (last 50 posts):`) is always
  printed in the human-readable mode,
- top counts are correct and sorted by (count desc, key asc),
- `--limit N` overrides the default window and is propagated to the
  backend (so the analytics function never sees more than N posts),
- JSON / CSV exports round-trip with the expected shape,
- empty windows print a clear "no posts to analyze" message instead of
  rendering an empty table.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Generator
from pathlib import Path

import pytest
from rich.console import Console

# Importing the package registers all command modules.
import insto.commands  # noqa: F401  (side-effect import)
from insto.commands._base import (
    CommandUsageError,
    Session,
    dispatch,
)
from insto.commands.content import CONTENT_DEFAULT_WINDOW
from insto.config import Config
from insto.models import Post, Profile
from insto.service.analytics import LikesStats, TopList
from insto.service.facade import OsintFacade
from insto.service.history import HistoryStore
from insto.ui.theme import INSTO_THEME
from tests.fakes import FakeBackend


@pytest.fixture
def history(tmp_path: Path) -> Generator[HistoryStore, None, None]:
    store = HistoryStore(tmp_path / "store.db")
    yield store
    store.close()


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(output_dir=tmp_path / "output", db_path=tmp_path / "store.db")


def _profile() -> Profile:
    return Profile(pk="42", username="alice", access="public", full_name="Alice Doe")


def _post(
    pk: str,
    *,
    code: str | None = None,
    caption: str = "",
    hashtags: list[str] | None = None,
    mentions: list[str] | None = None,
    location_name: str | None = None,
    like_count: int = 0,
    comment_count: int = 0,
    taken_at: int = 1_700_000_000,
) -> Post:
    return Post(
        pk=pk,
        code=code or f"C{pk}",
        taken_at=taken_at,
        media_type="image",
        caption=caption,
        like_count=like_count,
        comment_count=comment_count,
        location_name=location_name,
        hashtags=list(hashtags or []),
        mentions=list(mentions or []),
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


@pytest.fixture
def content_backend() -> FakeBackend:
    """Posts crafted so each analytic axis has a clear winner."""
    posts = [
        _post(
            "p1",
            caption="trip in Paris",
            hashtags=["travel", "Paris"],
            mentions=["bob"],
            location_name="Paris, France",
            like_count=100,
        ),
        _post(
            "p2",
            caption="more travel",
            hashtags=["travel", "summer"],
            mentions=["bob", "carol"],
            location_name="Paris, France",
            like_count=300,
        ),
        _post(
            "p3",
            caption="lunch",
            hashtags=["food"],
            mentions=["carol"],
            location_name="Berlin",
            like_count=200,
        ),
        _post(
            "p4",
            caption="no caption here",
            hashtags=[],
            mentions=[],
            location_name=None,
            like_count=50,
        ),
    ]
    return FakeBackend(profiles={"42": _profile()}, posts={"42": posts}, page_size=2)


# ---------------------------------------------------------------------------
# /hashtags
# ---------------------------------------------------------------------------


async def test_hashtags_default_window_50(
    content_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
) -> None:
    content_backend.request_log.clear()
    facade = OsintFacade(backend=content_backend, history=history, config=config)
    await dispatch("/hashtags", facade=facade, session=session)
    iter_calls = [c for c in content_backend.request_log if c[0] == "iter_user_posts"]
    assert iter_calls and iter_calls[0][1] == ("42", CONTENT_DEFAULT_WINDOW)


async def test_hashtags_window_header_renders(
    content_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
    recording_console: Console,
) -> None:
    facade = OsintFacade(backend=content_backend, history=history, config=config)
    await dispatch(
        "/hashtags",
        facade=facade,
        session=session,
        console=recording_console,
    )
    text = _captured(recording_console)
    assert "Hashtags from @alice (last 50 posts):" in text
    # `travel` appears in two posts → top
    assert "travel" in text
    assert "food" in text


async def test_hashtags_top_counts_correct(
    content_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
) -> None:
    facade = OsintFacade(backend=content_backend, history=history, config=config)
    out = await dispatch("/hashtags", facade=facade, session=session)
    assert isinstance(out, TopList)
    assert out.kind == "hashtags"
    # `travel` (2) > food / paris / summer (1) — ties broken by key asc.
    assert out.items[0] == ("travel", 2)
    keys = {k for k, _ in out.items}
    assert keys == {"travel", "food", "paris", "summer"}


async def test_hashtags_limit_overrides_default(
    content_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
    recording_console: Console,
) -> None:
    content_backend.request_log.clear()
    facade = OsintFacade(backend=content_backend, history=history, config=config)
    await dispatch(
        "/hashtags --limit 2",
        facade=facade,
        session=session,
        console=recording_console,
    )
    iter_calls = [c for c in content_backend.request_log if c[0] == "iter_user_posts"]
    assert iter_calls[0][1] == ("42", 2)
    text = _captured(recording_console)
    # Window header reflects the override.
    assert "(last 2 posts)" in text


async def test_hashtags_json_export(
    content_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
) -> None:
    facade = OsintFacade(backend=content_backend, history=history, config=config)
    await dispatch("/hashtags --json", facade=facade, session=session)
    out_path = config.output_dir / "alice" / "hashtags.json"
    assert out_path.exists()
    payload = json.loads(out_path.read_text())
    assert payload["command"] == "hashtags"
    assert payload["_schema"] == "insto.v1"
    data = payload["data"]
    assert data["target"] == "alice"
    assert data["window"] == CONTENT_DEFAULT_WINDOW
    assert data["analyzed"] == 4
    items = {it["key"]: it["count"] for it in data["items"]}
    assert items == {"travel": 2, "food": 1, "paris": 1, "summer": 1}


async def test_hashtags_csv_export(
    content_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
) -> None:
    facade = OsintFacade(backend=content_backend, history=history, config=config)
    await dispatch("/hashtags --csv", facade=facade, session=session)
    out_path = config.output_dir / "alice" / "hashtags.csv"
    rows = list(csv.DictReader(out_path.read_text().splitlines()))
    assert {r["hashtag"] for r in rows} == {"travel", "food", "paris", "summer"}
    assert rows[0]["rank"] == "1"
    assert rows[0]["hashtag"] == "travel"
    assert rows[0]["count"] == "2"


async def test_hashtags_empty_window_prints_message(
    history: HistoryStore,
    config: Config,
    session: Session,
    recording_console: Console,
) -> None:
    backend = FakeBackend(profiles={"42": _profile()}, posts={"42": []})
    facade = OsintFacade(backend=backend, history=history, config=config)
    out = await dispatch(
        "/hashtags",
        facade=facade,
        session=session,
        console=recording_console,
    )
    assert isinstance(out, TopList)
    assert out.empty is True
    text = _captured(recording_console)
    assert "Hashtags from @alice (last 50 posts):" in text
    assert "no posts to analyze for @alice" in text


# ---------------------------------------------------------------------------
# /mentions
# ---------------------------------------------------------------------------


async def test_mentions_top_and_header(
    content_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
    recording_console: Console,
) -> None:
    facade = OsintFacade(backend=content_backend, history=history, config=config)
    out = await dispatch(
        "/mentions",
        facade=facade,
        session=session,
        console=recording_console,
    )
    assert isinstance(out, TopList)
    # bob: 2, carol: 2 — tie broken by key asc → bob first.
    assert out.items[0] == ("bob", 2)
    text = _captured(recording_console)
    assert "Mentions from @alice (last 50 posts):" in text


async def test_mentions_limit_propagated(
    content_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
) -> None:
    content_backend.request_log.clear()
    facade = OsintFacade(backend=content_backend, history=history, config=config)
    await dispatch("/mentions --limit 1", facade=facade, session=session)
    iter_calls = [c for c in content_backend.request_log if c[0] == "iter_user_posts"]
    assert iter_calls[0][1] == ("42", 1)


# ---------------------------------------------------------------------------
# /locations
# ---------------------------------------------------------------------------


async def test_locations_top_and_header(
    content_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
    recording_console: Console,
) -> None:
    facade = OsintFacade(backend=content_backend, history=history, config=config)
    out = await dispatch(
        "/locations",
        facade=facade,
        session=session,
        console=recording_console,
    )
    assert isinstance(out, TopList)
    assert out.items[0] == ("Paris, France", 2)
    keys = {k for k, _ in out.items}
    assert keys == {"Paris, France", "Berlin"}
    text = _captured(recording_console)
    assert "Locations from @alice (last 50 posts):" in text


async def test_locations_csv_export(
    content_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
) -> None:
    facade = OsintFacade(backend=content_backend, history=history, config=config)
    await dispatch("/locations --csv", facade=facade, session=session)
    out_path = config.output_dir / "alice" / "locations.csv"
    rows = list(csv.DictReader(out_path.read_text().splitlines()))
    assert rows[0]["location"] == "Paris, France"
    assert rows[0]["count"] == "2"


# ---------------------------------------------------------------------------
# /captions
# ---------------------------------------------------------------------------


async def test_captions_default_window_and_header(
    content_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
    recording_console: Console,
) -> None:
    content_backend.request_log.clear()
    facade = OsintFacade(backend=content_backend, history=history, config=config)
    out = await dispatch(
        "/captions",
        facade=facade,
        session=session,
        console=recording_console,
    )
    assert isinstance(out, list)
    assert [p.pk for p in out] == ["p1", "p2", "p3", "p4"]
    iter_calls = [c for c in content_backend.request_log if c[0] == "iter_user_posts"]
    assert iter_calls and iter_calls[0][1] == ("42", CONTENT_DEFAULT_WINDOW)
    text = _captured(recording_console)
    assert "Captions from @alice (last 50 posts):" in text
    assert "trip in Paris" in text


async def test_captions_limit_overrides(
    content_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
    recording_console: Console,
) -> None:
    content_backend.request_log.clear()
    facade = OsintFacade(backend=content_backend, history=history, config=config)
    out = await dispatch(
        "/captions --limit 2",
        facade=facade,
        session=session,
        console=recording_console,
    )
    assert [p.pk for p in out] == ["p1", "p2"]
    iter_calls = [c for c in content_backend.request_log if c[0] == "iter_user_posts"]
    assert iter_calls[0][1] == ("42", 2)
    text = _captured(recording_console)
    assert "(last 2 posts)" in text


async def test_captions_csv_export(
    content_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
) -> None:
    facade = OsintFacade(backend=content_backend, history=history, config=config)
    await dispatch("/captions --limit 2 --csv", facade=facade, session=session)
    out_path = config.output_dir / "alice" / "captions.csv"
    rows = list(csv.DictReader(out_path.read_text().splitlines()))
    assert len(rows) == 2
    assert rows[0]["code"] == "Cp1"
    assert rows[0]["caption"] == "trip in Paris"
    assert rows[0]["like_count"] == "100"


async def test_captions_json_export(
    content_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
) -> None:
    facade = OsintFacade(backend=content_backend, history=history, config=config)
    await dispatch("/captions --json", facade=facade, session=session)
    out_path = config.output_dir / "alice" / "captions.json"
    payload = json.loads(out_path.read_text())
    assert payload["command"] == "captions"
    data = payload["data"]
    assert data["window"] == CONTENT_DEFAULT_WINDOW
    assert data["analyzed"] == 4
    assert [item["code"] for item in data["items"]] == ["Cp1", "Cp2", "Cp3", "Cp4"]


async def test_captions_empty_window(
    history: HistoryStore,
    config: Config,
    session: Session,
    recording_console: Console,
) -> None:
    backend = FakeBackend(profiles={"42": _profile()}, posts={"42": []})
    facade = OsintFacade(backend=backend, history=history, config=config)
    out = await dispatch(
        "/captions",
        facade=facade,
        session=session,
        console=recording_console,
    )
    assert out == []
    text = _captured(recording_console)
    assert "Captions from @alice (last 50 posts):" in text
    assert "no posts to analyze for @alice" in text


# ---------------------------------------------------------------------------
# /likes
# ---------------------------------------------------------------------------


async def test_likes_aggregates_and_header(
    content_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
    recording_console: Console,
) -> None:
    facade = OsintFacade(backend=content_backend, history=history, config=config)
    out = await dispatch(
        "/likes",
        facade=facade,
        session=session,
        console=recording_console,
    )
    assert isinstance(out, LikesStats)
    assert out.total_likes == 100 + 300 + 200 + 50
    assert out.analyzed == 4
    text = _captured(recording_console)
    assert "Likes from @alice (last 50 posts):" in text
    # Top-1 is p2 (300 likes).
    assert out.top_posts[0] == ("Cp2", 300)


async def test_likes_limit_overrides(
    content_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
    recording_console: Console,
) -> None:
    content_backend.request_log.clear()
    facade = OsintFacade(backend=content_backend, history=history, config=config)
    await dispatch(
        "/likes --limit 2",
        facade=facade,
        session=session,
        console=recording_console,
    )
    iter_calls = [c for c in content_backend.request_log if c[0] == "iter_user_posts"]
    assert iter_calls[0][1] == ("42", 2)
    text = _captured(recording_console)
    assert "(last 2 posts)" in text


async def test_likes_csv_export(
    content_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
) -> None:
    facade = OsintFacade(backend=content_backend, history=history, config=config)
    await dispatch("/likes --csv", facade=facade, session=session)
    out_path = config.output_dir / "alice" / "likes.csv"
    rows = list(csv.DictReader(out_path.read_text().splitlines()))
    assert rows[0]["code"] == "Cp2"
    assert rows[0]["like_count"] == "300"
    assert rows[0]["total_likes"] == "650"


async def test_likes_json_export(
    content_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
) -> None:
    facade = OsintFacade(backend=content_backend, history=history, config=config)
    await dispatch("/likes --json", facade=facade, session=session)
    out_path = config.output_dir / "alice" / "likes.json"
    payload = json.loads(out_path.read_text())
    data = payload["data"]
    assert data["total_likes"] == 650
    assert data["analyzed"] == 4
    assert data["top_posts"][0] == {"code": "Cp2", "like_count": 300}


async def test_likes_empty_window(
    history: HistoryStore,
    config: Config,
    session: Session,
    recording_console: Console,
) -> None:
    backend = FakeBackend(profiles={"42": _profile()}, posts={"42": []})
    facade = OsintFacade(backend=backend, history=history, config=config)
    out = await dispatch(
        "/likes",
        facade=facade,
        session=session,
        console=recording_console,
    )
    assert isinstance(out, LikesStats)
    assert out.empty is True
    text = _captured(recording_console)
    assert "Likes from @alice (last 50 posts):" in text
    assert "no posts to analyze for @alice" in text


# ---------------------------------------------------------------------------
# Cross-cutting
# ---------------------------------------------------------------------------


async def test_content_commands_require_target(
    content_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
) -> None:
    facade = OsintFacade(backend=content_backend, history=history, config=config)
    empty_session = Session()
    for line in (
        "/hashtags",
        "/mentions",
        "/locations",
        "/captions",
        "/likes",
    ):
        with pytest.raises(CommandUsageError, match="no target set"):
            await dispatch(line, facade=facade, session=empty_session)


@pytest.mark.parametrize("name", ["hashtags", "mentions", "locations", "captions", "likes"])
async def test_content_commands_registered(name: str) -> None:
    from insto.commands._base import COMMANDS

    assert name in COMMANDS
    assert COMMANDS[name].csv is True
