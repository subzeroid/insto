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

# INSTO logotype (figlet "standard" font) baked at design time. The two-line
# tagline below leans into the anagram: the same five letters of "OSINT" — open
# source intelligence — rearranged into "insto", an Instagram-flavoured handle.
LOGO_BANNER: str = r"""
 ___ _   _ ____ _____ ___
|_ _| \ | / ___|_   _/ _ \
 | ||  \| \___ \ | || | | |
 | || |\  |___) || || |_| |
|___|_| \_|____/ |_| \___/
""".strip("\n")

LOGO_TAGLINE: str = "i n s t o   ⇋   o s i n t"
LOGO_SUBTAGLINE: str = "instagram tool · open-source intel"

# Backwards-compat alias for code/tests that still import the old name.
WASP_BANNER: str = LOGO_BANNER


_TIPS: tuple[tuple[str, str], ...] = (
    ("/target <user>", "set OSINT target"),
    ("/info", "full profile dump"),
    ("/help", "list all commands"),
)


def _banner_text() -> RenderableType:
    """Render the INSTO logotype + anagram tagline.

    Five figlet rows are coloured per-row from the active theme's gradient
    (`logo.0` … `logo.4`). On a flat-colour theme like `claude` every row
    gets the same accent so visually nothing changes; on `instagram` /
    `aiograpi` the rows step through the brand gradient so the logotype
    reads like the source mark.
    """
    rows = LOGO_BANNER.splitlines()
    while len(rows) < 5:  # defensive: never short-row past the gradient
        rows.append("")
    parts: list[RenderableType] = []
    for i, row in enumerate(rows[:5]):
        parts.append(Text(row, style=f"bold logo.{i}", no_wrap=True))
    for row in rows[5:]:
        parts.append(Text(row, style="bold logo.4", no_wrap=True))
    parts.append(Text(""))
    parts.append(Text(LOGO_TAGLINE, style="value"))
    parts.append(Text(LOGO_SUBTAGLINE, style="muted"))
    return Group(*parts)


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
        body = "hiker · balance: pending"
    else:
        parts = [_format_requests(quota.remaining) + " requests left"]
        if quota.amount is not None and quota.currency:
            parts.append(_format_money(quota.amount, quota.currency))
        if quota.rate is not None:
            parts.append(f"{quota.rate} rps cap")
        body = "hiker · " + " · ".join(parts)
    return Text(body, style="muted")


def _format_requests(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _format_money(amount: float, currency: str) -> str:
    sym = {"USD": "$", "EUR": "€", "GBP": "£"}.get(currency.upper(), currency + " ")
    return f"{sym}{amount:,.0f}" if amount >= 100 else f"{sym}{amount:,.2f}"


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
