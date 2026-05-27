"""Tests for `insto.ui.theme`: the named-theme registry and the palettes.

Themes only pin colour; the contract is that every registered theme resolves
the full set of style keys the renderers reference (`accent`, `logo.0`…`logo.4`,
…) and that lookups fall back to the default for unknown names.
"""

from __future__ import annotations

from insto.ui.theme import (
    DEFAULT_THEME_NAME,
    THEMES,
    get_palette,
    get_theme,
    is_known,
    list_themes,
)

# Style keys every theme must resolve (mirrors `_make_theme`).
_REQUIRED_KEYS = {
    "accent",
    "muted",
    "field",
    "value",
    "panel.border",
    "tip.cmd",
    "section",
    "logo.0",
    "logo.1",
    "logo.2",
    "logo.3",
    "logo.4",
}


def test_new_themes_registered() -> None:
    for name in ("hacker", "amber", "cyberpunk"):
        assert is_known(name), name
        assert name in THEMES
        assert name in list_themes()


def test_new_theme_accents() -> None:
    assert get_palette("hacker").accent == "#00FF41"  # matrix phosphor green
    assert get_palette("amber").accent == "#FFB000"  # CRT amber
    assert get_palette("cyberpunk").accent == "#00FFD1"  # neon cyan


def test_hacker_palette_is_matrix_green() -> None:
    p = get_palette("hacker")
    assert len(p.gradient) == 5
    # Phosphor gradient steps dark → bright green.
    assert p.gradient[0] == "#003B00"
    assert p.gradient[-1] == "#5CFF8F"


def test_every_theme_resolves_required_keys() -> None:
    for name in list_themes():
        styles = set(get_theme(name).styles)
        missing = _REQUIRED_KEYS - styles
        assert not missing, f"theme {name!r} missing style keys: {missing}"


def test_unknown_theme_falls_back_to_default() -> None:
    assert get_theme("nope") is THEMES[DEFAULT_THEME_NAME]
    assert get_palette(None) is get_palette(DEFAULT_THEME_NAME)
    assert not is_known("nope")
