"""Named colour themes for insto.

Six themes are baked at build time and selected at runtime via `/theme
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

`aiograpi` (default) is the instagrapi/aiograpi violet→blue gradient.
`instagram` uses the 2022+ Instagram brand gradient (yellow → orange →
pink → magenta → violet). `claude` mirrors Claude Code's burnt-orange
palette. `hacker` is matrix phosphor green-on-black, `amber` is an 80s
amber CRT, and `cyberpunk` is a cyan→magenta→green neon mix.
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
            # to a single accent without the banner code knowing. `bold` is
            # baked into the registered style here because Rich does not
            # parse compound style strings that mix a literal modifier with
            # a dotted theme key (`"bold logo.0"` raises MissingStyle).
            "logo.0": f"bold {p.gradient[0]}",
            "logo.1": f"bold {p.gradient[1]}",
            "logo.2": f"bold {p.gradient[2]}",
            "logo.3": f"bold {p.gradient[3]}",
            "logo.4": f"bold {p.gradient[4]}",
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
        # magenta → blue diagonal. Accent kept inside the same colour family
        # (saturated violet) so panel borders / section headers / tip.cmd
        # all read as part of the gradient instead of clashing with a
        # contrasting yellow.
        accent="#A45EE5",
        gradient=("#7A2A8C", "#5E2C8E", "#3F4090", "#3D5DC4", "#3D72E0"),
        muted="grey54",
        field="bold #A45EE5",
        value="white",
    ),
    "hacker": _Palette(
        # Matrix phosphor green-on-black — the classic "hacker" terminal look.
        accent="#00FF41",
        gradient=("#003B00", "#008F11", "#00C82C", "#00FF41", "#5CFF8F"),
        muted="#2E7D32",
        field="bold #00FF41",
        value="#C8FFD4",
    ),
    "amber": _Palette(
        # Amber CRT phosphor — 80s monochrome terminal warmth.
        accent="#FFB000",
        gradient=("#5A2E00", "#A85D00", "#FF9E00", "#FFB000", "#FFD27F"),
        muted="#8A5A00",
        field="bold #FFB000",
        value="#FFE8C2",
    ),
    "cyberpunk": _Palette(
        # Neon cyberpunk: cyan → blue → violet → magenta → green.
        accent="#00FFD1",
        gradient=("#00FFD1", "#2BD9FF", "#6A5CFF", "#FF2EC4", "#39FF14"),
        muted="#4A6E7E",
        field="bold #FF2EC4",
        value="#E6FBFF",
    ),
}


THEMES: dict[str, Theme] = {name: _make_theme(p) for name, p in _PALETTES.items()}

DEFAULT_THEME_NAME: str = "aiograpi"
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
