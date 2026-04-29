"""Tests for the v0.7.x command additions.

`/where`, `/place`, `/placeposts`, `/postinfo`, `/pinned` — each is
exercised through `dispatch(...)` against a `FakeBackend` fixture so
we cover the command-layer plumbing (output formats, edge cases,
synthetic-target dirs) without touching the network.
"""

from __future__ import annotations

import json
from collections.abc import Generator
from pathlib import Path

import pytest

# Importing the package registers all command modules.
import insto.commands  # noqa: F401  (side-effect import)
from insto.commands._base import CommandUsageError, Session, dispatch
from insto.config import Config
from insto.exceptions import PostNotFound
from insto.models import Place, Post, Profile
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


def _profile() -> Profile:
    return Profile(pk="42", username="alice", access="public", full_name="Alice")


def _post(
    pk: str,
    *,
    code: str | None = None,
    location_name: str | None = None,
    location_pk: str | None = None,
    location_lat: float | None = None,
    location_lng: float | None = None,
) -> Post:
    return Post(
        pk=pk,
        code=code or f"C{pk}",
        taken_at=1_700_000_000,
        media_type="image",
        owner_username="alice",
        location_name=location_name,
        location_pk=location_pk,
        location_lat=location_lat,
        location_lng=location_lng,
    )


@pytest.fixture
def session() -> Session:
    s = Session()
    s.set_target("alice")
    return s


# ---------------------------------------------------------------------------
# /where
# ---------------------------------------------------------------------------


async def test_where_anchor_and_centroid(
    history: HistoryStore, config: Config, session: Session
) -> None:
    """Three Maranello posts + one Niseko: anchor = Maranello, big radius."""
    backend = FakeBackend(
        profiles={"42": _profile()},
        posts={
            "42": [
                _post("a", location_pk="L1", location_name="Maranello",
                      location_lat=44.5256, location_lng=10.8664),
                _post("b", location_pk="L1", location_name="Maranello",
                      location_lat=44.5256, location_lng=10.8664),
                _post("c", location_pk="L1", location_name="Maranello",
                      location_lat=44.5256, location_lng=10.8664),
                _post("d", location_pk="L2", location_name="Niseko",
                      location_lat=42.86, location_lng=140.71),
            ]
        },
    )
    facade = OsintFacade(backend=backend, history=history, config=config)
    result = await dispatch("/where --limit 10", facade=facade, session=session)
    assert result.geotagged == 4
    assert result.anchor is not None
    assert result.anchor.name == "Maranello"
    assert result.anchor.count == 3


async def test_where_zero_geotagged(
    history: HistoryStore, config: Config, session: Session
) -> None:
    """No GPS in any post → friendly message, no error."""
    backend = FakeBackend(
        profiles={"42": _profile()},
        posts={"42": [_post("a", location_name="Mystery")]},  # no lat/lng
    )
    facade = OsintFacade(backend=backend, history=history, config=config)
    result = await dispatch("/where --limit 10", facade=facade, session=session)
    assert result.geotagged == 0
    assert result.anchor is None


async def test_where_json_export(
    history: HistoryStore, config: Config, session: Session
) -> None:
    backend = FakeBackend(
        profiles={"42": _profile()},
        posts={
            "42": [
                _post("a", location_pk="L1", location_name="HQ",
                      location_lat=10.0, location_lng=20.0),
            ]
        },
    )
    facade = OsintFacade(backend=backend, history=history, config=config)
    await dispatch("/where --limit 5 --json", facade=facade, session=session)
    out_path = config.output_dir / "alice" / "where.json"
    assert out_path.exists()
    payload = json.loads(out_path.read_text())
    assert payload["data"]["geotagged"] == 1
    assert payload["data"]["anchor"]["name"] == "HQ"


# ---------------------------------------------------------------------------
# /place
# ---------------------------------------------------------------------------


async def test_place_search_returns_places(
    history: HistoryStore, config: Config, session: Session
) -> None:
    backend = FakeBackend(
        places={
            "tbilisi": [
                Place(pk="123", name="Tbilisi, Georgia", lat=41.69, lng=44.8),
                Place(pk="456", name="Tbilisi", lat=41.79, lng=44.79),
            ]
        }
    )
    facade = OsintFacade(backend=backend, history=history, config=config)
    out = await dispatch("/place tbilisi", facade=facade, session=session)
    assert len(out) == 2
    assert out[0].name == "Tbilisi, Georgia"


async def test_place_empty_query_rejected(
    history: HistoryStore, config: Config, session: Session
) -> None:
    backend = FakeBackend()
    facade = OsintFacade(backend=backend, history=history, config=config)
    with pytest.raises(CommandUsageError, match="non-empty query"):
        await dispatch('/place ""', facade=facade, session=session)


async def test_place_maltego_export(
    history: HistoryStore, config: Config, session: Session
) -> None:
    backend = FakeBackend(
        places={"tbilisi": [Place(pk="123", name="Tbilisi", lat=41.69, lng=44.8)]}
    )
    facade = OsintFacade(backend=backend, history=history, config=config)
    await dispatch("/place tbilisi --maltego", facade=facade, session=session)
    out_path = config.output_dir / "tbilisi" / "place.maltego.csv"
    assert out_path.exists()
    rows = out_path.read_text().splitlines()
    assert rows[0] == "Type,Value,Weight,Notes,Properties"
    assert "maltego.GPS" in rows[1] and "Tbilisi" in rows[1]


# ---------------------------------------------------------------------------
# /placeposts
# ---------------------------------------------------------------------------


async def test_placeposts_returns_media(
    history: HistoryStore, config: Config, session: Session
) -> None:
    backend = FakeBackend(
        place_posts={"123": [_post("a"), _post("b"), _post("c")]},
    )
    facade = OsintFacade(backend=backend, history=history, config=config)
    out = await dispatch("/placeposts 123 2", facade=facade, session=session)
    assert [p.pk for p in out] == ["a", "b"]


async def test_placeposts_empty_pk_rejected(
    history: HistoryStore, config: Config, session: Session
) -> None:
    backend = FakeBackend()
    facade = OsintFacade(backend=backend, history=history, config=config)
    with pytest.raises(CommandUsageError, match="needs a location pk"):
        await dispatch('/placeposts ""', facade=facade, session=session)


# ---------------------------------------------------------------------------
# /postinfo
# ---------------------------------------------------------------------------


async def test_postinfo_resolves_by_code(
    history: HistoryStore, config: Config, session: Session
) -> None:
    """Code passed as ref → fake returns the matching Post DTO."""
    target_post = _post("999", code="DXPduuvEY7S")
    backend = FakeBackend(posts_by_ref={"DXPduuvEY7S": target_post})
    facade = OsintFacade(backend=backend, history=history, config=config)
    out = await dispatch("/postinfo DXPduuvEY7S", facade=facade, session=session)
    assert out.code == "DXPduuvEY7S"
    assert out.pk == "999"


async def test_postinfo_unknown_ref_raises(
    history: HistoryStore, config: Config, session: Session
) -> None:
    backend = FakeBackend()  # empty posts_by_ref
    facade = OsintFacade(backend=backend, history=history, config=config)
    with pytest.raises(PostNotFound):
        await dispatch("/postinfo NoSuchCode", facade=facade, session=session)


async def test_postinfo_empty_ref_rejected(
    history: HistoryStore, config: Config, session: Session
) -> None:
    backend = FakeBackend()
    facade = OsintFacade(backend=backend, history=history, config=config)
    with pytest.raises(CommandUsageError, match="needs a URL"):
        await dispatch('/postinfo ""', facade=facade, session=session)


async def test_postinfo_json_export(
    history: HistoryStore, config: Config, session: Session
) -> None:
    target_post = _post("999", code="DXPduuvEY7S")
    backend = FakeBackend(posts_by_ref={"DXPduuvEY7S": target_post})
    facade = OsintFacade(backend=backend, history=history, config=config)
    await dispatch("/postinfo DXPduuvEY7S --json", facade=facade, session=session)
    out_path = config.output_dir / "DXPduuvEY7S" / "postinfo.json"
    assert out_path.exists()
    data = json.loads(out_path.read_text())
    assert data["data"]["pk"] == "999"


# ---------------------------------------------------------------------------
# /pinned
# ---------------------------------------------------------------------------


async def test_pinned_returns_posts(
    history: HistoryStore, config: Config, session: Session
) -> None:
    backend = FakeBackend(
        profiles={"42": _profile()},
        pinned={"42": [_post("p1"), _post("p2"), _post("p3")]},
    )
    facade = OsintFacade(backend=backend, history=history, config=config)
    out = await dispatch("/pinned", facade=facade, session=session)
    assert [p.pk for p in out] == ["p1", "p2", "p3"]


async def test_pinned_empty_friendly(
    history: HistoryStore, config: Config, session: Session
) -> None:
    backend = FakeBackend(profiles={"42": _profile()})  # empty pinned
    facade = OsintFacade(backend=backend, history=history, config=config)
    out = await dispatch("/pinned", facade=facade, session=session)
    assert out == []
