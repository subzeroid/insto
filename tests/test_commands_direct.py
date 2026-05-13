"""Tests for read-only Direct commands."""

from __future__ import annotations

import json
from collections.abc import Generator
from pathlib import Path

import pytest
from rich.console import Console

# Importing the package registers all command modules.
import insto.commands  # noqa: F401  (side-effect import)
from insto.commands._base import CommandUsageError, Session, dispatch
from insto.config import Config
from insto.models import DirectMessage, DirectThread, User
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


@pytest.fixture
def session() -> Session:
    return Session()


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


def _message(pk: str, *, thread_id: str = "t1", text: str = "hello") -> DirectMessage:
    return DirectMessage(
        pk=pk,
        thread_id=thread_id,
        sender_pk="100",
        timestamp=1_700_000_000,
        item_type="text",
        text=text,
    )


def _thread(pk: str, *, title: str, username: str, message_count: int = 1) -> DirectThread:
    return DirectThread(
        pk=pk,
        title=title,
        users=[User(pk="100", username=username)],
        last_activity_at=1_700_000_000,
        message_count=message_count,
        messages=[_message("m1", thread_id=pk, text="preview")],
    )


def _direct_backend() -> FakeBackend:
    backend = FakeBackend(
        direct_threads=[
            _thread("t1", title="Alice", username="alice"),
            _thread("t2", title="Bob", username="bob"),
            _thread("t3", title="Carol", username="carol"),
        ],
        direct_messages={
            "t1": [
                _message("m1", text="first"),
                _message("m2", text="second"),
                DirectMessage(
                    pk="m3",
                    thread_id="t1",
                    sender_pk="101",
                    timestamp=1_700_000_100,
                    item_type="media_share",
                    media_pk="p1",
                    media_code="ABC123",
                ),
            ]
        },
    )
    backend.capabilities = frozenset({"direct_read"})
    return backend


async def test_direct_lists_threads(
    history: HistoryStore,
    config: Config,
    session: Session,
    recording_console: Console,
) -> None:
    backend = _direct_backend()
    facade = OsintFacade(backend=backend, history=history, config=config)

    out = await dispatch("/direct 2", facade=facade, session=session, console=recording_console)

    assert [thread.pk for thread in out] == ["t1", "t2"]
    captured = _captured(recording_console)
    assert "Direct threads" in captured
    assert "Alice" in captured
    assert "Bob" in captured
    assert "Carol" not in captured


async def test_direct_thread_lists_messages(
    history: HistoryStore,
    config: Config,
    session: Session,
    recording_console: Console,
) -> None:
    backend = _direct_backend()
    facade = OsintFacade(backend=backend, history=history, config=config)

    out = await dispatch(
        "/direct-thread t1 2", facade=facade, session=session, console=recording_console
    )

    assert [message.pk for message in out] == ["m1", "m2"]
    captured = _captured(recording_console)
    assert "Direct thread t1" in captured
    assert "first" in captured
    assert "second" in captured
    assert "ABC123" not in captured


async def test_direct_json_export(
    history: HistoryStore,
    config: Config,
    session: Session,
) -> None:
    backend = _direct_backend()
    facade = OsintFacade(backend=backend, history=history, config=config)

    await dispatch("/direct 2 --json", facade=facade, session=session)

    out_path = config.output_dir / "_" / "direct.json"
    payload = json.loads(out_path.read_text())
    assert payload["command"] == "direct"
    assert payload["target"] is None
    assert [thread["pk"] for thread in payload["data"]] == ["t1", "t2"]


async def test_direct_thread_json_stdout(
    history: HistoryStore,
    config: Config,
    session: Session,
    capsysbinary: pytest.CaptureFixture[bytes],
) -> None:
    backend = _direct_backend()
    facade = OsintFacade(backend=backend, history=history, config=config)

    await dispatch("/direct-thread t1 2 --json -", facade=facade, session=session)

    payload = json.loads(capsysbinary.readouterr().out)
    assert payload["command"] == "direct-thread"
    assert payload["target"] == "t1"
    assert [message["pk"] for message in payload["data"]] == ["m1", "m2"]


async def test_direct_rejects_csv(
    history: HistoryStore,
    config: Config,
    session: Session,
) -> None:
    facade = OsintFacade(backend=_direct_backend(), history=history, config=config)

    with pytest.raises(CommandUsageError, match="cannot be exported as CSV"):
        await dispatch("/direct --csv -", facade=facade, session=session)


async def test_direct_requires_direct_read_capability(
    history: HistoryStore,
    config: Config,
    session: Session,
) -> None:
    backend = FakeBackend(direct_threads=[_thread("t1", title="Alice", username="alice")])
    facade = OsintFacade(backend=backend, history=history, config=config)

    with pytest.raises(CommandUsageError, match="missing capability: direct_read"):
        await dispatch("/direct", facade=facade, session=session)

    assert backend.request_log == []
