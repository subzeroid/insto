"""E2E #3: `/watch` actually ticks and surfaces notifications.

We monkey-patch `MIN_WATCH_INTERVAL_SECONDS` down to 0 so a watch can run
on a 0-second interval (a tight asyncio loop), launch the REPL with a
piped input, and confirm at least one tick reaches the recording console
through `patch_stdout`. The first tick always has no prior snapshot, so we
look for the `first snapshot` message — that proves the loop ran the diff
path end-to-end.
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
from insto.commands import watch as watch_mod
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
def test_watch_tick_emits_first_snapshot_notification(
    in_process_env: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`/watch alice 0` fires at least one tick whose message reaches the console."""
    monkeypatch.setattr(watch_mod, "MIN_WATCH_INTERVAL_SECONDS", 0)

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
                pipe.send_text("/target alice\n/watch alice 0\n")
                task = asyncio.create_task(repl.run())
                # Two yields' worth of ticks is plenty with interval=0.
                await asyncio.sleep(0.3)
                pipe.send_text("/quit\n")
                await task
        finally:
            await cleanup()

    asyncio.run(runner())

    out = console.export_text(styles=False)
    # Watch registration confirmation
    assert "watching @alice" in out
    # First-tick notification (first_seen branch of /diff)
    assert "first snapshot" in out
    # Clean exit
    assert "bye" in out
