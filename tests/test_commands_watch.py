"""Tests for persistent watch controls plus `/diff` and `/history`."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Generator
from pathlib import Path

import pytest
from rich.console import Console

# Importing the package registers /watch, /unwatch, /watching, /diff, /history.
import insto.commands  # noqa: F401  (side-effect import)
from insto._redact import register_secret
from insto.commands._base import CommandUsageError, Session, dispatch
from insto.config import Config
from insto.models import Profile
from insto.service.facade import OsintFacade
from insto.service.history import HistoryStore
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
        await f.aclose()


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
    await dispatch("/watch alice 600", facade=facade, session=session, console=console)
    await dispatch("/watch bob 600", facade=facade, session=session, console=console)
    await dispatch("/watch carol 600", facade=facade, session=session, console=console)
    assert len(facade.history.list_watches()) == 3
    with pytest.raises(CommandUsageError, match="too many active watches"):
        await dispatch(
            "/watch dave 600",
            facade=facade,
            session=session,
            console=console,
        )


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
    assert facade.history.list_watches() == []


async def test_watch_rejects_duplicate_user(
    facade: OsintFacade, session: Session, console: Console
) -> None:
    await dispatch("/watch alice 600", facade=facade, session=session, console=console)
    with pytest.raises(CommandUsageError, match="already watching"):
        await dispatch(
            "/watch @ALICE 600",
            facade=facade,
            session=session,
            console=console,
        )


async def test_watch_uses_session_target_when_no_arg(
    facade: OsintFacade, session: Session, console: Console
) -> None:
    session.set_target("alice")
    payload = await dispatch("/watch", facade=facade, session=session, console=console)
    assert payload["user"] == "alice"
    assert facade.history.get_watch("alice") is not None


async def test_watch_treats_single_numeric_arg_as_interval_for_session_target(
    facade: OsintFacade, session: Session, console: Console
) -> None:
    session.set_target("alice")

    payload = await dispatch("/watch 600", facade=facade, session=session, console=console)

    assert payload["user"] == "alice"
    assert payload["interval_seconds"] == 600


# ---------------------------------------------------------------------------
# /unwatch and /watching
# ---------------------------------------------------------------------------


async def test_unwatch_removes_active_watch(
    facade: OsintFacade, session: Session, console: Console
) -> None:
    await dispatch("/watch alice 600", facade=facade, session=session, console=console)
    result = await dispatch("/unwatch @ALICE", facade=facade, session=session, console=console)
    assert result is True
    assert facade.history.get_watch("alice") is None


async def test_unwatch_unknown_returns_false(
    facade: OsintFacade, session: Session, console: Console
) -> None:
    result = await dispatch("/unwatch ghost", facade=facade, session=session, console=console)
    assert result is False


async def test_watching_lists_active_watches(
    facade: OsintFacade, session: Session, console: Console
) -> None:
    await dispatch("/watch alice 600", facade=facade, session=session, console=console)
    await dispatch("/watch bob 900", facade=facade, session=session, console=console)
    rows = await dispatch("/watching", facade=facade, session=session, console=console)
    users = sorted(r["user"] for r in rows)
    assert users == ["alice", "bob"]
    assert all("registration_id" not in row for row in rows)


async def test_watching_when_empty(facade: OsintFacade, session: Session, console: Console) -> None:
    rows = await dispatch("/watching", facade=facade, session=session, console=console)
    assert rows == []
    assert "no active watches" in console.export_text()


async def test_watch_reactivates_paused_row_with_new_internal_id(
    facade: OsintFacade, session: Session, console: Console
) -> None:
    created = facade.history.register_watch("alice", 300).spec
    assert created is not None
    assert facade.history.update_watch_state(
        created, last_error="temporary", consecutive_errors=2, status="paused"
    )

    payload = await dispatch("/watch ALICE 900", facade=facade, session=session, console=console)
    current = facade.history.get_watch("alice")
    assert current is not None
    assert current.registration_id != created.registration_id
    assert current.interval_seconds == 900
    assert current.status == "active"
    assert current.consecutive_errors == 0
    assert current.last_error is None
    assert "registration_id" not in payload


async def test_watch_and_unwatch_request_immediate_reconciliation(
    facade: OsintFacade, session: Session, console: Console
) -> None:
    calls = 0

    class CoordinatorSpy:
        def request_reconcile(self) -> None:
            nonlocal calls
            calls += 1

        async def stop(self) -> None:
            return None

    facade.watch_daemon = CoordinatorSpy()  # type: ignore[assignment]
    await dispatch("/watch alice 300", facade=facade, session=session, console=console)
    await dispatch("/unwatch alice", facade=facade, session=session, console=console)
    assert calls == 2


async def test_one_shot_watch_prints_daemon_reminder(
    facade: OsintFacade, session: Session, console: Console
) -> None:
    facade.watch_role = "oneshot"
    payload = await dispatch("/watch @Alice 300", facade=facade, session=session, console=console)
    assert payload["user"] == "alice"
    assert "registration_id" not in payload
    assert "insto watch-daemon" in console.export_text()


async def test_watching_redacts_persisted_errors_and_hides_internal_id(
    facade: OsintFacade, session: Session, console: Console
) -> None:
    secret = "tok-super-secret"
    register_secret(secret)
    created = facade.history.register_watch("alice", 300).spec
    assert created is not None
    assert facade.history.update_watch_state(created, last_error=f"Bearer {secret}")

    rows = await dispatch("/watching", facade=facade, session=session, console=console)
    assert rows == [
        {
            "user": "alice",
            "interval_seconds": 300,
            "last_ok": None,
            "last_error": "Bearer ***",
            "consecutive_errors": 0,
            "status": "active",
        }
    ]
    assert secret not in console.export_text()


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
