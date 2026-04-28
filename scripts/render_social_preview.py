"""Render `docs/social-preview.png` — the GitHub social preview card.

Pipeline: render the welcome panel (no version, no recent activity)
through `rich.Console.save_svg`, then rasterise with CairoSVG to a 1280x640
PNG (GitHub's expected social-preview ratio).

Usage:

    uv run python scripts/render_social_preview.py

Re-run whenever the welcome layout changes. The output is committed at
`docs/social-preview.png`; GitHub picks it up via the repo's "Social
preview" setting (Settings → General → Social preview → Upload).

Why not just screenshot the live REPL?

- VHS GIF frames are GIF-quantised and look mushy at 1280x640.
- The live banner pulls real recent-activity / live HikerAPI balance,
  which are user-specific and outdated by the next session.

The script forces a temp `~/.insto` so the rendering machine's saved
recent-activity / theme is irrelevant — the preview is reproducible.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import os  # noqa: E402

# Make the rendering deterministic: a brand-new ~/.insto, the brand
# theme, no token leaking into env. The HistoryStore + facade still want
# a real on-disk path so we hand them a tempdir.
SANDBOX = Path(tempfile.mkdtemp(prefix="insto-social-"))
os.environ["INSTO_HOME"] = str(SANDBOX)

from rich.console import Console  # noqa: E402

from insto.config import Config  # noqa: E402
from insto.models import Quota  # noqa: E402
from insto.service.facade import OsintFacade  # noqa: E402
from insto.service.history import HistoryStore  # noqa: E402
from insto.ui.banner import render_welcome  # noqa: E402
from insto.ui.theme import get_theme  # noqa: E402
from tests.fakes import FakeBackend  # noqa: E402


class HikerBackend(FakeBackend):
    """Subclass purely so `type(...).__name__` strips to 'hiker' in the
    welcome banner — the social preview should not advertise the test
    fake. We never call the network from here; the parent's local-data
    behaviour is fine."""

OUT_PNG = REPO / "docs" / "social-preview.png"
WIDTH_COLS = 110  # Rich columns; controls aspect ratio of the SVG.
THEME = "aiograpi"
TITLE = "insto"
SUBTITLE = "Interactive Instagram OSINT CLI"


def _build_facade() -> OsintFacade:
    config = Config(
        theme=THEME,
        output_dir=SANDBOX / "out",
        db_path=SANDBOX / "store.db",
    )
    history = HistoryStore(config.db_path)
    backend = HikerBackend()
    # Inject a believable balance so the right-column footer reads as
    # the user will actually see it after `insto setup`.
    backend.quota = Quota.with_remaining(  # type: ignore[attr-defined]
        15_100_000, rate=15, amount=4540.0, currency="USD"
    )
    return OsintFacade(backend=backend, history=history, config=config)


def _seed_recent(facade: OsintFacade) -> None:
    """Drop in three plausible-looking recent targets so the right column
    is not just 'No recent activity'. Pure cosmetics — nothing real."""
    for handle in ("instagram", "nasa", "nike"):
        facade.history.record_command("info", handle)


def main() -> int:
    facade = _build_facade()
    _seed_recent(facade)

    console = Console(
        theme=get_theme(THEME),
        width=WIDTH_COLS,
        force_terminal=True,
        color_system="truecolor",
        record=True,
        file=open(os.devnull, "w"),  # noqa: SIM115 — closed at process exit
    )
    # Pad with blank lines top + bottom so the panel sits centered in the
    # 1280x640 (1.91:1) GitHub social-preview frame instead of letterboxing
    # against the dark background.
    console.print()
    console.print()
    console.print(render_welcome(facade, width=WIDTH_COLS, show_version=False))
    console.print()
    console.print()

    # Strip Rich's default 'macOS window' chrome and force a font stack
    # that ships the U+21CB harpoon glyph used in `i n s t o ⇋ o s i n t`.
    # Fira Code (the default) doesn't carry it, so the live SVG renders
    # the anagram arrow as a tofu square.
    code_format = """<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg">
    <style>
    .{unique_id}-matrix {{
        font-family: "JetBrains Mono", "Hack", "SF Mono", "Menlo",
                     "Fira Code", monospace;
        font-size: {char_height}px;
        line-height: {line_height}px;
        font-variant-east-asian: full-width;
    }}
    {styles}
    </style>
    <defs>
    <clipPath id="{unique_id}-clip-terminal">
      <rect x="0" y="0" width="{terminal_width}" height="{terminal_height}" />
    </clipPath>
    {lines}
    </defs>
    <rect width="{width}" height="{height}" rx="6" fill="#1a1b26"/>
    <g transform="translate({terminal_x}, {terminal_y})"
       clip-path="url(#{unique_id}-clip-terminal)">
    {backgrounds}
    <g class="{unique_id}-matrix">
    {matrix}
    </g>
    </g>
</svg>
"""

    svg_path = SANDBOX / "preview.svg"
    console.save_svg(
        str(svg_path),
        title=f"{TITLE} — {SUBTITLE}",
        clear=True,
        code_format=code_format,
    )

    try:
        import cairosvg  # type: ignore[import-not-found]
    except ModuleNotFoundError:
        sys.stderr.write(
            "cairosvg is required: `uv pip install --group dev cairosvg` or "
            "add it to pyproject [docs] extras.\n"
        )
        return 1

    OUT_PNG.parent.mkdir(parents=True, exist_ok=True)
    cairosvg.svg2png(
        url=str(svg_path),
        write_to=str(OUT_PNG),
        output_width=1280,
        output_height=640,
    )
    facade.history.close()
    print(f"wrote {OUT_PNG}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
