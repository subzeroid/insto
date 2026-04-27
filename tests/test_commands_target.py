"""Tests for `insto.commands.target`: /target, /current, /clear.

Each test runs the command through `dispatch(...)` so the parser, registry,
and session-state plumbing are exercised end-to-end against `FakeBackend`.
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest

# Importing the package registers /target, /current, /clear.
import insto.commands  # noqa: F401  (side-effect import)
from insto.commands._base import (
    CommandUsageError,
    Session,
    dispatch,
)
from insto.config import Config
from insto.models import Profile
from insto.service.facade import OsintFacade
from insto.service.history import HistoryStore
from tests.fakes import FakeBackend, ProfileNotFound


@pytest.fixture
def history(tmp_path: Path) -> Generator[HistoryStore, None, None]:
    s = HistoryStore(tmp_path / "store.db")
    yield s
    s.close()


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(output_dir=tmp_path / "output", db_path=tmp_path / "store.db")


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend(
        profiles={
            "42": Profile(pk="42", username="alice", access="public"),
            "99": Profile(pk="99", username="bob", access="public"),
        }
    )


@pytest.fixture
def facade(backend: FakeBackend, history: HistoryStore, config: Config) -> OsintFacade:
    return OsintFacade(backend=backend, history=history, config=config)


@pytest.fixture
def session() -> Session:
    return Session()


async def test_target_sets_session(facade: OsintFacade, session: Session) -> None:
    result = await dispatch("/target alice", facade=facade, session=session)
    assert result == "alice"
    assert session.target == "alice"


async def test_target_strips_at_sign(facade: OsintFacade, session: Session) -> None:
    await dispatch("/target @alice", facade=facade, session=session)
    assert session.target == "alice"


async def test_target_pre_resolves_pk(
    facade: OsintFacade, backend: FakeBackend, session: Session
) -> None:
    await dispatch("/target alice", facade=facade, session=session)
    # The pk was cached on the facade as a side effect of /target.
    cached = facade._pk_cache.get("alice")
    assert cached == "42"


async def test_target_unknown_username_raises(facade: OsintFacade, session: Session) -> None:
    with pytest.raises(ProfileNotFound):
        await dispatch("/target ghost", facade=facade, session=session)
    # Session must remain untouched on failure.
    assert session.target is None


async def test_target_without_arg_errors(facade: OsintFacade, session: Session) -> None:
    with pytest.raises(CommandUsageError, match="usage: /target <username>"):
        await dispatch("/target", facade=facade, session=session)


async def test_target_blank_arg_errors(facade: OsintFacade, session: Session) -> None:
    with pytest.raises(CommandUsageError, match="usage: /target <username>"):
        await dispatch("/target @", facade=facade, session=session)


async def test_current_returns_active_target(facade: OsintFacade, session: Session) -> None:
    await dispatch("/target alice", facade=facade, session=session)
    current = await dispatch("/current", facade=facade, session=session)
    assert current == "alice"


async def test_current_returns_none_when_unset(facade: OsintFacade, session: Session) -> None:
    current = await dispatch("/current", facade=facade, session=session)
    assert current is None


async def test_clear_drops_session_and_pk_cache(
    facade: OsintFacade, backend: FakeBackend, session: Session
) -> None:
    await dispatch("/target alice", facade=facade, session=session)
    assert facade._pk_cache.get("alice") == "42"

    await dispatch("/clear", facade=facade, session=session)
    assert session.target is None
    assert "alice" not in facade._pk_cache


async def test_clear_when_unset_is_noop(facade: OsintFacade, session: Session) -> None:
    # Should not raise when called with nothing to clear.
    result = await dispatch("/clear", facade=facade, session=session)
    assert result is None
    assert session.target is None


async def test_target_replaces_previous(facade: OsintFacade, session: Session) -> None:
    await dispatch("/target alice", facade=facade, session=session)
    await dispatch("/target bob", facade=facade, session=session)
    assert session.target == "bob"
