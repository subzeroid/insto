"""Tests for `insto.commands.profile`: /info, /propic, /email, /phone, /export.

Each test runs through `dispatch(...)` so the parser, registry, and
session-state plumbing are exercised end-to-end. A recording rich
`Console` captures user-visible output; a `MockTransport`-backed
`OsintFacade` is used whenever a propic actually needs to be streamed
to disk.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Generator
from pathlib import Path
from typing import Any

import httpx
import pytest
from rich.console import Console

# Importing the package registers /info, /propic, /email, /phone, /export.
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
from insto.ui.theme import INSTO_THEME
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


def _public_profile() -> Profile:
    return Profile(
        pk="42",
        username="alice",
        access="public",
        full_name="Alice Doe",
        biography="hi",
        public_email="alice@example.com",
        public_phone="+10000000000",
        avatar_url="https://scontent.cdninstagram.com/alice.jpg",
        follower_count=100,
        following_count=50,
        media_count=7,
    )


def _private_profile() -> Profile:
    return Profile(
        pk="55",
        username="carol",
        access="private",
        is_private=True,
        avatar_url="https://scontent.cdninstagram.com/carol.jpg",
    )


def _deleted_profile() -> Profile:
    return Profile(pk="0", username="ghost", access="deleted")


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend(
        profiles={
            "42": _public_profile(),
            "55": _private_profile(),
            "0": _deleted_profile(),
        },
        abouts={
            "42": {"country_code": "DE", "is_eligible_to_show_email": True},
            "55": {},
            "0": {},
        },
    )


@pytest.fixture
def facade(backend: FakeBackend, history: HistoryStore, config: Config) -> OsintFacade:
    return OsintFacade(backend=backend, history=history, config=config)


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


# ---------------------------------------------------------------------------
# /info
# ---------------------------------------------------------------------------


async def test_info_renders_panel_for_public_profile(
    facade: OsintFacade,
    session: Session,
    recording_console: Console,
) -> None:
    session.set_target("alice")
    profile, about = await dispatch(
        "/info", facade=facade, session=session, console=recording_console
    )
    assert profile.username == "alice"
    assert about == {"country_code": "DE", "is_eligible_to_show_email": True}
    out = _captured(recording_console)
    assert "@alice" in out
    assert "[public]" in out
    assert "Alice Doe" in out
    assert "alice@example.com" in out
    assert "DE" in out


async def test_info_for_private_profile_renders(
    facade: OsintFacade,
    session: Session,
    recording_console: Console,
) -> None:
    session.set_target("carol")
    profile, _about = await dispatch(
        "/info", facade=facade, session=session, console=recording_console
    )
    assert profile.access == "private"
    out = _captured(recording_console)
    assert "@carol" in out
    assert "[private]" in out


async def test_info_for_deleted_profile_renders(
    facade: OsintFacade,
    session: Session,
    recording_console: Console,
) -> None:
    session.set_target("ghost")
    profile, _about = await dispatch(
        "/info", facade=facade, session=session, console=recording_console
    )
    assert profile.access == "deleted"
    out = _captured(recording_console)
    assert "@ghost" in out
    assert "[deleted]" in out


async def test_info_json_writes_default_path(
    facade: OsintFacade,
    config: Config,
    session: Session,
) -> None:
    session.set_target("alice")
    await dispatch("/info --json", facade=facade, session=session)
    expected = config.output_dir / "alice" / "info.json"
    assert expected.exists()
    payload = json.loads(expected.read_text())
    assert payload["_schema"] == "insto.v1"
    assert payload["command"] == "info"
    assert payload["target"] == "alice"
    assert payload["data"]["profile"]["username"] == "alice"
    assert payload["data"]["about"]["country_code"] == "DE"


async def test_info_json_dash_writes_to_stdout(
    facade: OsintFacade,
    session: Session,
    capsysbinary: pytest.CaptureFixture[bytes],
) -> None:
    session.set_target("alice")
    await dispatch("/info --json -", facade=facade, session=session)
    blob = capsysbinary.readouterr().out
    payload = json.loads(blob)
    assert payload["data"]["profile"]["username"] == "alice"


async def test_info_explicit_path(
    facade: OsintFacade,
    session: Session,
    tmp_path: Path,
) -> None:
    out_path = tmp_path / "custom.json"
    session.set_target("alice")
    await dispatch(f"/info --json {out_path}", facade=facade, session=session)
    assert out_path.exists()


async def test_info_requires_target(
    facade: OsintFacade,
    session: Session,
) -> None:
    with pytest.raises(CommandUsageError, match="no target set"):
        await dispatch("/info", facade=facade, session=session)


# ---------------------------------------------------------------------------
# /propic
# ---------------------------------------------------------------------------


async def test_propic_no_download_prints_url(
    backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
    capsys: pytest.CaptureFixture[str],
    recording_console: Console,
) -> None:
    facade = OsintFacade(backend=backend, history=history, config=config)
    session.set_target("alice")
    out = await dispatch(
        "/propic --no-download",
        facade=facade,
        session=session,
        console=recording_console,
    )
    assert out is None
    captured = capsys.readouterr().out.strip().splitlines()
    assert captured == ["https://scontent.cdninstagram.com/alice.jpg"]


async def test_propic_streams_to_disk(
    backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
    recording_console: Console,
) -> None:
    body = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 1024

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=body,
            headers={"content-type": "image/jpeg", "content-length": str(len(body))},
        )

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    facade = OsintFacade(backend=backend, history=history, config=config, cdn_client=client)
    try:
        session.set_target("alice")
        out = await dispatch("/propic", facade=facade, session=session, console=recording_console)
        assert isinstance(out, Path)
        assert out.exists()
        assert out.parent == config.output_dir / "alice" / "propic"
        assert out.read_bytes().startswith(b"\xff\xd8")
        assert "saved" in _captured(recording_console)
    finally:
        await facade.aclose()


async def test_propic_missing_url_reports(
    facade: OsintFacade,
    session: Session,
    recording_console: Console,
) -> None:
    session.set_target("ghost")  # deleted profile, no avatar
    out = await dispatch("/propic", facade=facade, session=session, console=recording_console)
    assert out is None
    assert "no profile picture URL" in _captured(recording_console)


async def test_propic_sanitizes_drifted_pk(
    backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
    recording_console: Console,
) -> None:
    """A drifted backend returning `pk='../etc'` must not escape output_dir."""
    backend.profiles["42"] = Profile(
        pk="../../../etc/passwd",
        username="alice",
        access="public",
        full_name="Alice",
        avatar_url="https://scontent.cdninstagram.com/a.jpg",
    )

    body = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 1024

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=body,
            headers={"content-type": "image/jpeg", "content-length": str(len(body))},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    facade = OsintFacade(backend=backend, history=history, config=config, cdn_client=client)
    try:
        session.set_target("alice")
        out = await dispatch("/propic", facade=facade, session=session, console=recording_console)
        assert isinstance(out, Path)
        assert out.is_relative_to(config.output_dir)
        assert ".." not in out.parts
    finally:
        await facade.aclose()


# ---------------------------------------------------------------------------
# /email
# ---------------------------------------------------------------------------


async def test_email_prints_value(
    facade: OsintFacade,
    session: Session,
    recording_console: Console,
) -> None:
    session.set_target("alice")
    payload = await dispatch("/email", facade=facade, session=session, console=recording_console)
    assert payload["email"] == "alice@example.com"
    out = _captured(recording_console)
    assert "alice@example.com" in out


async def test_email_for_private_reports_no_public(
    facade: OsintFacade,
    session: Session,
    recording_console: Console,
) -> None:
    session.set_target("carol")
    payload = await dispatch("/email", facade=facade, session=session, console=recording_console)
    assert payload["email"] is None
    out = _captured(recording_console)
    assert "private" in out
    assert "no public email" in out


async def test_email_for_deleted_reports(
    facade: OsintFacade,
    session: Session,
    recording_console: Console,
) -> None:
    session.set_target("ghost")
    await dispatch("/email", facade=facade, session=session, console=recording_console)
    out = _captured(recording_console)
    assert "deleted" in out


async def test_email_json_export(
    facade: OsintFacade,
    config: Config,
    session: Session,
) -> None:
    session.set_target("alice")
    await dispatch("/email --json", facade=facade, session=session)
    path = config.output_dir / "alice" / "email.json"
    payload = json.loads(path.read_text())
    assert payload["data"]["email"] == "alice@example.com"
    assert payload["data"]["username"] == "alice"


# ---------------------------------------------------------------------------
# /phone
# ---------------------------------------------------------------------------


async def test_phone_prints_value(
    facade: OsintFacade,
    session: Session,
    recording_console: Console,
) -> None:
    session.set_target("alice")
    payload = await dispatch("/phone", facade=facade, session=session, console=recording_console)
    assert payload["phone"] == "+10000000000"
    assert "+10000000000" in _captured(recording_console)


async def test_phone_for_private_reports_no_public(
    facade: OsintFacade,
    session: Session,
    recording_console: Console,
) -> None:
    session.set_target("carol")
    payload = await dispatch("/phone", facade=facade, session=session, console=recording_console)
    assert payload["phone"] is None
    out = _captured(recording_console)
    assert "no public phone" in out


# ---------------------------------------------------------------------------
# /export
# ---------------------------------------------------------------------------


async def test_export_writes_default_path(
    facade: OsintFacade,
    config: Config,
    session: Session,
) -> None:
    session.set_target("alice")
    out = await dispatch("/export", facade=facade, session=session)
    assert out == config.output_dir / "alice" / "export.json"
    payload = json.loads(out.read_text())
    assert payload["command"] == "export"
    assert payload["target"] == "alice"
    assert payload["data"]["profile"]["username"] == "alice"
    assert payload["data"]["about"]["country_code"] == "DE"


async def test_export_explicit_path(
    facade: OsintFacade,
    session: Session,
    tmp_path: Path,
) -> None:
    target = tmp_path / "deep" / "alice.json"
    session.set_target("alice")
    out = await dispatch(f"/export --json {target}", facade=facade, session=session)
    assert out == target
    assert target.exists()


async def test_export_dash_to_stdout(
    facade: OsintFacade,
    session: Session,
    capsysbinary: pytest.CaptureFixture[bytes],
) -> None:
    session.set_target("alice")
    await dispatch("/export --json -", facade=facade, session=session)
    blob = capsysbinary.readouterr().out
    payload = json.loads(blob)
    assert payload["data"]["profile"]["username"] == "alice"


async def test_export_rejects_csv(
    facade: OsintFacade,
    session: Session,
) -> None:
    session.set_target("alice")
    with pytest.raises(CommandUsageError):
        # /export is not in CSV_FLAT_COMMANDS — parse-time rejection.
        await dispatch("/export --csv -", facade=facade, session=session)


async def test_export_rejects_maltego(
    facade: OsintFacade,
    session: Session,
) -> None:
    session.set_target("alice")
    with pytest.raises(CommandUsageError, match="cannot be exported as Maltego"):
        await dispatch("/export --maltego", facade=facade, session=session)


# ---------------------------------------------------------------------------
# Positional target argument support
# ---------------------------------------------------------------------------


async def test_info_accepts_positional_target_via_session(
    facade: OsintFacade, session: Session
) -> None:
    """Sanity: with_target works the same way it does in target.py.

    /info reads from the session — set the target via /target first.
    """
    await dispatch("/target alice", facade=facade, session=session)
    profile, _ = await dispatch("/info", facade=facade, session=session)
    assert profile.username == "alice"


# ---------------------------------------------------------------------------
# Misc — global flag plumbing
# ---------------------------------------------------------------------------


async def test_info_global_limit_does_not_break(facade: OsintFacade, session: Session) -> None:
    """`--limit` is a no-op for profile commands but must not error."""
    session.set_target("alice")
    profile, _ = await dispatch("/info --limit 5", facade=facade, session=session)
    assert profile.username == "alice"


def _ensure_clean_stdout() -> None:
    # pytest captures stdout by default; nothing to clean — kept here for
    # symmetry with future tests that may need it.
    sys.stdout.flush()


__all__: list[str] = []  # exposed for type-checker only

# Force `Any` import to be considered used in case future tests need it.
_TYPE_KEEPER: Any = None
