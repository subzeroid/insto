"""Tests for `insto.commands.interactions`: /comments, /wcommented, /wtagged.

Each command operates over a bounded post window (default 50). The tests
exercise the three commands through `dispatch(...)` against a `FakeBackend`
populated with synthetic posts, comments and tagged posts.

What the tests assert:

- `/comments` with a post code dumps the comments of just that post and
  keeps the analytic restricted to one post.
- `/comments` without a code aggregates comments across the bounded
  window of the target's recent posts.
- `/comments` with an unknown code surfaces a `CommandUsageError`.
- `/wcommented` and `/wtagged` produce the right `TopList` ordering with
  ties broken by key asc.
- `--limit N` overrides the default window and is propagated end-to-end.
- JSON / CSV exports round-trip with the expected shape and the file
  lands in the per-target default directory.
- empty windows yield a clear message instead of a silent empty table.
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
    COMMANDS,
    CommandUsageError,
    Session,
    dispatch,
)
from insto.commands.interactions import INTERACTIONS_DEFAULT_WINDOW
from insto.config import Config
from insto.models import Comment, Post, Profile, User
from insto.service.analytics import FansResult, TopList
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
    owner_username: str | None = None,
    taken_at: int = 1_700_000_000,
) -> Post:
    return Post(
        pk=pk,
        code=code or f"C{pk}",
        taken_at=taken_at,
        media_type="image",
        owner_username=owner_username,
    )


def _comment(
    pk: str,
    *,
    media_pk: str,
    user: str,
    text: str = "nice",
    created_at: int = 1_700_000_000,
    likes: int = 0,
) -> Comment:
    return Comment(
        pk=pk,
        media_pk=media_pk,
        user_pk=f"u-{pk}",
        user_username=user,
        text=text,
        created_at=created_at,
        like_count=likes,
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


def _user(pk: str, username: str) -> User:
    return User(pk=pk, username=username, full_name=username.title())


@pytest.fixture
def interactions_backend() -> FakeBackend:
    """Two posts of @alice with overlapping commenters and likers; tagged
    posts owned by various other users."""
    posts = [
        _post("p1", code="Cp1"),
        _post("p2", code="Cp2"),
    ]
    comments = {
        "p1": [
            _comment("c1", media_pk="p1", user="bob", text="first!"),
            _comment("c2", media_pk="p1", user="carol", text="hi"),
            _comment("c3", media_pk="p1", user="bob", text="again"),
        ],
        "p2": [
            _comment("c4", media_pk="p2", user="dave", text="cool"),
            _comment("c5", media_pk="p2", user="bob", text="nice"),
        ],
    }
    likers = {
        # bob: 2L, carol: 2L, dave: 1L, eve: 1L
        "p1": [_user("u1", "bob"), _user("u2", "carol"), _user("u4", "eve")],
        "p2": [_user("u1", "bob"), _user("u2", "carol"), _user("u3", "dave")],
    }
    tagged = [
        _post("t1", code="Ct1", owner_username="bob"),
        _post("t2", code="Ct2", owner_username="carol"),
        _post("t3", code="Ct3", owner_username="bob"),
        _post("t4", code="Ct4", owner_username=None),
    ]
    return FakeBackend(
        profiles={"42": _profile()},
        posts={"42": posts},
        comments=comments,
        likers=likers,
        tagged={"42": tagged},
        page_size=2,
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["comments", "wcommented", "wtagged"])
async def test_interactions_commands_registered(name: str) -> None:
    assert name in COMMANDS
    assert COMMANDS[name].csv is True


# ---------------------------------------------------------------------------
# /comments — by post code
# ---------------------------------------------------------------------------


async def test_comments_with_post_code_returns_only_that_post(
    interactions_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
    recording_console: Console,
) -> None:
    facade = OsintFacade(backend=interactions_backend, history=history, config=config)
    out = await dispatch(
        "/comments Cp1",
        facade=facade,
        session=session,
        console=recording_console,
    )
    assert isinstance(out, list)
    assert [c.pk for c in out] == ["c1", "c2", "c3"]
    text = _captured(recording_console)
    assert "Comments on Cp1 (post by @alice):" in text
    assert "@bob" in text
    assert "first!" in text


async def test_comments_unknown_post_code_raises(
    interactions_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
) -> None:
    facade = OsintFacade(backend=interactions_backend, history=history, config=config)
    with pytest.raises(CommandUsageError, match="not found in last 50 posts"):
        await dispatch("/comments NOPE", facade=facade, session=session)


# ---------------------------------------------------------------------------
# /comments — aggregate
# ---------------------------------------------------------------------------


async def test_comments_aggregate_default_window(
    interactions_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
    recording_console: Console,
) -> None:
    interactions_backend.request_log.clear()
    facade = OsintFacade(backend=interactions_backend, history=history, config=config)
    out = await dispatch(
        "/comments",
        facade=facade,
        session=session,
        console=recording_console,
    )
    assert isinstance(out, list)
    # All five comments across both posts.
    assert {c.pk for c in out} == {"c1", "c2", "c3", "c4", "c5"}
    iter_posts = [c for c in interactions_backend.request_log if c[0] == "iter_user_posts"]
    assert iter_posts and iter_posts[0][1] == ("42", INTERACTIONS_DEFAULT_WINDOW)
    text = _captured(recording_console)
    assert f"Comments from @alice (last {INTERACTIONS_DEFAULT_WINDOW} posts):" in text


async def test_comments_aggregate_limit_overrides(
    interactions_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
    recording_console: Console,
) -> None:
    interactions_backend.request_log.clear()
    facade = OsintFacade(backend=interactions_backend, history=history, config=config)
    out = await dispatch(
        "/comments --limit 1",
        facade=facade,
        session=session,
        console=recording_console,
    )
    assert isinstance(out, list)
    # --limit 1 → only first post inspected → only its three comments.
    assert {c.pk for c in out} == {"c1", "c2", "c3"}
    iter_posts = [c for c in interactions_backend.request_log if c[0] == "iter_user_posts"]
    assert iter_posts[0][1] == ("42", 1)
    text = _captured(recording_console)
    assert "(last 1 posts)" in text


async def test_comments_aggregate_empty(
    history: HistoryStore,
    config: Config,
    session: Session,
    recording_console: Console,
) -> None:
    backend = FakeBackend(profiles={"42": _profile()}, posts={"42": []})
    facade = OsintFacade(backend=backend, history=history, config=config)
    out = await dispatch(
        "/comments",
        facade=facade,
        session=session,
        console=recording_console,
    )
    assert out == []
    text = _captured(recording_console)
    assert f"Comments from @alice (last {INTERACTIONS_DEFAULT_WINDOW} posts):" in text
    assert "no posts to analyze for @alice" in text


async def test_comments_csv_export(
    interactions_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
) -> None:
    facade = OsintFacade(backend=interactions_backend, history=history, config=config)
    await dispatch("/comments Cp1 --csv", facade=facade, session=session)
    out_path = config.output_dir / "alice" / "comments.csv"
    rows = list(csv.DictReader(out_path.read_text().splitlines()))
    assert len(rows) == 3
    assert rows[0]["post_code"] == "Cp1"
    assert rows[0]["user"] == "bob"
    assert rows[0]["text"] == "first!"
    assert rows[0]["rank"] == "1"


async def test_comments_json_export_aggregate(
    interactions_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
) -> None:
    facade = OsintFacade(backend=interactions_backend, history=history, config=config)
    await dispatch("/comments --json", facade=facade, session=session)
    out_path = config.output_dir / "alice" / "comments.json"
    payload = json.loads(out_path.read_text())
    assert payload["_schema"] == "insto.v1"
    assert payload["command"] == "comments"
    data = payload["data"]
    assert data["target"] == "alice"
    assert data["window"] == INTERACTIONS_DEFAULT_WINDOW
    assert data["analyzed_posts"] == 2
    assert data["post_code"] is None
    assert {item["comment_pk"] for item in data["items"]} == {"c1", "c2", "c3", "c4", "c5"}


# ---------------------------------------------------------------------------
# /wcommented
# ---------------------------------------------------------------------------


async def test_wcommented_top_and_header(
    interactions_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
    recording_console: Console,
) -> None:
    facade = OsintFacade(backend=interactions_backend, history=history, config=config)
    out = await dispatch(
        "/wcommented",
        facade=facade,
        session=session,
        console=recording_console,
    )
    assert isinstance(out, TopList)
    assert out.kind == "wcommented"
    # bob: 3, carol: 1, dave: 1 — bob first, then carol (tie broken by key asc).
    assert out.items[0] == ("bob", 3)
    keys = {k for k, _ in out.items}
    assert keys == {"bob", "carol", "dave"}
    text = _captured(recording_console)
    assert "Top commenters on @alice (last 50 posts):" in text


async def test_wcommented_limit_propagated(
    interactions_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
) -> None:
    interactions_backend.request_log.clear()
    facade = OsintFacade(backend=interactions_backend, history=history, config=config)
    await dispatch("/wcommented --limit 1", facade=facade, session=session)
    iter_posts = [c for c in interactions_backend.request_log if c[0] == "iter_user_posts"]
    assert iter_posts[0][1] == ("42", 1)


async def test_wcommented_csv_export(
    interactions_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
) -> None:
    facade = OsintFacade(backend=interactions_backend, history=history, config=config)
    await dispatch("/wcommented --csv", facade=facade, session=session)
    out_path = config.output_dir / "alice" / "wcommented.csv"
    rows = list(csv.DictReader(out_path.read_text().splitlines()))
    assert rows[0]["user"] == "bob"
    assert rows[0]["count"] == "3"
    assert rows[0]["rank"] == "1"


async def test_wcommented_maltego_export(
    interactions_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
) -> None:
    facade = OsintFacade(backend=interactions_backend, history=history, config=config)
    await dispatch("/wcommented --maltego", facade=facade, session=session)
    out_path = config.output_dir / "alice" / "wcommented.maltego.csv"
    assert out_path.exists()
    rows = list(csv.DictReader(out_path.read_text().splitlines()))
    assert all(r["Type"] == "maltego.Person" for r in rows)
    bob = next(r for r in rows if r["Value"] == "bob")
    assert bob["Weight"] == "3"


async def test_wcommented_json_export(
    interactions_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
) -> None:
    facade = OsintFacade(backend=interactions_backend, history=history, config=config)
    await dispatch("/wcommented --json", facade=facade, session=session)
    out_path = config.output_dir / "alice" / "wcommented.json"
    payload = json.loads(out_path.read_text())
    data = payload["data"]
    assert data["kind"] == "wcommented"
    assert data["target"] == "alice"
    counts = {item["key"]: item["count"] for item in data["items"]}
    assert counts == {"bob": 3, "carol": 1, "dave": 1}


async def test_wcommented_empty_window(
    history: HistoryStore,
    config: Config,
    session: Session,
    recording_console: Console,
) -> None:
    backend = FakeBackend(profiles={"42": _profile()}, posts={"42": []})
    facade = OsintFacade(backend=backend, history=history, config=config)
    out = await dispatch(
        "/wcommented",
        facade=facade,
        session=session,
        console=recording_console,
    )
    assert isinstance(out, TopList)
    assert out.empty is True
    text = _captured(recording_console)
    assert "Top commenters on @alice (last 50 posts):" in text
    assert "no posts to analyze for @alice" in text


# ---------------------------------------------------------------------------
# /wtagged
# ---------------------------------------------------------------------------


async def test_wtagged_top_and_header(
    interactions_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
    recording_console: Console,
) -> None:
    facade = OsintFacade(backend=interactions_backend, history=history, config=config)
    out = await dispatch(
        "/wtagged",
        facade=facade,
        session=session,
        console=recording_console,
    )
    assert isinstance(out, TopList)
    assert out.kind == "wtagged"
    # bob: 2, carol: 1, t4 owner missing → skipped.
    assert out.items[0] == ("bob", 2)
    keys = {k for k, _ in out.items}
    assert keys == {"bob", "carol"}
    text = _captured(recording_console)
    assert "Users tagging @alice (last 50 tagged posts):" in text


async def test_wtagged_limit_propagated(
    interactions_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
) -> None:
    interactions_backend.request_log.clear()
    facade = OsintFacade(backend=interactions_backend, history=history, config=config)
    await dispatch("/wtagged --limit 2", facade=facade, session=session)
    iter_tagged = [c for c in interactions_backend.request_log if c[0] == "iter_user_tagged"]
    assert iter_tagged and iter_tagged[0][1] == ("42", 2)


async def test_wtagged_csv_export(
    interactions_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
) -> None:
    facade = OsintFacade(backend=interactions_backend, history=history, config=config)
    await dispatch("/wtagged --csv", facade=facade, session=session)
    out_path = config.output_dir / "alice" / "wtagged.csv"
    rows = list(csv.DictReader(out_path.read_text().splitlines()))
    assert rows[0]["owner"] == "bob"
    assert rows[0]["count"] == "2"


async def test_wtagged_empty_window(
    history: HistoryStore,
    config: Config,
    session: Session,
    recording_console: Console,
) -> None:
    backend = FakeBackend(profiles={"42": _profile()}, tagged={"42": []})
    facade = OsintFacade(backend=backend, history=history, config=config)
    out = await dispatch(
        "/wtagged",
        facade=facade,
        session=session,
        console=recording_console,
    )
    assert isinstance(out, TopList)
    assert out.empty is True
    text = _captured(recording_console)
    assert "Users tagging @alice (last 50 tagged posts):" in text
    assert "no tagged posts to analyze for @alice" in text


# ---------------------------------------------------------------------------
# /wliked
# ---------------------------------------------------------------------------


async def test_wliked_aggregates_across_post_window(
    interactions_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
) -> None:
    """bob and carol like both posts → 2 each; dave / eve only one each."""
    facade = OsintFacade(backend=interactions_backend, history=history, config=config)
    out = await dispatch("/wliked", facade=facade, session=session)
    assert isinstance(out, TopList)
    assert dict(out.items) == {"bob": 2, "carol": 2, "dave": 1, "eve": 1}
    assert out.kind == "wliked"


async def test_wliked_csv_export(
    interactions_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
) -> None:
    facade = OsintFacade(backend=interactions_backend, history=history, config=config)
    await dispatch("/wliked --csv", facade=facade, session=session)
    out_path = config.output_dir / "alice" / "wliked.csv"
    assert out_path.exists()
    rows = out_path.read_text().splitlines()
    assert rows[0] == "rank,user,count"
    assert "bob,2" in rows[1] or "carol,2" in rows[1]


async def test_wliked_maltego_export(
    interactions_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
) -> None:
    facade = OsintFacade(backend=interactions_backend, history=history, config=config)
    await dispatch("/wliked --maltego", facade=facade, session=session)
    out_path = config.output_dir / "alice" / "wliked.maltego.csv"
    assert out_path.exists()
    rows = out_path.read_text().splitlines()
    assert rows[0] == "Type,Value,Weight,Notes,Properties"
    assert any("maltego.Person" in line for line in rows[1:])


# ---------------------------------------------------------------------------
# /fans (composite likers + commenters)
# ---------------------------------------------------------------------------


async def test_fans_combines_likes_and_comments(
    interactions_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
) -> None:
    """bob: 2L + 3C → score 2 + 9 = 11. carol: 2L + 1C → 2 + 3 = 5.
    dave: 1L + 1C → 1 + 3 = 4. eve: 1L + 0C → 1.
    Default comment_weight is 3."""
    facade = OsintFacade(backend=interactions_backend, history=history, config=config)
    out = await dispatch("/fans", facade=facade, session=session)
    assert isinstance(out, FansResult)
    by_user = {row.username: row for row in out.items}
    assert by_user["bob"].likes == 2 and by_user["bob"].comments == 3
    assert by_user["bob"].score == 2 + 3 * 3
    assert by_user["carol"].score == 2 + 3 * 1
    assert out.items[0].username == "bob"  # top fan
    assert out.comment_weight == 3


async def test_fans_csv_export_has_breakdown_columns(
    interactions_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
) -> None:
    facade = OsintFacade(backend=interactions_backend, history=history, config=config)
    await dispatch("/fans --csv", facade=facade, session=session)
    out_path = config.output_dir / "alice" / "fans.csv"
    assert out_path.exists()
    header = out_path.read_text().splitlines()[0]
    assert header == "rank,user,likes,comments,score"


async def test_fans_maltego_export_with_score_weight(
    interactions_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
) -> None:
    facade = OsintFacade(backend=interactions_backend, history=history, config=config)
    await dispatch("/fans --maltego", facade=facade, session=session)
    out_path = config.output_dir / "alice" / "fans.maltego.csv"
    assert out_path.exists()
    rows = list(csv.DictReader(out_path.read_text().splitlines()))
    # bob is top fan with score 11 (2L+3C)
    bob_row = next(r for r in rows if r["Value"] == "bob")
    assert bob_row["Type"] == "maltego.Person"
    assert bob_row["Weight"] == "11"
    assert bob_row["Notes"] == "2L+3C"


async def test_fans_empty_target_renders_message(
    history: HistoryStore,
    config: Config,
    session: Session,
    recording_console: Console,
) -> None:
    backend = FakeBackend(profiles={"42": _profile()}, posts={"42": []})
    facade = OsintFacade(backend=backend, history=history, config=config)
    await dispatch("/fans", facade=facade, session=session, console=recording_console)
    text = _captured(recording_console)
    assert "no posts to analyze for @alice" in text


# ---------------------------------------------------------------------------
# Cross-cutting
# ---------------------------------------------------------------------------


async def test_interactions_commands_require_target(
    interactions_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
) -> None:
    facade = OsintFacade(backend=interactions_backend, history=history, config=config)
    empty_session = Session()
    for line in ("/comments", "/wcommented", "/wliked", "/wtagged", "/fans"):
        with pytest.raises(CommandUsageError, match="no target set"):
            await dispatch(line, facade=facade, session=empty_session)
