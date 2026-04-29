"""Tests for `insto.commands.discovery`: /resolve, /audio, /recommended.

Each command is exercised through `dispatch(...)` against a `FakeBackend`
fixture. The hiker-side stubs (raising BackendError) are validated in
the contract tests; here we cover the command surface only.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Generator
from pathlib import Path

import pytest

# Importing the package registers all command modules.
import insto.commands  # noqa: F401  (side-effect import)
from insto.commands._base import CommandUsageError, Session, dispatch
from insto.config import Config
from insto.exceptions import BackendError
from insto.models import Post, Profile, User
from insto.service.facade import OsintFacade
from insto.service.history import HistoryStore
from tests.fakes import FakeBackend


@pytest.fixture
def history(tmp_path: Path) -> Generator[HistoryStore, None, None]:
    store = HistoryStore(tmp_path / "store.db")
    yield store
    store.close()


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(output_dir=tmp_path / "output", db_path=tmp_path / "store.db")


def _user(pk: str, username: str, *, full_name: str = "") -> User:
    return User(pk=pk, username=username, full_name=full_name)


def _post(pk: str) -> Post:
    return Post(
        pk=pk,
        code=f"C{pk}",
        taken_at=1_700_000_000,
        media_type="video",
        media_urls=[f"https://scontent.cdninstagram.com/{pk}.mp4"],
        owner_pk="42",
        owner_username="alice",
    )


@pytest.fixture
def session() -> Session:
    s = Session()
    s.set_target("alice")
    return s


# ---------------------------------------------------------------------------
# /resolve
# ---------------------------------------------------------------------------


async def test_resolve_returns_canonical_url(
    history: HistoryStore, config: Config, session: Session
) -> None:
    backend = FakeBackend(
        short_url_redirects={
            "https://instagram.com/share/abc": "https://www.instagram.com/p/RealPostCode/",
        }
    )
    facade = OsintFacade(backend=backend, history=history, config=config)
    out = await dispatch("/resolve https://instagram.com/share/abc", facade=facade, session=session)
    assert out == "https://www.instagram.com/p/RealPostCode/"


async def test_resolve_returns_input_when_already_canonical(
    history: HistoryStore, config: Config, session: Session
) -> None:
    backend = FakeBackend()  # no redirects configured
    facade = OsintFacade(backend=backend, history=history, config=config)
    out = await dispatch(
        "/resolve https://www.instagram.com/p/Already/", facade=facade, session=session
    )
    assert out == "https://www.instagram.com/p/Already/"


async def test_resolve_empty_url_rejected(
    history: HistoryStore, config: Config, session: Session
) -> None:
    backend = FakeBackend()
    facade = OsintFacade(backend=backend, history=history, config=config)
    with pytest.raises(CommandUsageError, match="needs a URL"):
        await dispatch('/resolve ""', facade=facade, session=session)


async def test_resolve_propagates_backend_error(
    history: HistoryStore, config: Config, session: Session
) -> None:
    """The hiker-side stub raises ``BackendError`` to point users at aiograpi.
    The command must propagate that as-is (not swallow / not coerce)."""
    backend = FakeBackend()
    backend.errors.resolve_short_url = BackendError("needs aiograpi backend")
    facade = OsintFacade(backend=backend, history=history, config=config)
    with pytest.raises(BackendError, match="needs aiograpi"):
        await dispatch("/resolve https://instagram.com/share/abc", facade=facade, session=session)


# ---------------------------------------------------------------------------
# /audio
# ---------------------------------------------------------------------------


async def test_audio_returns_clips(history: HistoryStore, config: Config, session: Session) -> None:
    backend = FakeBackend(
        audio_clips={"123abc": [_post("c1"), _post("c2"), _post("c3")]},
    )
    facade = OsintFacade(backend=backend, history=history, config=config)
    out = await dispatch("/audio 123abc", facade=facade, session=session)
    assert [p.pk for p in out] == ["c1", "c2", "c3"]


async def test_audio_count_caps_results(
    history: HistoryStore, config: Config, session: Session
) -> None:
    backend = FakeBackend(audio_clips={"x": [_post(str(i)) for i in range(5)]})
    facade = OsintFacade(backend=backend, history=history, config=config)
    out = await dispatch("/audio x 2", facade=facade, session=session)
    assert len(out) == 2


async def test_audio_empty_track_id_rejected(
    history: HistoryStore, config: Config, session: Session
) -> None:
    backend = FakeBackend()
    facade = OsintFacade(backend=backend, history=history, config=config)
    with pytest.raises(CommandUsageError, match="needs a track_id"):
        await dispatch('/audio ""', facade=facade, session=session)


async def test_audio_json_export(history: HistoryStore, config: Config, session: Session) -> None:
    backend = FakeBackend(audio_clips={"k": [_post("a"), _post("b")]})
    facade = OsintFacade(backend=backend, history=history, config=config)
    await dispatch("/audio k --json", facade=facade, session=session)
    out_path = config.output_dir / "k" / "audio.json"
    assert out_path.exists()
    payload = json.loads(out_path.read_text())
    assert payload["command"] == "audio"
    assert len(payload["data"]) == 2


# ---------------------------------------------------------------------------
# /recommended
# ---------------------------------------------------------------------------


def _profile() -> Profile:
    return Profile(pk="42", username="alice", access="public", full_name="Alice Doe")


async def test_recommended_returns_users(
    history: HistoryStore, config: Config, session: Session
) -> None:
    backend = FakeBackend(
        profiles={"42": _profile()},
        recommended={"42": [_user("r1", "rec1"), _user("r2", "rec2")]},
    )
    facade = OsintFacade(backend=backend, history=history, config=config)
    out = await dispatch("/recommended", facade=facade, session=session)
    assert [u.username for u in out] == ["rec1", "rec2"]


async def test_recommended_empty_prints_friendly_message(
    history: HistoryStore, config: Config, session: Session
) -> None:
    backend = FakeBackend(profiles={"42": _profile()})  # empty recommended
    facade = OsintFacade(backend=backend, history=history, config=config)
    out = await dispatch("/recommended", facade=facade, session=session)
    assert out == []


async def test_recommended_maltego_export(
    history: HistoryStore, config: Config, session: Session
) -> None:
    backend = FakeBackend(
        profiles={"42": _profile()},
        recommended={"42": [_user("r1", "rec1"), _user("r2", "rec2")]},
    )
    facade = OsintFacade(backend=backend, history=history, config=config)
    await dispatch("/recommended --maltego", facade=facade, session=session)
    out_path = config.output_dir / "alice" / "recommended.maltego.csv"
    assert out_path.exists()
    rows = list(csv.DictReader(out_path.read_text().splitlines()))
    assert len(rows) == 2
    assert rows[0]["Type"] == "maltego.Person"
    assert rows[0]["Value"] == "rec1"


async def test_recommended_propagates_backend_error(
    history: HistoryStore, config: Config, session: Session
) -> None:
    """Hiker stub raises BackendError("needs aiograpi"); /recommended propagates."""
    backend = FakeBackend(profiles={"42": _profile()})
    backend.errors.get_recommended = BackendError("needs aiograpi backend")
    facade = OsintFacade(backend=backend, history=history, config=config)
    with pytest.raises(BackendError, match="needs aiograpi"):
        await dispatch("/recommended", facade=facade, session=session)
