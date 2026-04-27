"""`rich.Theme` tuned to match Claude Code's welcome-screen palette.

The accent colour is the Claude Code orange (`#d97757`); every panel border
and section header in the UI references the `accent` style so we never
hard-code colour strings outside this module. `rich` styles are inheritable
key/value pairs, so a renderer can write `style="bold accent"` and pick up
the orange automatically.

`make_console` is a thin convenience wrapper used by tests to build a
deterministic `Console` (fixed width, theme attached). Production code
constructs its own `Console` in `cli.py` / `repl.py`.
"""

from __future__ import annotations

from rich.console import Console
from rich.theme import Theme

ACCENT = "#d97757"
MUTED = "grey54"

INSTO_THEME = Theme(
    {
        "accent": ACCENT,
        "accent.bold": f"bold {ACCENT}",
        "muted": MUTED,
        "ok": "green",
        "warn": "yellow",
        "err": "red",
        "field": "bold cyan",
        "value": "white",
        "panel.border": ACCENT,
        "table.header": f"bold {ACCENT}",
        "tip.cmd": f"bold {ACCENT}",
        "tip.desc": "white",
        "section": f"bold {ACCENT}",
    }
)


def make_console(width: int = 100, *, force_terminal: bool = True) -> Console:
    """Construct a `Console` with the insto theme applied.

    Used by tests to render at a controlled width; production code creates
    its own `Console(theme=INSTO_THEME)`.
    """
    return Console(
        theme=INSTO_THEME,
        width=width,
        force_terminal=force_terminal,
        color_system="truecolor" if force_terminal else None,
        record=True,
    )
