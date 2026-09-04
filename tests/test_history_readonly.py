"""Credential-free service status must never initialize or migrate a store."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from insto.exceptions import BackendError
from insto.service import history


def test_missing_store_does_not_create_parent(tmp_path: Path) -> None:
    path = tmp_path / "absent" / "store.db"
    assert history.read_watches_readonly(path) is None
    assert not path.parent.exists()


def test_existing_rows_are_read_without_changing_database(tmp_path: Path) -> None:
    path = tmp_path / "store ?#name.db"
    store = history.HistoryStore(path)
    store.register_watch("alice", 600)
    store.register_watch("bob", 900)
    bob = store.get_watch("bob")
    assert bob is not None
    store.update_watch_state(bob, status="paused", last_ok=123, last_error="private")
    expected = store.list_watches()
    store.close()
    before = path.read_bytes()
    before_mode = path.stat().st_mode
    assert history.read_watches_readonly(path) == expected
    assert path.read_bytes() == before
    assert path.stat().st_mode == before_mode


@pytest.mark.parametrize("version", ["1", "3", "invalid"])
def test_unsupported_schema_is_not_migrated(tmp_path: Path, version: str) -> None:
    path = tmp_path / "store.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE _meta(key TEXT PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO _meta VALUES('schema_version', ?)", (version,))
    before = path.read_bytes()
    with pytest.raises(BackendError, match="schema"):
        history.read_watches_readonly(path)
    assert path.read_bytes() == before


def test_corrupt_database_is_an_error_not_empty(tmp_path: Path) -> None:
    path = tmp_path / "store.db"
    path.write_bytes(b"not sqlite private-token")
    with pytest.raises(BackendError, match="read watch") as caught:
        history.read_watches_readonly(path)
    assert "private-token" not in str(caught.value)


def test_async_wrapper_uses_readonly_path(tmp_path: Path) -> None:
    path = tmp_path / "none.db"
    assert asyncio.run(history.read_watches_readonly_async(path)) is None
    assert not path.exists()
