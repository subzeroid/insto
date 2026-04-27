"""Tests for `insto.commands.dossier`: the killer-feature `/dossier` command.

Coverage:

  - happy-path with `--no-download` (FakeBackend, no CDN client wired)
  - happy-path with media downloads (httpx MockTransport)
  - **fail-fast pre-flight** for every non-public access state — asserts
    that NO dossier directory is created and only the three pre-flight
    backend calls happen
  - **disk pre-check** — monkeypatched `shutil.disk_usage` returning
    < 2GB free aborts the run before any directory is created
  - **partial mode** when a single section fails (QuotaExhausted on
    `iter_user_tagged`) — MANIFEST flips `partial: true`, the failed
    section is enumerated, the rest still produce output files
  - directory layout matches the spec
  - MANIFEST.md contains every required field
"""

from __future__ import annotations

import json
from collections import namedtuple
from collections.abc import Generator
from pathlib import Path

import httpx
import pytest

# Importing the package registers all command modules (incl. /dossier).
import insto.commands  # noqa: F401
from insto.commands import dossier as dossier_mod
from insto.commands._base import CommandUsageError, Session, dispatch
from insto.config import Config
from insto.exceptions import QuotaExhausted
from insto.models import Comment, Post, Profile, User
from insto.service.facade import OsintFacade
from insto.service.history import HistoryStore
from tests.fakes import FakeBackend

# JPEG magic + padding to satisfy CDN sniff (≥ 512 bytes, valid magic).
_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 1024


_FakeUsage = namedtuple("_FakeUsage", ["total", "used", "free"])


@pytest.fixture
def history(tmp_path: Path) -> Generator[HistoryStore, None, None]:
    store = HistoryStore(tmp_path / "store.db")
    yield store
    store.close()


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(output_dir=tmp_path / "output", db_path=tmp_path / "store.db")


def _profile(access: str = "public") -> Profile:
    return Profile(
        pk="42",
        username="alice",
        access=access,  # type: ignore[arg-type]
        full_name="Alice Doe",
        avatar_url="https://scontent.cdninstagram.com/alice.jpg",
    )


def _post(
    pk: str,
    *,
    code: str,
    owner: str = "alice",
    taken_at: int = 1_700_000_000,
) -> Post:
    return Post(
        pk=pk,
        code=code,
        taken_at=taken_at,
        media_type="image",
        media_urls=[f"https://scontent.cdninstagram.com/{pk}.jpg"],
        hashtags=["travel", "food"],
        mentions=["bob"],
        location_name="Paris",
        owner_pk="42",
        owner_username=owner,
    )


def _user(pk: str, username: str) -> User:
    return User(pk=pk, username=username, full_name=username.title())


@pytest.fixture
def backend() -> FakeBackend:
    posts = [_post(f"p{i}", code=f"C{i}") for i in range(1, 6)]
    followers_list = [_user(f"f{i}", f"flw{i}") for i in range(1, 4)]
    following_list = [_user(f"f{i}", f"flw{i}") for i in range(1, 3)]  # 2 mutuals
    tagged = [_post(f"t{i}", code=f"T{i}", owner=f"tagger{i}") for i in range(1, 4)]
    comments = {
        "p1": [
            Comment(
                pk="c1",
                media_pk="p1",
                user_pk="u1",
                user_username="bob",
                text="hi",
                created_at=1,
            )
        ],
        "p2": [
            Comment(
                pk="c2",
                media_pk="p2",
                user_pk="u1",
                user_username="bob",
                text="hey",
                created_at=2,
            )
        ],
    }
    return FakeBackend(
        profiles={"42": _profile()},
        abouts={"42": {"is_eligible_to_show_email": True}},
        posts={"42": posts},
        followers={"42": followers_list},
        following={"42": following_list},
        tagged={"42": tagged},
        comments=comments,
    )


@pytest.fixture
def session() -> Session:
    s = Session()
    s.set_target("alice")
    return s


def _make_facade(
    backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    *,
    with_cdn: bool = False,
) -> OsintFacade:
    if with_cdn:

        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=_JPEG,
                headers={
                    "content-type": "image/jpeg",
                    "content-length": str(len(_JPEG)),
                },
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return OsintFacade(backend=backend, history=history, config=config, cdn_client=client)
    return OsintFacade(backend=backend, history=history, config=config)


# ---------------------------------------------------------------------------
# Pre-flight: non-public profiles must abort BEFORE any other request.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("access", ["private", "deleted", "blocked", "followed"])
async def test_dossier_fail_fast_non_public_profile_creates_nothing(
    backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
    access: str,
) -> None:
    backend.profiles["42"] = _profile(access=access)
    facade = _make_facade(backend, history, config)

    with pytest.raises(CommandUsageError) as excinfo:
        await dispatch("/dossier --no-download --yes", facade=facade, session=session)
    assert f"profile is {access}" in str(excinfo.value)
    assert "@alice" in str(excinfo.value)

    # No dossier directory should exist anywhere.
    assert not (config.output_dir / "alice" / "dossier").exists()

    # Only the three pre-flight calls happened.
    methods = [name for name, _ in backend.request_log]
    assert methods == ["resolve_target", "get_profile", "get_user_about"]


# ---------------------------------------------------------------------------
# Disk pre-check
# ---------------------------------------------------------------------------


async def test_dossier_aborts_when_disk_below_2gb(
    backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    facade = _make_facade(backend, history, config)
    # 100MB free → well below the 2GB threshold.
    monkeypatch.setattr(
        dossier_mod.shutil,
        "disk_usage",
        lambda _p: _FakeUsage(total=10**12, used=10**12 - 10**8, free=10**8),
    )

    with pytest.raises(CommandUsageError, match="insufficient disk space"):
        await dispatch("/dossier --no-download --yes", facade=facade, session=session)

    # Pre-flight calls happened, but no dossier directory was created.
    assert not (config.output_dir / "alice" / "dossier").exists()


# ---------------------------------------------------------------------------
# Happy-path: layout, MANIFEST, profile contents, no media (--no-download)
# ---------------------------------------------------------------------------


async def test_dossier_happy_path_no_download_creates_full_layout(
    backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
) -> None:
    facade = _make_facade(backend, history, config)
    out = await dispatch("/dossier --no-download --yes", facade=facade, session=session)
    assert isinstance(out, Path)
    assert out.exists() and out.is_dir()

    # Required files
    expected_files = {
        "profile.json",
        "posts.json",
        "followers.csv",
        "following.csv",
        "mutuals.csv",
        "hashtags.csv",
        "mentions.csv",
        "locations.csv",
        "wcommented.csv",
        "wtagged.csv",
        "MANIFEST.md",
    }
    actual_files = {p.name for p in out.iterdir() if p.is_file()}
    assert expected_files <= actual_files

    # No media subdirectory in --no-download mode.
    assert not (out / "posts").exists()

    # profile.json carries the schema envelope.
    payload = json.loads((out / "profile.json").read_text())
    assert payload["_schema"] == "insto.v1"
    assert payload["target"] == "alice"
    assert payload["data"]["profile"]["username"] == "alice"
    assert payload["data"]["about"]["is_eligible_to_show_email"] is True

    # MANIFEST.md has every required field and lists every section.
    manifest = (out / "MANIFEST.md").read_text()
    assert "# insto dossier — @alice" in manifest
    assert "captured_at:" in manifest
    assert "schema: insto.v1" in manifest
    assert "partial: false" in manifest
    assert "duration_seconds:" in manifest
    for section in (
        "profile",
        "posts",
        "followers",
        "following",
        "mutuals",
        "hashtags",
        "mentions",
        "locations",
        "wcommented",
        "wtagged",
    ):
        assert f"**{section}**" in manifest
    assert "total_files:" in manifest
    assert "total_bytes:" in manifest

    # mutuals: 2 users (intersection of {f1,f2,f3} and {f1,f2}).
    mutuals_csv = (out / "mutuals.csv").read_text().splitlines()
    # Header + 2 rows.
    assert len(mutuals_csv) == 3


async def test_dossier_directory_named_with_utc_timestamp(
    backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
) -> None:
    facade = _make_facade(backend, history, config)
    out = await dispatch("/dossier --no-download --yes", facade=facade, session=session)
    # Format is YYYYMMDDTHHMMSSZ — 16 chars, ends with Z.
    assert out.parent == config.output_dir / "alice" / "dossier"
    name = out.name
    assert len(name) == 16 and name.endswith("Z") and "T" in name


# ---------------------------------------------------------------------------
# Happy-path with media downloads
# ---------------------------------------------------------------------------


async def test_dossier_with_media_download_writes_files(
    backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
) -> None:
    facade = _make_facade(backend, history, config, with_cdn=True)
    try:
        out = await dispatch("/dossier --yes", facade=facade, session=session)
    finally:
        await facade.aclose()

    media_dir = out / "posts"
    assert media_dir.exists()
    files = sorted(p.name for p in media_dir.iterdir() if p.is_file())
    # 5 posts, 1 url each, 5 files written (extension picked from JPEG sniff).
    assert len(files) == 5
    for name in files:
        assert name.endswith(".jpg")

    # MANIFEST mentions the media folder when media was actually written.
    manifest = (out / "MANIFEST.md").read_text()
    assert "**posts/**" in manifest


# ---------------------------------------------------------------------------
# Partial mode on QuotaExhausted in one section.
# ---------------------------------------------------------------------------


async def test_dossier_partial_when_one_section_quota_exhausted(
    backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
) -> None:
    # `iter_user_tagged` is consumed only by `_do_wtagged`, so the failure
    # is deterministic: only that section errors, every other one writes
    # its file as usual.
    backend.errors.iter_user_tagged = QuotaExhausted("monthly limit hit")
    facade = _make_facade(backend, history, config)
    out = await dispatch("/dossier --no-download --yes", facade=facade, session=session)

    manifest = (out / "MANIFEST.md").read_text()
    assert "partial: true" in manifest
    assert "**wtagged** — failed:" in manifest
    assert "QuotaExhausted" in manifest

    # Other sections still produced their files.
    for name in (
        "profile.json",
        "posts.json",
        "followers.csv",
        "following.csv",
        "mutuals.csv",
        "hashtags.csv",
        "mentions.csv",
        "locations.csv",
        "wcommented.csv",
    ):
        assert (out / name).exists(), f"{name} should still be written"
    # The failed section produces no file.
    assert not (out / "wtagged.csv").exists()


# ---------------------------------------------------------------------------
# Limit override: --limit shrinks every collection equally.
# ---------------------------------------------------------------------------


async def test_dossier_limit_override_caps_collections(
    backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    session: Session,
) -> None:
    facade = _make_facade(backend, history, config)
    out = await dispatch(
        "/dossier --no-download --yes --limit 2",
        facade=facade,
        session=session,
    )
    posts_payload = json.loads((out / "posts.json").read_text())
    assert len(posts_payload["data"]) == 2

    followers_lines = (out / "followers.csv").read_text().splitlines()
    # Header + at most 2 user rows.
    assert len(followers_lines) <= 3

    manifest = (out / "MANIFEST.md").read_text()
    # Truncation should be flagged on collections that hit the cap.
    assert "truncated=true" in manifest
