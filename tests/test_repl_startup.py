"""Tests for REPL startup target selection (`insto @user`).

`_safe_set_startup_target` is the best-effort bridge that turns the CLI
positional target into an active session target before the prompt loop opens.
It must: set the target on success, never raise, and never touch the network
(so a slow/cold backend cannot stall startup — the first command resolves).
"""

from __future__ import annotations

from collections.abc import Generator, Iterator
from pathlib import Path

import pytest
from rich.console import Console

from insto import repl as repl_mod
from insto.commands._base import Session
from insto.config import Config
from insto.models import Profile
from insto.repl import Repl
from insto.service.facade import OsintFacade
from insto.service.history import HistoryStore
from insto.ui.theme import INSTO_THEME, list_themes
from tests.fakes import FakeBackend


@pytest.fixture
def history(tmp_path: Path) -> Generator[HistoryStore, None, None]:
    s = HistoryStore(tmp_path / "store.db")
    yield s
    s.close()


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(output_dir=tmp_path / "output", db_path=tmp_path / "store.db")


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend(profiles={"42": Profile(pk="42", username="alice", access="public")})


@pytest.fixture
def repl(
    backend: FakeBackend, history: HistoryStore, config: Config, tmp_path: Path
) -> Iterator[Repl]:
    facade = OsintFacade(backend=backend, history=history, config=config)
    console = Console(theme=INSTO_THEME, width=120, force_terminal=True, record=True)
    yield Repl(
        facade=facade,
        config=config,
        console=console,
        history_path=tmp_path / "cli_history",
    )


def test_startup_target_sets_session_and_prompt(repl: Repl) -> None:
    repl_mod._safe_set_startup_target(repl, "@alice")
    assert repl.session.target == "alice"
    assert repl._prompt_prefix() == "insto @alice> "


def test_startup_target_does_no_network_resolve(repl: Repl) -> None:
    # Startup must be instant: it sets the target locally and never calls the
    # backend. A name that doesn't exist in the fake backend is still accepted —
    # the first command resolves it (and would surface a typo there).
    repl_mod._safe_set_startup_target(repl, "ghost")
    assert repl.session.target == "ghost"
    assert not any(name == "resolve_target" for name, _ in repl.facade.backend.request_log)


def test_startup_target_invalid_format_opens_repl_with_warning(repl: Repl) -> None:
    repl_mod._safe_set_startup_target(repl, "a/b")  # filesystem-unsafe → rejected
    assert repl.session.target is None
    out = repl.console.export_text(styles=False)
    assert "startup target not set" in out


def test_run_repl_pre_selects_startup_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """run_repl(target=...) drives _main so the target is selected before the
    prompt loop. Repl.run is stubbed so no TTY is needed."""
    backend = FakeBackend(profiles={"42": Profile(pk="42", username="alice", access="public")})
    hist = HistoryStore(tmp_path / "s.db")
    config = Config(output_dir=tmp_path / "o", db_path=tmp_path / "s.db")
    facade = OsintFacade(backend=backend, history=hist, config=config)

    async def _cleanup() -> None:
        return None

    monkeypatch.setattr(repl_mod, "_bootstrap", lambda cfg=None: (facade, _cleanup))

    captured: dict[str, str | None] = {}

    class StubRepl:
        def __init__(
            self, *, facade: OsintFacade, config: Config, email: str | None = None
        ) -> None:
            self.facade = facade
            self.session = Session()
            self.console = Console(record=True)

        async def run(self) -> None:
            captured["target"] = self.session.target

    monkeypatch.setattr(repl_mod, "Repl", StubRepl)

    try:
        repl_mod.run_repl(config=config, target="@alice")
    finally:
        hist.close()

    assert captured["target"] == "alice"


def test_theme_switch_applies_live(repl: Repl) -> None:
    # Simulate `/theme <other>`: the command layer sets config.theme; the REPL
    # must apply it to the live session (no restart) on the next sync.
    start = repl.config.theme
    other = next(t for t in list_themes() if t != start)
    before_style = repl.prompt_session.style

    repl.config.theme = other
    repl._sync_theme()

    assert repl._applied_theme == other
    assert repl.prompt_session.style is not before_style  # popup style rebuilt
    assert "insto v" in repl.console.export_text()  # banner repainted


def test_theme_sync_is_noop_when_unchanged(repl: Repl) -> None:
    repl._sync_theme()  # theme not changed since construction
    assert repl.console.export_text() == ""  # nothing repainted
