"""Tests for `insto.commands.operational`: /quota, /health, /config, /purge.

The four meta-commands are exercised through the public `dispatch` entry
point so the registry and per-command parsers are wired the same way the
REPL drives them. `/purge` confirmation is monkeypatched at the module
level (the same pattern used by the batch tests) so the test suite never
blocks on real stdin.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Generator
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

# Importing the package registers the operational commands.
import insto.commands  # noqa: F401  (side-effect import)
from insto.commands import operational as op_module
from insto.commands._base import (
    CommandUsageError,
    Session,
    dispatch,
)
from insto.config import Config
from insto.exceptions import SchemaDrift
from insto.models import Profile, Quota, Snapshot
from insto.service.facade import OsintFacade
from insto.service.history import HistoryStore
from tests.fakes import FakeBackend


@pytest.fixture
def history(tmp_path: Path) -> Generator[HistoryStore, None, None]:
    s = HistoryStore(tmp_path / "store.db")
    yield s
    s.close()


@pytest.fixture
def config(tmp_path: Path) -> Config:
    cfg = Config(
        hiker_token="abcd1234",
        output_dir=tmp_path / "output",
        db_path=tmp_path / "store.db",
        cli_history_path=tmp_path / "cli_history",
    )
    cfg.sources = {
        "hiker.token": "env",
        "hiker.proxy": "default",
        "output_dir": "default",
        "db_path": "default",
        "cli_history_path": "default",
    }
    return cfg


def _profile(pk: str, username: str) -> Profile:
    return Profile(pk=pk, username=username, access="public")


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend(
        profiles={"1": _profile("1", "alice")},
        quota=Quota.with_remaining(42, limit=100, reset_at=1700000000),
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
    return Console(record=True, color_system=None, width=160)


# ---------------------------------------------------------------------------
# /quota
# ---------------------------------------------------------------------------


async def test_quota_prints_quota_snapshot(
    facade: OsintFacade, session: Session, console: Console
) -> None:
    payload = await dispatch("/quota", facade=facade, session=session, console=console)
    assert payload == {"remaining": 42, "limit": 100, "reset_at": 1700000000}
    text = console.export_text()
    assert "remaining=42" in text
    assert "limit=100" in text


async def test_quota_unknown_renders_question_marks(
    backend: FakeBackend, history: HistoryStore, config: Config
) -> None:
    backend.quota = Quota.unknown()
    facade = OsintFacade(backend=backend, history=history, config=config)
    try:
        console = Console(record=True, color_system=None, width=160)
        await dispatch("/quota", facade=facade, session=Session(), console=console)
        text = console.export_text()
        assert "remaining=?" in text
    finally:
        await facade.watches.cancel_all()


async def test_quota_json_export_writes_envelope(
    facade: OsintFacade, session: Session, console: Console, tmp_path: Path
) -> None:
    dest = tmp_path / "quota.json"
    await dispatch(
        f"/quota --json {dest}", facade=facade, session=session, console=console
    )
    body = json.loads(dest.read_text())
    assert body["command"] == "quota"
    assert body["data"] == {"remaining": 42, "limit": 100, "reset_at": 1700000000}


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------


async def test_health_reports_backend_quota_and_no_error(
    facade: OsintFacade, session: Session, console: Console
) -> None:
    payload = await dispatch(
        "/health", facade=facade, session=session, console=console
    )
    assert payload["backend"] == "FakeBackend"
    assert payload["quota"] == {
        "remaining": 42,
        "limit": 100,
        "reset_at": 1700000000,
    }
    assert payload["last_error"] == "—"
    assert payload["schema_drifts"] == 0


async def test_health_surfaces_last_error_and_drift_counter(
    backend: FakeBackend, history: HistoryStore, config: Config
) -> None:
    backend._last_error = SchemaDrift(  # type: ignore[attr-defined]
        endpoint="user_by_username_v2", missing_field="pk"
    )
    facade = OsintFacade(backend=backend, history=history, config=config)
    try:
        console = Console(record=True, color_system=None, width=160)
        payload = await dispatch(
            "/health", facade=facade, session=Session(), console=console
        )
        assert payload["schema_drifts"] == 1
        assert "SchemaDrift" in payload["last_error"]
        assert "missing field" in payload["last_error"]
    finally:
        await facade.watches.cancel_all()


# ---------------------------------------------------------------------------
# /config
# ---------------------------------------------------------------------------


async def test_config_reports_each_key_with_origin(
    facade: OsintFacade, session: Session, console: Console
) -> None:
    rows = await dispatch(
        "/config", facade=facade, session=session, console=console
    )
    keys = {r["key"] for r in rows}
    assert {
        "hiker.token",
        "hiker.proxy",
        "output_dir",
        "db_path",
        "cli_history_path",
    } <= keys
    by_key = {r["key"]: r for r in rows}
    assert by_key["hiker.token"]["origin"] == "env"
    # token must be redacted, never displayed in full
    assert "abcd1234" not in (by_key["hiker.token"]["value"] or "")
    text = console.export_text()
    assert "abcd1234" not in text


async def test_config_json_export_round_trip(
    facade: OsintFacade, session: Session, console: Console, tmp_path: Path
) -> None:
    dest = tmp_path / "cfg.json"
    await dispatch(
        f"/config --json {dest}", facade=facade, session=session, console=console
    )
    body = json.loads(dest.read_text())
    assert body["command"] == "config"
    assert isinstance(body["data"], list)
    assert all({"key", "value", "origin"} <= set(row) for row in body["data"])


# ---------------------------------------------------------------------------
# /purge
# ---------------------------------------------------------------------------


async def test_purge_history_with_yes_flag_skips_confirmation(
    facade: OsintFacade, session: Session, console: Console
) -> None:
    await facade.history.record_command_async("/info", "alice")
    await facade.history.record_command_async("/posts", "alice")

    result = await dispatch(
        "/purge history --yes", facade=facade, session=session, console=console
    )
    assert result == {"kind": "history", "deleted": 2}
    assert facade.history.recent_commands(50) == []


async def test_purge_snapshots_with_user_filter(
    facade: OsintFacade, session: Session, console: Console
) -> None:
    snap = Snapshot(
        target_pk="1",
        captured_at=1700000000,
        profile_fields={"username": "alice"},
        last_post_pks=[],
    )
    other = Snapshot(
        target_pk="2",
        captured_at=1700000001,
        profile_fields={"username": "bob"},
        last_post_pks=[],
    )
    facade.history.add_snapshot(snap)
    facade.history.add_snapshot(other)

    result = await dispatch(
        "/purge snapshots --user 1 --yes",
        facade=facade,
        session=session,
        console=console,
    )
    assert result["kind"] == "snapshots"
    assert result["user"] == "1"
    assert result["deleted"] == 1
    assert facade.history.last_snapshot("1") is None
    assert facade.history.last_snapshot("2") is not None


async def test_purge_cache_wipes_history_and_snapshots(
    facade: OsintFacade, session: Session, console: Console
) -> None:
    await facade.history.record_command_async("/info", "alice")
    facade.history.add_snapshot(
        Snapshot(
            target_pk="1",
            captured_at=1700000000,
            profile_fields={"username": "alice"},
            last_post_pks=[],
        )
    )

    result = await dispatch(
        "/purge cache --yes", facade=facade, session=session, console=console
    )
    assert result["kind"] == "cache"
    assert result["cli_history_deleted"] == 1
    assert result["snapshots_deleted"] == 1


async def test_purge_user_filter_rejected_for_history(
    facade: OsintFacade, session: Session, console: Console
) -> None:
    with pytest.raises(CommandUsageError, match="--user can only be combined"):
        await dispatch(
            "/purge history --user 1 --yes",
            facade=facade,
            session=session,
            console=console,
        )


async def test_purge_unknown_kind_rejected_by_argparse(
    facade: OsintFacade, session: Session, console: Console
) -> None:
    with pytest.raises(CommandUsageError):
        await dispatch(
            "/purge bogus --yes",
            facade=facade,
            session=session,
            console=console,
        )


async def test_purge_aborts_when_user_declines(
    facade: OsintFacade,
    session: Session,
    console: Console,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await facade.history.record_command_async("/info", "alice")

    async def decline(_: Any, _msg: str) -> bool:
        return False

    monkeypatch.setattr(op_module, "_confirm", decline)
    result = await dispatch(
        "/purge history", facade=facade, session=session, console=console
    )
    assert result == {"kind": "history", "deleted": 0, "aborted": True}
    assert facade.history.recent_commands(50)  # untouched


async def test_purge_proceeds_when_user_confirms(
    facade: OsintFacade,
    session: Session,
    console: Console,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await facade.history.record_command_async("/info", "alice")

    async def accept(_: Any, _msg: str) -> bool:
        return True

    monkeypatch.setattr(op_module, "_confirm", accept)
    result = await dispatch(
        "/purge history", facade=facade, session=session, console=console
    )
    assert result["kind"] == "history"
    assert result["deleted"] == 1
    assert facade.history.recent_commands(50) == []
