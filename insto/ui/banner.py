"""Static wasp ASCII banner + welcome panel for the REPL startup screen.

The banner is hand-baked into source (no runtime image conversion). It is
laid out at roughly 30 columns by 16 rows, all printable ASCII so it renders
identically across terminals and CI.

`render_welcome(facade, *, email=None)` composes the full welcome panel:

- on a wide terminal (≥ 100 cols): two-column `Panel` with the banner on the
  left and a tips / recent activity / status block on the right;
- on a narrow terminal (60-99 cols): the banner only, inside the same panel;
- on a tiny terminal (< 60 cols): a single status line.

`Console` width is read off the caller's console — if no console is given
the standard width detection runs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.align import Align
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from insto._version import __version__

if TYPE_CHECKING:  # pragma: no cover
    from insto.service.facade import OsintFacade

WASP_BANNER: str = r"""
       __
      /  \
     ( ^  )
      \__/
     //||\\
    // || \\
   //  ||  \\
  //  /||\  \\
 //__/_||_\__\\
    ##||##
    ##||##
   ###||###
  ####||####
   ## || ##
      ||
      vv
""".strip("\n")


_TIPS: tuple[tuple[str, str], ...] = (
    ("/target <user>", "set OSINT target"),
    ("/info", "full profile dump"),
    ("/help", "list all commands"),
)


def _banner_text() -> Text:
    """Return the wasp banner as a styled `Text`, accent-coloured."""
    return Text(WASP_BANNER, style="accent", no_wrap=True)


def _tips_table() -> Table:
    """Render the 'Tips for getting started' block."""
    table = Table.grid(padding=(0, 2))
    table.add_column(justify="left", no_wrap=True)
    table.add_column(justify="left")
    for cmd, desc in _TIPS:
        table.add_row(Text(cmd, style="tip.cmd"), Text(desc, style="tip.desc"))
    return table


def _recent_block(recent: list[str]) -> RenderableType:
    """Render the 'Recent activity' block."""
    if not recent:
        return Text("No recent activity", style="muted")
    table = Table.grid(padding=(0, 1))
    table.add_column(no_wrap=True)
    for target in recent:
        table.add_row(Text(f"@{target.lstrip('@')}", style="value"))
    return table


def _quota_line(facade: OsintFacade) -> Text:
    """One-line backend / quota status."""
    quota = facade.quota()
    if quota.remaining is None:
        body = "hiker · quota: unknown"
    elif quota.limit is None:
        body = f"hiker · {quota.remaining} quota remaining"
    else:
        body = f"hiker · {quota.remaining}/{quota.limit} quota"
    return Text(body, style="muted")


def _right_column(facade: OsintFacade, email: str | None) -> RenderableType:
    """Build the tips / recent / status column."""
    recent = facade.history.recent_targets(3)
    blocks: list[RenderableType] = [
        Text("Tips for getting started", style="section"),
        _tips_table(),
        Text(""),
        Text("Recent activity", style="section"),
        _recent_block(recent),
        Text(""),
        _quota_line(facade),
    ]
    if email:
        blocks.append(Text(email, style="muted"))
    return Group(*blocks)


def _two_column(facade: OsintFacade, email: str | None) -> RenderableType:
    """Banner left, tips/status right; both inside one panel."""
    grid = Table.grid(padding=(0, 4), expand=True)
    grid.add_column(no_wrap=True)
    grid.add_column(ratio=1)
    grid.add_row(_banner_text(), _right_column(facade, email))
    return grid


def _narrow_panel_body(facade: OsintFacade, email: str | None) -> RenderableType:
    """Banner-only body for medium terminals."""
    blocks: list[RenderableType] = [
        Align.center(_banner_text()),
        Text(""),
        _quota_line(facade),
    ]
    if email:
        blocks.append(Text(email, style="muted"))
    return Group(*blocks)


def _tiny_line(facade: OsintFacade, target: str | None) -> Text:
    """Single-line fallback for very narrow terminals."""
    parts = [f"insto v{__version__}", "hiker"]
    if target:
        parts.append(f"target: {target}")
    quota = facade.quota()
    if quota.remaining is not None:
        parts.append(f"quota: {quota.remaining}")
    return Text(" · ".join(parts), style="accent")


def render_welcome(
    facade: OsintFacade,
    *,
    width: int,
    email: str | None = None,
    target: str | None = None,
) -> RenderableType:
    """Render the welcome screen sized to `width`.

    Width tiers:
      - `width < 60`: a single status line (no panel).
      - `60 ≤ width < 100`: banner-only `Panel`.
      - `width ≥ 100`: two-column `Panel` (banner + tips/recent/status).
    """
    title = f"insto v{__version__}"
    if width < 60:
        return _tiny_line(facade, target)
    body = _narrow_panel_body(facade, email) if width < 100 else _two_column(facade, email)
    return Panel(
        body,
        title=title,
        title_align="left",
        border_style="panel.border",
        padding=(1, 2),
    )
