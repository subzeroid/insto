"""Tests for `insto.commands.network`: /followers, /followings, /mutuals, /similar.

Each command is exercised through `dispatch(...)` against a `FakeBackend`
fixture so that pagination, limit propagation, JSON/CSV exports, and the
`/mutuals` truncation guard can all be asserted without touching the
network or the disk for media files.
"""

from __future__ import annotations

import csv
import io
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
from insto.commands.network import (
    MUTUALS_DEFAULT_LIMIT,
    MUTUALS_UNBOUNDED_LIMIT,
)
from insto.config import Config
from insto.models import Profile, User
from insto.service.analytics import MutualsResult
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


def _user(
    pk: str, username: str, *, full_name: str = "", private: bool = False, verified: bool = False
) -> User:
    return User(
        pk=pk,
        username=username,
        full_name=full_name,
        is_private=private,
        is_verified=verified,
    )


def _make_followers(n: int, *, prefix: str = "f") -> list[User]:
    return [_user(f"{prefix}{i}", f"{prefix}user{i}") for i in range(n)]


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


# ---------------------------------------------------------------------------
# /followers
# ---------------------------------------------------------------------------


@pytest.fixture
def network_backend() -> FakeBackend:
    """Backend with overlap and a small page size for pagination assertions."""
    followers = [
        _user("u1", "common1"),
        _user("u2", "follower_only"),
        _user("u3", "common2", verified=True),
        _user("u4", "follower_only2"),
        _user("u5", "common3", private=True),
    ]
    following = [
        _user("u1", "common1"),
        _user("u3", "common2", verified=True),
        _user("u5", "common3", private=True),
        _user("u6", "following_only"),
    ]
    suggested = [_user("s1", "suggested1"), _user("s2", "suggested2")]
    backend = FakeBackend(
        profiles={"42": _profile()},
        followers={"42": followers},
        following={"42": following},
        suggested={"42": suggested},
        page_size=2,
    )
    return backend


async def test_followers_default_limit_is_50(
    network_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
) -> None:
    network_backend.request_log.clear()
    facade = OsintFacade(backend=network_backend, history=history, config=config)
    await dispatch("/followers", facade=facade, session=session)
    iter_calls = [c for c in network_backend.request_log if c[0] == "iter_user_followers"]
    assert iter_calls, "iter_user_followers must be called"
    assert iter_calls[0][1] == ("42", 50)


async def test_followers_pagination_stops_at_positional_limit(
    network_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
) -> None:
    # 5 users in fixture, page_size=2; --limit 3 should stop after 2 pages.
    network_backend.request_log.clear()
    facade = OsintFacade(backend=network_backend, history=history, config=config)
    out = await dispatch("/followers 3", facade=facade, session=session)
    assert isinstance(out, list)
    assert [u.pk for u in out] == ["u1", "u2", "u3"]
    assert network_backend.page_requests["iter_user_followers"] == 2


async def test_followers_global_limit_overrides_positional(
    network_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
) -> None:
    network_backend.request_log.clear()
    facade = OsintFacade(backend=network_backend, history=history, config=config)
    await dispatch("/followers 99 --limit 2", facade=facade, session=session)
    iter_calls = [c for c in network_backend.request_log if c[0] == "iter_user_followers"]
    assert iter_calls[0][1] == ("42", 2)


async def test_followers_renders_table(
    network_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
    recording_console: Console,
) -> None:
    facade = OsintFacade(backend=network_backend, history=history, config=config)
    await dispatch(
        "/followers 3",
        facade=facade,
        session=session,
        console=recording_console,
    )
    text = _captured(recording_console)
    assert "@common1" in text
    assert "@common2" in text
    assert "followers of @alice" in text


async def test_followers_empty_prints_message(
    network_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
    recording_console: Console,
) -> None:
    network_backend.followers = {}
    facade = OsintFacade(backend=network_backend, history=history, config=config)
    out = await dispatch("/followers", facade=facade, session=session, console=recording_console)
    assert out == []
    assert "no followers" in _captured(recording_console)


async def test_followers_json_export_writes_default_path(
    network_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
) -> None:
    facade = OsintFacade(backend=network_backend, history=history, config=config)
    await dispatch("/followers 3 --json", facade=facade, session=session)
    out_path = config.output_dir / "alice" / "followers.json"
    assert out_path.exists()
    payload = json.loads(out_path.read_text())
    assert payload["command"] == "followers"
    assert payload["_schema"] == "insto.v1"
    data = payload["data"]
    assert len(data) == 3
    assert data[0]["pk"] == "u1"
    assert data[0]["username"] == "common1"


async def test_followers_csv_export_is_flat(
    network_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
) -> None:
    facade = OsintFacade(backend=network_backend, history=history, config=config)
    await dispatch("/followers 3 --csv", facade=facade, session=session)
    out_path = config.output_dir / "alice" / "followers.csv"
    assert out_path.exists()
    rows = list(csv.DictReader(out_path.read_text().splitlines()))
    assert len(rows) == 3
    assert rows[0].keys() == {"rank", "pk", "username", "full_name", "is_private", "is_verified"}
    assert rows[0]["rank"] == "1"
    assert rows[0]["pk"] == "u1"


async def test_followers_maltego_export(
    network_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
) -> None:
    facade = OsintFacade(backend=network_backend, history=history, config=config)
    await dispatch("/followers 3 --maltego", facade=facade, session=session)
    out_path = config.output_dir / "alice" / "followers.maltego.csv"
    assert out_path.exists()
    rows = list(csv.DictReader(out_path.read_text().splitlines()))
    assert len(rows) == 3
    assert set(rows[0].keys()) == {"Type", "Value", "Weight", "Notes", "Properties"}
    assert rows[0]["Type"] == "maltego.Person"
    assert rows[0]["Value"] == "common1"
    assert rows[0]["Weight"] == "1"
    props = json.loads(rows[0]["Properties"])
    assert props["pk"] == "u1"
    assert props["rank"] == 1


async def test_followers_csv_to_stdout(
    network_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
    capsysbinary: pytest.CaptureFixture[bytes],
) -> None:
    facade = OsintFacade(backend=network_backend, history=history, config=config)
    await dispatch("/followers 2 --csv -", facade=facade, session=session)
    blob = capsysbinary.readouterr().out.decode("utf-8").splitlines()
    assert blob[0].split(",") == [
        "rank",
        "pk",
        "username",
        "full_name",
        "is_private",
        "is_verified",
    ]
    assert len(blob) == 3  # header + 2 rows


# ---------------------------------------------------------------------------
# /followings
# ---------------------------------------------------------------------------


async def test_followings_default_limit_is_50(
    network_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
) -> None:
    network_backend.request_log.clear()
    facade = OsintFacade(backend=network_backend, history=history, config=config)
    await dispatch("/followings", facade=facade, session=session)
    iter_calls = [c for c in network_backend.request_log if c[0] == "iter_user_following"]
    assert iter_calls and iter_calls[0][1] == ("42", 50)


async def test_followings_renders_table(
    network_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
    recording_console: Console,
) -> None:
    facade = OsintFacade(backend=network_backend, history=history, config=config)
    out = await dispatch(
        "/followings 4",
        facade=facade,
        session=session,
        console=recording_console,
    )
    assert [u.pk for u in out] == ["u1", "u3", "u5", "u6"]
    text = _captured(recording_console)
    assert "@following_only" in text


async def test_followings_csv_flat(
    network_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
) -> None:
    facade = OsintFacade(backend=network_backend, history=history, config=config)
    await dispatch("/followings 4 --csv", facade=facade, session=session)
    out_path = config.output_dir / "alice" / "followings.csv"
    rows = list(csv.DictReader(out_path.read_text().splitlines()))
    assert {r["username"] for r in rows} == {
        "common1",
        "common2",
        "common3",
        "following_only",
    }


# ---------------------------------------------------------------------------
# /similar
# ---------------------------------------------------------------------------


async def test_similar_renders_users(
    network_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
    recording_console: Console,
) -> None:
    facade = OsintFacade(backend=network_backend, history=history, config=config)
    out = await dispatch("/similar", facade=facade, session=session, console=recording_console)
    assert [u.pk for u in out] == ["s1", "s2"]
    assert "similar to @alice" in _captured(recording_console)


async def test_similar_empty(
    network_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
    recording_console: Console,
) -> None:
    network_backend.suggested = {}
    facade = OsintFacade(backend=network_backend, history=history, config=config)
    out = await dispatch("/similar", facade=facade, session=session, console=recording_console)
    assert out == []
    assert "no suggested" in _captured(recording_console)


async def test_similar_json_export(
    network_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
) -> None:
    facade = OsintFacade(backend=network_backend, history=history, config=config)
    await dispatch("/similar --json", facade=facade, session=session)
    out_path = config.output_dir / "alice" / "similar.json"
    payload = json.loads(out_path.read_text())
    assert [u["pk"] for u in payload["data"]] == ["s1", "s2"]


async def test_similar_csv_export(
    network_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
) -> None:
    facade = OsintFacade(backend=network_backend, history=history, config=config)
    await dispatch("/similar --csv", facade=facade, session=session)
    out_path = config.output_dir / "alice" / "similar.csv"
    rows = list(csv.DictReader(out_path.read_text().splitlines()))
    assert [r["username"] for r in rows] == ["suggested1", "suggested2"]


async def test_similar_limit_truncates_client_side(
    network_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
) -> None:
    facade = OsintFacade(backend=network_backend, history=history, config=config)
    out = await dispatch("/similar --limit 1", facade=facade, session=session)
    assert [u.pk for u in out] == ["s1"]


# ---------------------------------------------------------------------------
# /mutuals
# ---------------------------------------------------------------------------


async def test_mutuals_intersection_correct(
    network_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
    recording_console: Console,
) -> None:
    facade = OsintFacade(backend=network_backend, history=history, config=config)
    out = await dispatch("/mutuals", facade=facade, session=session, console=recording_console)
    assert isinstance(out, MutualsResult)
    # The three "common*" users appear in both lists; sorted by username asc.
    assert [u.username for u in out.items] == ["common1", "common2", "common3"]
    text = _captured(recording_console)
    assert "mutuals of @alice" in text


async def test_mutuals_default_uses_1000_per_side(
    network_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
) -> None:
    network_backend.request_log.clear()
    facade = OsintFacade(backend=network_backend, history=history, config=config)
    await dispatch("/mutuals", facade=facade, session=session)
    foll = [c for c in network_backend.request_log if c[0] == "iter_user_followers"]
    folw = [c for c in network_backend.request_log if c[0] == "iter_user_following"]
    assert foll and foll[0][1] == ("42", MUTUALS_DEFAULT_LIMIT)
    assert folw and folw[0][1] == ("42", MUTUALS_DEFAULT_LIMIT)


async def test_mutuals_truncated_warning_on_default_cap(
    history: HistoryStore,
    config: Config,
    session: Session,
    recording_console: Console,
) -> None:
    """When followers / following both fill the default cap, print warning."""
    # Use a small explicit --limit so we can observe truncation deterministically.
    followers = _make_followers(20, prefix="f")
    following = _make_followers(20, prefix="g")
    # Add overlap so mutuals isn't empty.
    overlap = _user("o1", "shared")
    followers.append(overlap)
    following.append(overlap)
    backend = FakeBackend(
        profiles={"42": _profile()},
        followers={"42": followers},
        following={"42": following},
        page_size=5,
    )
    facade = OsintFacade(backend=backend, history=history, config=config)
    await dispatch(
        "/mutuals --limit 5",
        facade=facade,
        session=session,
        console=recording_console,
    )
    text = _captured(recording_console)
    assert "truncated at 5 followers / 5 following" in text
    assert "pass --limit to widen" in text


async def test_mutuals_no_truncated_warning_under_cap(
    network_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
    recording_console: Console,
) -> None:
    """When neither side fills the cap, no truncation note appears."""
    facade = OsintFacade(backend=network_backend, history=history, config=config)
    await dispatch("/mutuals", facade=facade, session=session, console=recording_console)
    text = _captured(recording_console)
    assert "truncated" not in text


async def test_mutuals_unbounded_with_zero_limit(
    history: HistoryStore,
    config: Config,
    session: Session,
) -> None:
    """`--limit 0` opts out of the safety cap."""
    backend = FakeBackend(
        profiles={"42": _profile()},
        followers={"42": _make_followers(3)},
        following={"42": _make_followers(3)},
    )
    backend.request_log.clear()
    facade = OsintFacade(backend=backend, history=history, config=config)
    await dispatch("/mutuals --limit 0", facade=facade, session=session)
    foll = [c for c in backend.request_log if c[0] == "iter_user_followers"]
    assert foll and foll[0][1] == ("42", MUTUALS_UNBOUNDED_LIMIT)


async def test_mutuals_explicit_high_limit_no_warning_when_not_full(
    history: HistoryStore,
    config: Config,
    session: Session,
    recording_console: Console,
) -> None:
    """`--limit 5000` against 10-user lists must not print truncation note."""
    backend = FakeBackend(
        profiles={"42": _profile()},
        followers={"42": _make_followers(10)},
        following={"42": _make_followers(10)},
    )
    facade = OsintFacade(backend=backend, history=history, config=config)
    await dispatch(
        "/mutuals --limit 5000",
        facade=facade,
        session=session,
        console=recording_console,
    )
    assert "truncated" not in _captured(recording_console)


async def test_mutuals_json_export(
    network_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
) -> None:
    facade = OsintFacade(backend=network_backend, history=history, config=config)
    await dispatch("/mutuals --json", facade=facade, session=session)
    out_path = config.output_dir / "alice" / "mutuals.json"
    payload = json.loads(out_path.read_text())
    assert payload["command"] == "mutuals"
    data = payload["data"]
    assert data["target"] == "alice"
    assert data["follower_window"] == MUTUALS_DEFAULT_LIMIT
    assert data["following_window"] == MUTUALS_DEFAULT_LIMIT
    assert [u["username"] for u in data["items"]] == ["common1", "common2", "common3"]


async def test_mutuals_csv_export_only_intersection(
    network_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
) -> None:
    facade = OsintFacade(backend=network_backend, history=history, config=config)
    await dispatch("/mutuals --csv", facade=facade, session=session)
    out_path = config.output_dir / "alice" / "mutuals.csv"
    rows = list(csv.DictReader(out_path.read_text().splitlines()))
    assert [r["username"] for r in rows] == ["common1", "common2", "common3"]
    assert rows[0]["rank"] == "1"


async def test_mutuals_maltego_export(
    network_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
) -> None:
    facade = OsintFacade(backend=network_backend, history=history, config=config)
    await dispatch("/mutuals --maltego", facade=facade, session=session)
    out_path = config.output_dir / "alice" / "mutuals.maltego.csv"
    assert out_path.exists()
    rows = list(csv.DictReader(out_path.read_text().splitlines()))
    assert [r["Value"] for r in rows] == ["common1", "common2", "common3"]
    assert all(r["Type"] == "maltego.Person" for r in rows)


async def test_mutuals_empty_intersection(
    history: HistoryStore,
    config: Config,
    session: Session,
    recording_console: Console,
) -> None:
    backend = FakeBackend(
        profiles={"42": _profile()},
        followers={"42": [_user("a", "alpha")]},
        following={"42": [_user("b", "beta")]},
    )
    facade = OsintFacade(backend=backend, history=history, config=config)
    out = await dispatch("/mutuals", facade=facade, session=session, console=recording_console)
    assert isinstance(out, MutualsResult)
    assert out.items == []
    assert "no mutuals" in _captured(recording_console)


# ---------------------------------------------------------------------------
# Cross-cutting
# ---------------------------------------------------------------------------


async def test_network_commands_require_target(
    network_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
) -> None:
    facade = OsintFacade(backend=network_backend, history=history, config=config)
    empty_session = Session()
    for line in ("/followers", "/followings", "/mutuals", "/similar"):
        with pytest.raises(CommandUsageError, match="no target set"):
            await dispatch(line, facade=facade, session=empty_session)


async def test_followers_explicit_target_overrides_session(
    network_backend: FakeBackend,
    history: HistoryStore,
    config: Config,
) -> None:
    """Sanity: with no positional target arg the command relies on session.

    /followers does not declare an explicit positional target — only `count`.
    The session-level target is what feeds the facade.
    """
    facade = OsintFacade(backend=network_backend, history=history, config=config)
    sess = Session()
    sess.set_target("alice")
    out = await dispatch("/followers 2", facade=facade, session=sess)
    assert [u.pk for u in out] == ["u1", "u2"]


# Suppress unused-import warnings for io — kept for potential future stream tests.
_ = io
