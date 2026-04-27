"""E2E #2: REPL session `/target → /info → /posts → /quit`.

We drive the same `Repl` class the production entry point uses, but feed
input through `prompt_toolkit.input.create_pipe_input` so the session runs
without a real TTY. The fake backend is selected via `INSTO_BACKEND=fake`
in the env (applied by the `in_process_env` fixture).

Asserts:
- the welcome banner prints (`insto v…` panel title);
- each of the three commands renders something recognisable;
- `/quit` ends the loop cleanly (the REPL prints `bye` on exit).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from insto import repl as repl_mod
from insto.config import load_config
from insto.repl import Repl
from insto.ui.theme import INSTO_THEME


def _make_console() -> Console:
    return Console(
        theme=INSTO_THEME,
        width=120,
        force_terminal=True,
        color_system="truecolor",
        record=True,
    )


@pytest.mark.e2e
def test_repl_target_info_posts_quit_session(
    in_process_env: dict[str, str], tmp_path: Path
) -> None:
    """Feed `/target alice`, `/info`, `/posts`, `/quit` — all dispatch cleanly."""
    console = _make_console()
    history_path = tmp_path / "cli_history"

    async def runner() -> None:
        config = load_config()
        facade, cleanup = repl_mod._bootstrap(config)
        try:
            with (
                create_pipe_input() as pipe,
                create_app_session(input=pipe, output=DummyOutput()),
            ):
                repl = Repl(
                    facade=facade,
                    config=config,
                    console=console,
                    history_path=history_path,
                )
                pipe.send_text("/target alice\n/info\n/posts\n/quit\n")
                await repl.run()
        finally:
            await cleanup()

    asyncio.run(runner())

    out = console.export_text(styles=False)

    # Welcome banner panel title
    assert "insto v" in out
    # `/info` rendered the profile
    assert "Alice Example" in out
    assert "fake bio for e2e tests" in out
    # `/posts --no-download` printed at least one post (table or list of codes)
    assert "ABC123" in out or "ABC456" in out
    # Clean exit
    assert "bye" in out
