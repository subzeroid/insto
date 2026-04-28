"""Named colour themes for insto.

Five themes are baked at build time and selected at runtime via `/theme
<name>` or `[theme] = "<name>"` in `~/.insto/config.toml`. Themes only
control colour: they pin the same set of `rich.Theme` keys so every
renderer (`render_profile`, `_tips_table`, `BottomToolbar`, popup style,
etc.) just references `accent` / `muted` / `tip.cmd` / `panel.border`
and picks up whatever the active theme defines.

Adding a theme:

1. Pick 5 colours: ACCENT (single anchor), GRADIENT (5-stop list, used
   by the figlet logo), MUTED, FIELD, VALUE.
2. Build a Theme via `_make_theme(...)`.
3. Register in `THEMES`.
4. Mention in `docs/cli-reference.md` under `/theme`.

The `instagram` theme uses the 2022+ Instagram brand gradient
(yellow → orange → pink → magenta → violet). `claude` mirrors Claude
Code's burnt-orange palette. `default` is a neutral cyan/orange that
reads well on both dark and light terminals. `mono` switches everything
off for accessibility and screen recordings. `matrix` is the joke.
"""

from __future__ import annotations

from typing import NamedTuple

from rich.console import Console
from rich.theme import Theme


class _Palette(NamedTuple):
    """Five anchor colours each theme has to provide.

    `gradient` is a list of 5 hex colours used by the figlet INSTO logo —
    one row of the banner gets one stop, top-down. The same palette can
    be referenced by future renderers that want a multi-stop accent.
    """

    accent: str
    gradient: tuple[str, str, str, str, str]
    muted: str
    field: str
    value: str


def _make_theme(p: _Palette) -> Theme:
    """Map a palette onto the full set of style keys the UI consumes."""
    return Theme(
        {
            "accent": p.accent,
            "accent.bold": f"bold {p.accent}",
            "muted": p.muted,
            "ok": "green",
            "warn": "yellow",
            "err": "red",
            "field": p.field,
            "value": p.value,
            "panel.border": p.accent,
            "table.header": f"bold {p.accent}",
            "tip.cmd": f"bold {p.accent}",
            "tip.desc": p.value,
            "section": f"bold {p.accent}",
            # Per-row gradient classes. The banner addresses these by name
            # (`logo.0` … `logo.4`) so non-instagram themes can collapse them
            # to a single accent without the banner code knowing.
            "logo.0": p.gradient[0],
            "logo.1": p.gradient[1],
            "logo.2": p.gradient[2],
            "logo.3": p.gradient[3],
            "logo.4": p.gradient[4],
        }
    )


_PALETTES: dict[str, _Palette] = {
    "claude": _Palette(
        # Claude Code burnt-orange welcome screen (default).
        accent="#d97757",
        gradient=("#d97757", "#d97757", "#d97757", "#d97757", "#d97757"),
        muted="grey54",
        field="bold cyan",
        value="white",
    ),
    "instagram": _Palette(
        # Instagram 2022+ brand redesign — the conic gradient on the app icon.
        accent="#FF1B6B",
        gradient=("#FFD600", "#FF7A00", "#FF1B6B", "#E63CF7", "#7638FA"),
        muted="grey50",
        field="bold #FF7A00",
        value="white",
    ),
    "aiograpi": _Palette(
        # Same palette as the instagrapi / aiograpi project banner: purple-
        # magenta → blue diagonal with a single-pop yellow accent. The accent
        # uses the brand yellow so it stays readable over both purple and blue.
        accent="#FFC72A",
        gradient=("#7A2A8C", "#5E2C8E", "#3F4090", "#3D5DC4", "#3D72E0"),
        muted="grey54",
        field="bold #FFC72A",
        value="white",
    ),
}


THEMES: dict[str, Theme] = {name: _make_theme(p) for name, p in _PALETTES.items()}

DEFAULT_THEME_NAME: str = "claude"
INSTO_THEME: Theme = THEMES[DEFAULT_THEME_NAME]


def get_theme(name: str | None) -> Theme:
    """Return the named theme. Unknown / None falls back to default."""
    if name is None:
        return THEMES[DEFAULT_THEME_NAME]
    return THEMES.get(name, THEMES[DEFAULT_THEME_NAME])


def get_palette(name: str | None) -> _Palette:
    """Same lookup, but for code that needs to read raw hex (e.g. the
    prompt_toolkit completion-menu style, which is not a `rich.Theme`)."""
    if name is None:
        return _PALETTES[DEFAULT_THEME_NAME]
    return _PALETTES.get(name, _PALETTES[DEFAULT_THEME_NAME])


def list_themes() -> list[str]:
    """Names of every available theme, default first then alphabetical."""
    rest = sorted(n for n in THEMES if n != DEFAULT_THEME_NAME)
    return [DEFAULT_THEME_NAME, *rest]


def is_known(name: str | None) -> bool:
    return name in THEMES


def make_console(
    width: int = 100,
    *,
    force_terminal: bool = True,
    theme_name: str | None = None,
) -> Console:
    """Construct a `Console` with the named theme attached.

    Used by tests to render at a controlled width; production code
    constructs its own `Console(theme=get_theme(name))` from the live
    config.
    """
    return Console(
        theme=get_theme(theme_name),
        width=width,
        force_terminal=force_terminal,
        color_system="truecolor" if force_terminal else None,
        record=True,
    )
