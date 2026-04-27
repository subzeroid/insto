"""Rendering layer: theme, banner / welcome panel, and DTO renderers.

`ui` is consumed by the command layer; it never imports from `commands` or
`cli`. Every function takes plain DTOs (or a `OsintFacade` for the welcome
panel) and returns a `rich.console.RenderableType` so callers stay in
control of where output is printed.
"""

from insto.ui.banner import WASP_BANNER, render_welcome
from insto.ui.render import (
    render_highlights_tree,
    render_kv,
    render_media_grid,
    render_profile,
    render_user_table,
)
from insto.ui.theme import INSTO_THEME, make_console

__all__ = [
    "INSTO_THEME",
    "WASP_BANNER",
    "make_console",
    "render_highlights_tree",
    "render_kv",
    "render_media_grid",
    "render_profile",
    "render_user_table",
    "render_welcome",
]
