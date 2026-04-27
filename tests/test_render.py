"""Snapshot-style tests for `insto.ui.render` and `insto.ui.banner`.

We render to a `Console(width=N, force_terminal=True, record=True)` and
read back the plain text via `Console.export_text(styles=False)` so the
assertions don't depend on terminal colour codes. The point is to verify
*structure and content* (key fields, expected rows, fallback layouts at
narrow widths), not exact pixel output.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from rich.console import Console

from insto.config import Config
from insto.models import Highlight, HighlightItem, Post, Profile, Quota, User
from insto.service.facade import OsintFacade
from insto.service.history import HistoryStore
from insto.ui.banner import WASP_BANNER, render_welcome
from insto.ui.render import (
    render_highlights_tree,
    render_kv,
    render_media_grid,
    render_profile,
    render_user_table,
)
from insto.ui.theme import INSTO_THEME, make_console
from tests.fakes import FakeBackend


def _capture(renderable: object, width: int = 100) -> str:
    console = Console(
        theme=INSTO_THEME,
        width=width,
        force_terminal=True,
        color_system="truecolor",
        record=True,
    )
    console.print(renderable)
    return console.export_text(styles=False)


# ---------------------------------------------------------------- render_profile


def test_render_profile_panel_includes_key_fields() -> None:
    profile = Profile(
        pk="42",
        username="alice",
        access="public",
        full_name="Alice Doe",
        biography="hi there",
        external_url="https://example.com",
        is_verified=True,
        is_business=False,
        follower_count=12345,
        following_count=42,
        media_count=7,
        public_email="alice@example.com",
        previous_usernames=["alic3"],
    )
    out = _capture(render_profile(profile, about={"country_code": "DE"}))
    assert "@alice" in out
    assert "[public]" in out
    assert "Alice Doe" in out
    assert "hi there" in out
    assert "12 345" in out  # formatted follower count with non-breaking-style spaces
    assert "alice@example.com" in out
    assert "alic3" in out
    assert "DE" in out


def test_render_profile_handles_empty_about_and_blanks() -> None:
    profile = Profile(pk="1", username="empty", access="private")
    out = _capture(render_profile(profile, about=None))
    assert "@empty" in out
    assert "[private]" in out


# ------------------------------------------------------------ render_user_table


def test_render_user_table_lists_each_user() -> None:
    users = [
        User(pk="1", username="bob", full_name="Bob"),
        User(pk="2", username="carol", is_private=True, is_verified=True),
    ]
    out = _capture(render_user_table(users, title="followers"))
    assert "@bob" in out
    assert "@carol" in out
    assert "followers" in out
    # private/verified yes/no cells render
    assert "yes" in out and "no" in out


def test_render_user_table_empty_still_renders_header() -> None:
    out = _capture(render_user_table([], title="followers"))
    assert "username" in out
    assert "followers" in out


# ------------------------------------------------------------- render_media_grid


def test_render_media_grid_shows_codes_and_metrics() -> None:
    posts = [
        Post(
            pk="p1",
            code="ABC",
            taken_at=1_700_000_000,
            media_type="image",
            caption="hello world #python",
            like_count=100,
            comment_count=3,
        ),
        Post(
            pk="p2",
            code="DEF",
            taken_at=1_700_100_000,
            media_type="video",
            caption="line1\nline2",
            like_count=2_500,
            comment_count=12,
        ),
    ]
    out = _capture(render_media_grid(posts, title="posts"))
    assert "ABC" in out
    assert "DEF" in out
    assert "image" in out
    assert "video" in out
    assert "100" in out
    assert "2 500" in out
    # newlines collapse in caption preview
    assert "line1 line2" in out


# --------------------------------------------------------- render_highlights_tree


def test_render_highlights_tree_collapsed_and_expanded() -> None:
    highlights = [Highlight(pk="h1", title="Trip", item_count=2)]
    items = {
        "h1": [
            HighlightItem(
                pk="i1",
                highlight_pk="h1",
                taken_at=1_700_000_000,
                media_type="image",
                media_url="https://x/i1",
            ),
            HighlightItem(
                pk="i2",
                highlight_pk="h1",
                taken_at=1_700_000_100,
                media_type="video",
                media_url="https://x/i2",
            ),
        ]
    }
    collapsed = _capture(render_highlights_tree(highlights), width=80)
    assert "Trip" in collapsed
    assert "2 items" in collapsed
    expanded = _capture(render_highlights_tree(highlights, items), width=80)
    assert "i1" in expanded
    assert "i2" in expanded
    assert "video" in expanded


# ------------------------------------------------------------------- render_kv


def test_render_kv_renders_top_list() -> None:
    rows = [("python", 12), ("osint", 8), ("berlin", 3)]
    out = _capture(render_kv(rows, title="hashtags", key_label="tag", value_label="count"))
    assert "python" in out
    assert "12" in out
    assert "osint" in out
    assert "tag" in out
    assert "count" in out


# ----------------------------------------------------------------- render_welcome


@pytest.fixture
def _facade(tmp_path: Path) -> Iterator[OsintFacade]:
    history = HistoryStore(tmp_path / "store.db")
    config = Config(output_dir=tmp_path / "output", db_path=tmp_path / "store.db")
    backend = FakeBackend(quota=Quota(remaining=100, limit=100))
    facade = OsintFacade(backend=backend, history=history, config=config)
    yield facade
    history.close()


def test_welcome_wide_terminal_shows_two_columns(_facade: OsintFacade) -> None:
    out = _capture(render_welcome(_facade, width=120, email="me@example.com"), width=120)
    assert "insto v0.1.0" in out
    assert "Tips for getting started" in out
    assert "/target <user>" in out
    assert "Recent activity" in out
    assert "No recent activity" in out
    assert "100/100 quota" in out
    assert "me@example.com" in out
    # wasp banner present
    assert "##" in out


def test_welcome_includes_recent_targets(_facade: OsintFacade) -> None:
    _facade.history.record_command("info", "@alice")
    _facade.history.record_command("info", "@bob")
    out = _capture(render_welcome(_facade, width=120), width=120)
    assert "@alice" in out
    assert "@bob" in out


def test_welcome_narrow_terminal_drops_tips_panel(_facade: OsintFacade) -> None:
    out = _capture(render_welcome(_facade, width=80), width=80)
    assert "##" in out  # banner still rendered
    assert "Tips for getting started" not in out
    assert "Recent activity" not in out


def test_welcome_tiny_terminal_falls_back_to_status_line(_facade: OsintFacade) -> None:
    out = _capture(render_welcome(_facade, width=50, target="@alice"), width=50)
    assert "insto v0.1.0" in out
    assert "hiker" in out
    assert "@alice" in out
    # no panel border in the tiny fallback
    assert "Tips for getting started" not in out


def test_welcome_quota_unknown_does_not_crash(tmp_path: Path) -> None:
    history = HistoryStore(tmp_path / "store.db")
    config = Config(output_dir=tmp_path / "output", db_path=tmp_path / "store.db")
    backend = FakeBackend()  # default Quota.unknown()
    facade = OsintFacade(backend=backend, history=history, config=config)
    try:
        out = _capture(render_welcome(facade, width=120), width=120)
        assert "quota: unknown" in out
    finally:
        history.close()


# ----------------------------------------------------------------------- theme


def test_make_console_attaches_theme() -> None:
    console = make_console(width=80)
    assert console.width == 80
    # accent style resolves through the theme registry
    style = console.get_style("accent")
    assert style.color is not None


def test_wasp_banner_is_static_ascii() -> None:
    # banner must be plain ASCII (no chafa-specific runtime escapes)
    WASP_BANNER.encode("ascii")
    assert WASP_BANNER.count("\n") >= 12  # ~16 rows
