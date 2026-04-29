"""Tests for `insto.repl`: completer, toolbar, keybindings, asyncio interop."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.document import Document
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from insto import repl as repl_mod
from insto.commands import COMMANDS
from insto.config import Config
from insto.models import Quota
from insto.repl import (
    Repl,
    _completer,
    _format_quota,
    _format_unknown_command,
    _make_bottom_toolbar,
    did_you_mean,
)
from insto.service.facade import OsintFacade
from insto.service.history import HistoryStore
from insto.ui.theme import INSTO_THEME
from tests.fakes import FakeBackend

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def _facade(tmp_path: Path) -> Iterator[OsintFacade]:
    history = HistoryStore(tmp_path / "store.db")
    config = Config(output_dir=tmp_path / "output", db_path=tmp_path / "store.db")
    backend = FakeBackend(quota=Quota(remaining=80, limit=100))
    facade = OsintFacade(backend=backend, history=history, config=config)
    yield facade
    history.close()


@pytest.fixture
def _console() -> Console:
    return Console(
        theme=INSTO_THEME,
        width=100,
        force_terminal=True,
        color_system="truecolor",
        record=True,
    )


def _make_repl(facade: OsintFacade, console: Console, tmp_path: Path) -> Repl:
    config = Config(output_dir=tmp_path / "output", db_path=tmp_path / "store.db")
    return Repl(
        facade=facade,
        config=config,
        console=console,
        history_path=tmp_path / "cli_history",
    )


# ---------------------------------------------------------------------------
# Completer
# ---------------------------------------------------------------------------


def _completions(completer: object, text: str) -> list[str]:
    """Drive a `Completer` with `text` and return its completion strings."""
    doc = Document(text=text, cursor_position=len(text))
    completions = completer.get_completions(doc, complete_event=None)  # type: ignore[attr-defined]
    return [c.text for c in completions]


def test_completer_lists_every_command_when_only_slash_typed() -> None:
    """Slack/Claude-Code-style: typing '/' opens a popup with every command."""
    completer = _completer()
    matches = _completions(completer, "/")
    assert all(f"/{name}" in matches for name in COMMANDS)
    # And no bare names — display always includes the slash.
    assert all(m.startswith("/") for m in matches)


def test_completer_narrows_on_slash_prefix() -> None:
    completer = _completer()
    matches = _completions(completer, "/info")
    assert "/info" in matches
    assert "/posts" not in matches


def test_completer_works_without_leading_slash() -> None:
    """Bare prefix (REPL strips leading '/' anyway) still narrows correctly."""
    completer = _completer()
    matches = _completions(completer, "inf")
    # Returned text keeps its lack of slash so the user's typing isn't replaced
    # with one they did not type.
    assert "info" in matches
    assert "posts" not in matches


def test_completer_yields_argument_choices_when_command_typed_exactly() -> None:
    """Typing `/theme` (no trailing space) and Tab should already show
    `claude` / `instagram` / `aiograpi` — without forcing the user to
    type a space first. Same for `/purge`, etc.

    The choices are yielded with a leading space in their `text` so
    that accepting `instagram` produces `/theme instagram`, not
    `/themeinstagram` (the cursor sits at end of `/theme` with no
    trailing whitespace; without the space-prefix, prompt-toolkit
    would insert the choice flush against the command name).
    """
    completer = _completer()
    matches = _completions(completer, "/theme")
    # Command itself is still in the popup (so Tab can finish-and-submit).
    assert "/theme" in matches
    # Theme choices appear inline, each with a leading space separator.
    assert " claude" in matches
    assert " instagram" in matches
    assert " aiograpi" in matches
    # And the bare names (no leading space) must NOT be in the list —
    # that would be the regression we're fixing.
    assert "claude" not in matches
    assert "instagram" not in matches


def test_completer_meta_uses_command_help() -> None:
    completer = _completer()
    spec = next(iter(COMMANDS.values()))
    doc = Document(text="/", cursor_position=1)
    by_text = {c.text: c for c in completer.get_completions(doc, complete_event=None)}
    assert f"/{spec.name}" in by_text
    meta = by_text[f"/{spec.name}"].display_meta_text
    assert spec.help == meta


# ---------------------------------------------------------------------------
# did_you_mean
# ---------------------------------------------------------------------------


def test_did_you_mean_for_close_match() -> None:
    # `inf` is one keystroke from a real command name (varies by registry; pick one)
    real = next((n for n in COMMANDS if n.startswith("info")), None)
    if real is None:
        pytest.skip("no /info command registered")
    typo = real[:-1]  # drop last char
    suggestion = did_you_mean(typo)
    assert real in suggestion
    assert suggestion.startswith(" — did you mean /")


def test_did_you_mean_for_no_match_is_empty() -> None:
    assert did_you_mean("zzzzzzzzzzz_not_a_command") == ""


def test_format_unknown_command_includes_did_you_mean() -> None:
    real = next(iter(COMMANDS))
    typo = real[:-1]
    rendered = _format_unknown_command(typo)
    assert rendered.startswith(f"unknown command: /{typo}")
    if did_you_mean(typo):
        assert "did you mean" in rendered


# ---------------------------------------------------------------------------
# Bottom toolbar
# ---------------------------------------------------------------------------


def test_bottom_toolbar_shows_target_backend_and_quota(_facade: OsintFacade) -> None:
    from insto.commands import Session

    session = Session(target="alice")
    toolbar = _make_bottom_toolbar(_facade, session)
    text = "".join(s for _, s in toolbar())
    assert "target: @alice" in text
    assert "backend: fake" in text
    # Toolbar now renders pay-per-call quota: "80 req" instead of "80/100".
    assert "80 req" in text


def test_bottom_toolbar_target_none(_facade: OsintFacade) -> None:
    from insto.commands import Session

    toolbar = _make_bottom_toolbar(_facade, Session())
    text = "".join(s for _, s in toolbar())
    assert "target: (none)" in text


def test_bottom_toolbar_quota_unknown(_facade: OsintFacade) -> None:
    from insto.commands import Session

    _facade.backend.quota = Quota.unknown()  # type: ignore[attr-defined]
    toolbar = _make_bottom_toolbar(_facade, Session())
    text = "".join(s for _, s in toolbar())
    assert "quota: ?" in text


def test_format_quota_handles_remaining_only(_facade: OsintFacade) -> None:
    _facade.backend.quota = Quota(remaining=42)  # type: ignore[attr-defined]
    assert _format_quota(_facade) == "42 req"


def test_format_quota_handles_get_quota_raising(_facade: OsintFacade) -> None:
    def boom() -> Quota:
        raise RuntimeError("oh no")

    _facade.backend.get_quota = boom  # type: ignore[method-assign,assignment]
    assert _format_quota(_facade) == "quota: ?"


# ---------------------------------------------------------------------------
# Keybindings — Ctrl+T / Ctrl+L
# ---------------------------------------------------------------------------


def test_keybindings_ctrl_t_calls_quick_show_target(
    _facade: OsintFacade, _console: Console, tmp_path: Path
) -> None:
    repl = _make_repl(_facade, _console, tmp_path)
    repl.session.set_target("ferrari")

    calls: list[bool] = []
    repl.quick_show_target = lambda: calls.append(True)  # type: ignore[method-assign]

    binding = next(
        b
        for b in repl.key_bindings.bindings
        if any(getattr(k, "value", k) == "c-t" or k == "c-t" for k in b.keys)
    )
    binding.handler(None)  # type: ignore[arg-type]
    assert calls == [True]


def test_keybindings_ctrl_l_clears_and_redraws(
    _facade: OsintFacade, _console: Console, tmp_path: Path
) -> None:
    repl = _make_repl(_facade, _console, tmp_path)
    cleared: list[bool] = []
    redrawn: list[bool] = []
    repl.console.clear = lambda: cleared.append(True)  # type: ignore[method-assign]
    repl.redraw_banner = lambda: redrawn.append(True)  # type: ignore[method-assign]

    binding = next(
        b
        for b in repl.key_bindings.bindings
        if any(getattr(k, "value", k) == "c-l" or k == "c-l" for k in b.keys)
    )
    binding.handler(None)  # type: ignore[arg-type]
    assert cleared == [True]
    assert redrawn == [True]


def test_quick_show_target_outputs_active_target(
    _facade: OsintFacade, _console: Console, tmp_path: Path
) -> None:
    repl = _make_repl(_facade, _console, tmp_path)
    repl.session.set_target("ferrari")
    repl.quick_show_target()
    out = _console.export_text(styles=False)
    assert "@ferrari" in out


def test_quick_show_target_no_target(
    _facade: OsintFacade, _console: Console, tmp_path: Path
) -> None:
    repl = _make_repl(_facade, _console, tmp_path)
    repl.quick_show_target()
    out = _console.export_text(styles=False)
    assert "(none)" in out


# ---------------------------------------------------------------------------
# prompt_toolkit ↔ asyncio interop
# ---------------------------------------------------------------------------


def test_prompt_async_does_not_block_concurrent_tasks(
    _facade: OsintFacade, _console: Console, tmp_path: Path
) -> None:
    """Drive `prompt_async` and a concurrent ticker; assert ticker runs on time.

    The acceptance criterion in the plan: the ticker fires twice with no
    drift > 100ms while a `prompt_async` is active. We close the pipe to
    let `prompt_async` end with EOFError after the ticker has run.
    """
    tick_times: list[float] = []

    async def runner() -> None:
        with (
            create_pipe_input() as pipe,
            create_app_session(input=pipe, output=DummyOutput()),
        ):
            repl = _make_repl(_facade, _console, tmp_path)

            async def tick() -> None:
                start = time.monotonic()
                for _ in range(3):
                    await asyncio.sleep(0.05)
                    tick_times.append(time.monotonic() - start)
                pipe.close()

            ticker = asyncio.create_task(tick())
            with pytest.raises((EOFError, asyncio.CancelledError)):
                await repl.prompt_session.prompt_async("> ")
            await ticker

    asyncio.run(runner())

    assert len(tick_times) == 3
    # The third tick should land near 0.15s; allow 100ms drift.
    assert tick_times[-1] < 0.25, f"ticker drifted: {tick_times}"


def test_repl_run_handles_eof_cleanly(
    _facade: OsintFacade, _console: Console, tmp_path: Path
) -> None:
    """A pipe closed with no input → `prompt_async` raises EOF → run() returns."""

    async def runner() -> None:
        with (
            create_pipe_input() as pipe,
            create_app_session(input=pipe, output=DummyOutput()),
        ):
            repl = _make_repl(_facade, _console, tmp_path)
            pipe.close()
            await repl.run()

    asyncio.run(runner())
    out = _console.export_text(styles=False)
    assert "bye" in out


def test_repl_executes_quit_command(
    _facade: OsintFacade, _console: Console, tmp_path: Path
) -> None:
    """Sending `/quit\\n` ends the loop without dispatching a real command."""

    async def runner() -> None:
        with (
            create_pipe_input() as pipe,
            create_app_session(input=pipe, output=DummyOutput()),
        ):
            repl = _make_repl(_facade, _console, tmp_path)
            pipe.send_text("/quit\n")
            await repl.run()

    asyncio.run(runner())


def test_repl_dispatches_known_command(
    _facade: OsintFacade, _console: Console, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A known command line is forwarded into `dispatch`."""
    captured: list[str] = []

    async def fake_dispatch(line: str, **kwargs: object) -> None:
        captured.append(line)

    monkeypatch.setattr(repl_mod, "dispatch", fake_dispatch)

    async def runner() -> None:
        with (
            create_pipe_input() as pipe,
            create_app_session(input=pipe, output=DummyOutput()),
        ):
            repl = _make_repl(_facade, _console, tmp_path)
            pipe.send_text("/help\n/quit\n")
            await repl.run()

    asyncio.run(runner())
    assert captured == ["/help"]


def test_repl_unknown_command_renders_error(
    _facade: OsintFacade, _console: Console, tmp_path: Path
) -> None:
    """Unknown command surfaces through `_format_error` (not a crash)."""

    async def runner() -> None:
        with (
            create_pipe_input() as pipe,
            create_app_session(input=pipe, output=DummyOutput()),
        ):
            repl = _make_repl(_facade, _console, tmp_path)
            pipe.send_text("/notacommand\n/quit\n")
            await repl.run()

    asyncio.run(runner())
    out = _console.export_text(styles=False)
    assert "unknown command" in out
