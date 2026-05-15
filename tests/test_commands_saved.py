"""Tests for read-only saved collections/media commands."""

from __future__ import annotations

import csv
import json
from collections.abc import Generator
from pathlib import Path

import pytest
from rich.console import Console

import insto.commands  # noqa: F401  (side-effect import)
from insto.commands._base import CommandUsageError, Session, dispatch
from insto.config import Config
from insto.models import Post, SavedCollection
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


def _post(pk: str, *, code: str, owner: str = "instagram") -> Post:
    return Post(
        pk=pk,
        code=code,
        taken_at=1_700_000_000,
        media_type="image",
        caption=f"saved {code}",
        like_count=10,
        comment_count=2,
        owner_pk="25025320",
        owner_username=owner,
        media_urls=["https://cdn.example/post.jpg"],
    )


def _saved_backend() -> FakeBackend:
    backend = FakeBackend(
        saved_collections=[
            SavedCollection(pk="c1", name="Research", collection_type="MEDIA", media_count=2),
            SavedCollection(pk="c2", name="Travel", collection_type="MEDIA", media_count=1),
        ],
        saved_posts={
            None: [
                _post("m1", code="AAA111"),
                _post("m2", code="BBB222"),
            ],
            "Research": [
                _post("m3", code="CCC333"),
                _post("m4", code="DDD444"),
            ],
        },
    )
    backend.capabilities = frozenset({"saved_read"})
    return backend


async def test_collections_lists_saved_collections(
    history: HistoryStore,
    config: Config,
    session: Session,
    recording_console: Console,
) -> None:
    backend = _saved_backend()
    facade = OsintFacade(backend=backend, history=history, config=config)

    out = await dispatch(
        "/collections 1",
        facade=facade,
        session=session,
        console=recording_console,
    )

    assert [collection.pk for collection in out] == ["c1"]
    captured = _captured(recording_console)
    assert "Saved collections" in captured
    assert "Research" in captured
    assert "Travel" not in captured
    assert backend.request_log == [("iter_saved_collections", (1,))]


async def test_saved_lists_generic_saved_posts_without_downloading(
    history: HistoryStore,
    config: Config,
    session: Session,
    recording_console: Console,
) -> None:
    backend = _saved_backend()
    facade = OsintFacade(backend=backend, history=history, config=config)

    out = await dispatch("/saved 1", facade=facade, session=session, console=recording_console)

    assert [post.pk for post in out] == ["m1"]
    captured = _captured(recording_console)
    assert "saved posts" in captured
    assert "AAA111" in captured
    assert "BBB222" not in captured
    assert not list(config.output_dir.rglob("*"))
    assert backend.request_log == [("iter_saved_posts", (None, 1))]


async def test_saved_filters_by_named_collection(
    history: HistoryStore,
    config: Config,
    session: Session,
    recording_console: Console,
) -> None:
    backend = _saved_backend()
    facade = OsintFacade(backend=backend, history=history, config=config)

    out = await dispatch(
        "/saved --collection Research 2",
        facade=facade,
        session=session,
        console=recording_console,
    )

    assert [post.pk for post in out] == ["m3", "m4"]
    captured = _captured(recording_console)
    assert "Research" in captured
    assert "CCC333" in captured
    assert backend.request_log == [("iter_saved_posts", ("Research", 2))]


async def test_saved_json_export(
    history: HistoryStore,
    config: Config,
    session: Session,
) -> None:
    backend = _saved_backend()
    facade = OsintFacade(backend=backend, history=history, config=config)

    await dispatch("/saved --collection Research 1 --json", facade=facade, session=session)

    out_path = config.output_dir / "_" / "saved.json"
    payload = json.loads(out_path.read_text())
    assert payload["command"] == "saved"
    assert payload["target"] is None
    assert [post["pk"] for post in payload["data"]] == ["m3"]


async def test_collections_csv_stdout(
    history: HistoryStore,
    config: Config,
    session: Session,
    capsysbinary: pytest.CaptureFixture[bytes],
) -> None:
    backend = _saved_backend()
    facade = OsintFacade(backend=backend, history=history, config=config)

    await dispatch("/collections 1 --csv -", facade=facade, session=session)

    rows = list(csv.DictReader(capsysbinary.readouterr().out.decode().splitlines()))
    assert rows == [
        {
            "pk": "c1",
            "name": "Research",
            "collection_type": "MEDIA",
            "media_count": "2",
        }
    ]


async def test_saved_requires_saved_read_capability(
    history: HistoryStore,
    config: Config,
    session: Session,
) -> None:
    backend = FakeBackend(saved_posts={None: [_post("m1", code="AAA111")]})
    facade = OsintFacade(backend=backend, history=history, config=config)

    with pytest.raises(CommandUsageError, match="missing capability: saved_read"):
        await dispatch("/saved", facade=facade, session=session)

    assert backend.request_log == []
