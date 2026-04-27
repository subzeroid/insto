"""DTO renderers — every command lives downstream of these.

Each function takes plain DTOs (or, for `render_profile`, a profile + an
optional `user_about` dict) and returns a `rich` renderable. Commands
choose where to print the result; they never construct rich objects of
their own.

Style names referenced here (`field`, `value`, `accent`, `muted`, …) live
in `insto.ui.theme.INSTO_THEME`. A `Console` without that theme will
silently fall back to plain text — which is what tests want when they
export with `Console.export_text(styles=False)`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from rich.console import RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from insto.models import Highlight, HighlightItem, Post, Profile, User


def _yesno(flag: bool) -> str:
    return "yes" if flag else "no"


def _fmt_int(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def _fmt_ts(ts: int | float | None) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(int(ts), tz=UTC).strftime("%Y-%m-%d %H:%M")


def render_profile(
    profile: Profile,
    about: dict[str, Any] | None = None,
) -> RenderableType:
    """Render a single profile + optional `user_about` payload as a `Panel`.

    The body is a two-column key/value grid; long fields (biography,
    external_url) wrap. The panel title carries the username + access tag.
    """
    body = Table.grid(padding=(0, 2))
    body.add_column(style="field", no_wrap=True)
    body.add_column(style="value", overflow="fold")

    def row(label: str, value: object) -> None:
        body.add_row(label, "" if value in (None, "") else str(value))

    row("pk", profile.pk)
    row("full name", profile.full_name)
    row("access", profile.access)
    row("verified", _yesno(profile.is_verified))
    row("private", _yesno(profile.is_private))
    row("business", _yesno(profile.is_business))
    row("followers", _fmt_int(profile.follower_count))
    row("following", _fmt_int(profile.following_count))
    row("media", _fmt_int(profile.media_count))
    row("biography", profile.biography or "—")
    row("external url", profile.external_url or "—")
    row("public email", profile.public_email or "—")
    row("public phone", profile.public_phone or "—")
    row("category", profile.business_category or "—")
    if profile.previous_usernames:
        row("aliases", ", ".join(profile.previous_usernames))
    if about:
        for key in ("country", "country_code", "is_eligible_to_show_email"):
            if key in about and about[key] not in (None, ""):
                row(key.replace("_", " "), about[key])

    title = Text.assemble(
        ("@", "muted"),
        (profile.username, "accent.bold"),
        ("  ", ""),
        (f"[{profile.access}]", "muted"),
    )
    return Panel(body, title=title, border_style="panel.border", padding=(1, 2))


def render_user_table(users: Sequence[User], *, title: str | None = None) -> Table:
    """Render a list of users as a sortable table (followers / following / likers).

    Columns: username · full name · private · verified.
    """
    table = Table(
        title=title,
        title_style="section",
        header_style="table.header",
        border_style="panel.border",
        expand=False,
    )
    table.add_column("#", justify="right", style="muted", no_wrap=True)
    table.add_column("username", style="accent")
    table.add_column("full name", style="value", overflow="fold")
    table.add_column("private", justify="center", style="muted", no_wrap=True)
    table.add_column("verified", justify="center", style="muted", no_wrap=True)
    for i, u in enumerate(users, 1):
        table.add_row(
            str(i),
            f"@{u.username}",
            u.full_name or "",
            _yesno(u.is_private),
            _yesno(u.is_verified),
        )
    return table


def render_media_grid(posts: Sequence[Post], *, title: str | None = None) -> Table:
    """Render a list of posts as a grid (`/posts`, `/tagged`, `/captions`).

    Columns: shortcode · type · taken_at · likes · comments · caption preview.
    """
    table = Table(
        title=title,
        title_style="section",
        header_style="table.header",
        border_style="panel.border",
        expand=False,
    )
    table.add_column("#", justify="right", style="muted", no_wrap=True)
    table.add_column("code", style="accent", no_wrap=True)
    table.add_column("type", style="muted", no_wrap=True)
    table.add_column("taken_at", style="muted", no_wrap=True)
    table.add_column("likes", justify="right", style="value", no_wrap=True)
    table.add_column("comments", justify="right", style="value", no_wrap=True)
    table.add_column("caption", style="value", overflow="ellipsis", max_width=40)
    for i, p in enumerate(posts, 1):
        caption = p.caption.replace("\n", " ").strip()
        table.add_row(
            str(i),
            p.code,
            p.media_type,
            _fmt_ts(p.taken_at),
            _fmt_int(p.like_count),
            _fmt_int(p.comment_count),
            caption,
        )
    return table


def render_highlights_tree(
    highlights: Sequence[Highlight],
    items_by_highlight: Mapping[str, Sequence[HighlightItem]] | None = None,
) -> Tree:
    """Render highlights as a `Tree`. `items_by_highlight` is optional —

    when supplied, each highlight node expands into a sub-tree of items
    (`pk · type · taken_at`). When omitted, a single line summary per
    highlight is shown.
    """
    root = Tree(Text("highlights", style="section"))
    for h in highlights:
        label = Text.assemble(
            (h.title or "(untitled)", "accent"),
            (f"  · {h.item_count} items", "muted"),
        )
        node = root.add(label)
        if items_by_highlight:
            for item in items_by_highlight.get(h.pk, ()):
                node.add(
                    Text.assemble(
                        (f"{item.pk}", "value"),
                        (f"  · {item.media_type} · {_fmt_ts(item.taken_at)}", "muted"),
                    )
                )
    return root


def render_kv(
    rows: Iterable[tuple[str, object]],
    *,
    title: str | None = None,
    key_label: str = "key",
    value_label: str = "count",
) -> Table:
    """Render an arbitrary key/value list (top hashtags, mentions, locations)."""
    table = Table(
        title=title,
        title_style="section",
        header_style="table.header",
        border_style="panel.border",
        expand=False,
    )
    table.add_column("#", justify="right", style="muted", no_wrap=True)
    table.add_column(key_label, style="accent", overflow="fold")
    table.add_column(value_label, justify="right", style="value", no_wrap=True)
    for i, (k, v) in enumerate(rows, 1):
        table.add_row(str(i), str(k), str(v))
    return table
