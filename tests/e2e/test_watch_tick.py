"""E2E #3: `/watch` actually ticks and surfaces notifications.

We register through a piped REPL, wait for the shared coordinator to schedule
the persisted row, then invoke that real manager entry once without waiting
five minutes. The first tick has no prior snapshot, so `first snapshot` proves
the backend, diff, persistence, callback, and output path all ran end-to-end.
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
def test_watch_tick_emits_first_snapshot_notification(
    in_process_env: dict[str, str],
    tmp_path: Path,
) -> None:
    """A persisted `/watch alice 300` tick reaches the REPL console."""

    console = _make_console()
    history_path = tmp_path / "cli_history"

    async def runner() -> None:
        config = load_config()
        facade, cleanup = await repl_mod._bootstrap(
            config, watch_output=lambda message: console.print(message)
        )
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
                pipe.send_text("/target alice\n/watch alice 300\n")
                task = asyncio.create_task(repl.run())
                for _ in range(100):
                    if "alice" in facade.watches:
                        break
                    await asyncio.sleep(0.01)
                assert "alice" in facade.watches
                await facade.watches.tick_once("alice")
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
