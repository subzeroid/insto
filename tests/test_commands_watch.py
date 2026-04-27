"""Tests for `insto.commands.watch`: /watch, /unwatch, /watching, /diff, /history.

The watch tick is exercised directly through `WatchManager.tick_once(...)`
so tests do not have to wait minutes for the periodic loop. The full
periodic loop is exercised in the cancellation test where every watch is
sleeping in its 5-minute interval and `aclose()` cancels them.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, Generator
from pathlib import Path

import pytest
from rich.console import Console

# Importing the package registers /watch, /unwatch, /watching, /diff, /history.
import insto.commands  # noqa: F401  (side-effect import)
from insto.commands._base import (
    CommandUsageError,
    Session,
    dispatch,
)
from insto.config import Config
from insto.exceptions import Banned, Transient
from insto.models import Profile
from insto.service.facade import OsintFacade
from insto.service.history import HistoryStore
from insto.service.watch import WatchManager
from tests.fakes import FakeBackend

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def history(tmp_path: Path) -> Generator[HistoryStore, None, None]:
    s = HistoryStore(tmp_path / "store.db")
    yield s
    s.close()


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(output_dir=tmp_path / "output", db_path=tmp_path / "store.db")


def _profile(pk: str, username: str, **kw: object) -> Profile:
    return Profile(pk=pk, username=username, access="public", **kw)  # type: ignore[arg-type]


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend(
        profiles={
            "1": _profile("1", "alice"),
            "2": _profile("2", "bob"),
            "3": _profile("3", "carol"),
            "4": _profile("4", "dave"),
        }
    )


@pytest.fixture
async def facade(
    backend: FakeBackend, history: HistoryStore, config: Config
) -> AsyncGenerator[OsintFacade, None]:
    f = OsintFacade(backend=backend, history=history, config=config)
    try:
        yield f
    finally:
        await f.watches.cancel_all()


@pytest.fixture
def session() -> Session:
    return Session()


@pytest.fixture
def console() -> Console:
    return Console(record=True, color_system=None, width=120)


# ---------------------------------------------------------------------------
# /watch — registration rules
# ---------------------------------------------------------------------------


async def test_watch_registers_target_and_caps_at_three(
    facade: OsintFacade, session: Session, console: Console
) -> None:
    try:
        await dispatch("/watch alice 600", facade=facade, session=session, console=console)
        await dispatch("/watch bob 600", facade=facade, session=session, console=console)
        await dispatch("/watch carol 600", facade=facade, session=session, console=console)
        assert len(facade.watches) == 3
        with pytest.raises(CommandUsageError, match="too many active watches"):
            await dispatch(
                "/watch dave 600",
                facade=facade,
                session=session,
                console=console,
            )
    finally:
        await facade.watches.cancel_all()


async def test_watch_rejects_short_interval(
    facade: OsintFacade, session: Session, console: Console
) -> None:
    with pytest.raises(CommandUsageError, match="at least 300 seconds"):
        await dispatch(
            "/watch alice 60",
            facade=facade,
            session=session,
            console=console,
        )
    assert len(facade.watches) == 0


async def test_watch_rejects_duplicate_user(
    facade: OsintFacade, session: Session, console: Console
) -> None:
    try:
        await dispatch("/watch alice 600", facade=facade, session=session, console=console)
        with pytest.raises(CommandUsageError, match="already watching"):
            await dispatch(
                "/watch alice 600",
                facade=facade,
                session=session,
                console=console,
            )
    finally:
        await facade.watches.cancel_all()


async def test_watch_uses_session_target_when_no_arg(
    facade: OsintFacade, session: Session, console: Console
) -> None:
    session.set_target("alice")
    try:
        await dispatch("/watch", facade=facade, session=session, console=console)
        assert "alice" in facade.watches
    finally:
        await facade.watches.cancel_all()


# ---------------------------------------------------------------------------
# /unwatch and /watching
# ---------------------------------------------------------------------------


async def test_unwatch_removes_active_watch(
    facade: OsintFacade, session: Session, console: Console
) -> None:
    try:
        await dispatch("/watch alice 600", facade=facade, session=session, console=console)
        result = await dispatch("/unwatch alice", facade=facade, session=session, console=console)
        assert result is True
        assert "alice" not in facade.watches
    finally:
        await facade.watches.cancel_all()


async def test_unwatch_unknown_returns_false(
    facade: OsintFacade, session: Session, console: Console
) -> None:
    result = await dispatch("/unwatch ghost", facade=facade, session=session, console=console)
    assert result is False


async def test_watching_lists_active_watches(
    facade: OsintFacade, session: Session, console: Console
) -> None:
    try:
        await dispatch("/watch alice 600", facade=facade, session=session, console=console)
        await dispatch("/watch bob 900", facade=facade, session=session, console=console)
        rows = await dispatch("/watching", facade=facade, session=session, console=console)
        users = sorted(r["user"] for r in rows)
        assert users == ["alice", "bob"]
    finally:
        await facade.watches.cancel_all()


async def test_watching_when_empty(facade: OsintFacade, session: Session, console: Console) -> None:
    rows = await dispatch("/watching", facade=facade, session=session, console=console)
    assert rows == []
    assert "no active watches" in console.export_text()


# ---------------------------------------------------------------------------
# Tick state machine — direct WatchManager API
# ---------------------------------------------------------------------------


async def test_tick_paused_after_two_consecutive_failures() -> None:
    mgr = WatchManager()
    calls = {"n": 0}

    async def tick() -> None:
        calls["n"] += 1
        raise Transient("network blip")

    mgr.add("alice", 600, tick=tick, start=False)

    spec = await mgr.tick_once("alice")
    assert spec.status == "active"
    # one failed tick uses both the initial attempt and one retry
    assert calls["n"] == 2

    spec = await mgr.tick_once("alice")
    assert spec.status == "paused"
    assert spec.last_error and "network blip" in spec.last_error
    assert calls["n"] == 4

    await mgr.cancel_all()


async def test_tick_recovers_on_retry() -> None:
    mgr = WatchManager()
    state = {"n": 0}

    async def tick() -> None:
        state["n"] += 1
        if state["n"] == 1:
            raise Transient("flap")

    mgr.add("alice", 600, tick=tick, start=False)
    spec = await mgr.tick_once("alice")
    assert spec.status == "active"
    assert spec.last_error is None
    assert spec.last_ok is not None
    await mgr.cancel_all()


async def test_tick_banned_pauses_immediately_without_breaking_loop() -> None:
    mgr = WatchManager()
    calls = {"n": 0}

    async def tick() -> None:
        calls["n"] += 1
        raise Banned("account suspended")

    mgr.add("alice", 600, tick=tick, start=False)
    spec = await mgr.tick_once("alice")
    assert spec.status == "paused"
    assert calls["n"] == 1  # no retry on a hard ban
    assert "suspended" in (spec.last_error or "")
    # The manager itself stayed alive after the failed tick.
    assert "alice" in mgr
    await mgr.cancel_all()


async def test_cancel_all_drains_running_loop_tasks_quickly() -> None:
    mgr = WatchManager()

    async def tick() -> None:
        return None

    for user in ("alice", "bob", "carol"):
        mgr.add(user, 600, tick=tick)

    # Yield once so the loop tasks reach their first `asyncio.sleep`.
    await asyncio.sleep(0)

    start = time.monotonic()
    await mgr.cancel_all()
    elapsed = time.monotonic() - start
    assert elapsed < 0.1
    assert len(mgr) == 0


# ---------------------------------------------------------------------------
# /diff — surface the snapshot diff via the registry
# ---------------------------------------------------------------------------


async def test_diff_first_seen_when_no_prior_snapshot(
    facade: OsintFacade, session: Session, console: Console
) -> None:
    result = await dispatch("/diff alice", facade=facade, session=session, console=console)
    assert result["first_seen"] is True
    assert result["changes"] == {}


async def test_diff_picks_up_username_rename_into_previous_usernames(
    facade: OsintFacade, backend: FakeBackend, session: Session, console: Console
) -> None:
    # Take an initial snapshot under the old username "alice".
    await facade.snapshot("alice")

    # Mutate the backend so the same pk now reports a different username.
    backend.profiles["1"] = _profile("1", "alice2")
    facade.clear_target_cache("alice")

    result = await dispatch("/diff alice2", facade=facade, session=session, console=console)
    assert result["first_seen"] is False
    assert "alice" in result["previous_usernames"]
    assert "username" in result["changes"]
    assert result["changes"]["username"] == {"old": "alice", "new": "alice2"}


# ---------------------------------------------------------------------------
# /history
# ---------------------------------------------------------------------------


async def test_history_reads_recent_cli_history_rows(
    facade: OsintFacade, session: Session, console: Console
) -> None:
    await facade.history.record_command_async("/info", "alice")
    await facade.history.record_command_async("/posts", "alice")
    await facade.history.record_command_async("/info", "bob")

    rows = await dispatch("/history 2", facade=facade, session=session, console=console)
    assert len(rows) == 2
    # Most recent first.
    assert rows[0]["cmd"] == "/info"
    assert rows[0]["target"] == "bob"


async def test_history_empty_prints_note(
    facade: OsintFacade, session: Session, console: Console
) -> None:
    rows = await dispatch("/history", facade=facade, session=session, console=console)
    assert rows == []
    assert "no recorded commands yet" in console.export_text()
