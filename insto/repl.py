"""Interactive REPL: prompt_toolkit session with completer, toolbar, banner.

Layered on top of the same command registry the one-shot CLI uses:

- `WordCompleter` over `COMMANDS.keys()` (with leading `/`) so tab-cycle and
  inline hints surface every registered command and its one-line description.
- `BottomToolbar` is a callable read on every render — pulls the active
  target, backend name, and quota lazily via `facade.backend.get_quota()` so
  the toolbar reflects backend state in real time without a refresh loop.
- Welcome banner prints once at startup via `ui.banner.render_welcome`.
- Unknown commands fall back through `parse_command_line` (which already
  produces a `did_you_mean` hint via `difflib`) and are surfaced through the
  same `_format_error()` the CLI uses.
- All data output flows through `rich.Console`; prompt_toolkit only renders
  the input line and bottom toolbar.

Background tasks (notably `/watch`) print mid-prompt by way of
`patch_stdout`; the watch command already wraps its notifications with that
context manager so a tick will not garble the in-progress input line.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console

from insto._redact import redact_secrets
from insto.cli import _format_error
from insto.commands import COMMANDS, CommandUsageError, Session, dispatch
from insto.config import Config, cli_history_path, load_config
from insto.exceptions import BackendError
from insto.ui.banner import render_welcome
from insto.ui.theme import INSTO_THEME

if TYPE_CHECKING:
    from prompt_toolkit.key_binding import KeyPressEvent

    from insto.service.facade import OsintFacade

EXIT_COMMANDS = frozenset({"quit", "exit", "q"})


def _completer() -> WordCompleter:
    """Build a `WordCompleter` over every registered command name.

    Each command appears twice — bare (`info`) and slash-prefixed (`/info`) —
    so the completer triggers regardless of whether the user starts typing a
    leading `/`. Per-completion meta strings carry the one-line `help` text
    so the completion menu can render `command — description`.
    """
    words: list[str] = []
    meta: dict[str, str] = {}
    for name, spec in sorted(COMMANDS.items()):
        words.append(name)
        words.append(f"/{name}")
        meta[name] = spec.help
        meta[f"/{name}"] = spec.help
    return WordCompleter(
        words=words,
        meta_dict=meta,
        ignore_case=True,
        match_middle=False,
        sentence=False,
    )


def _format_quota(facade: OsintFacade) -> str:
    """Render the toolbar quota fragment lazily."""
    try:
        quota = facade.backend.get_quota()
    except Exception:
        return "quota: ?"
    if quota is None or quota.remaining is None:
        return "quota: ?"
    if quota.limit is None:
        return f"quota: {quota.remaining}"
    return f"quota: {quota.remaining}/{quota.limit}"


def _backend_label(facade: OsintFacade) -> str:
    name = type(facade.backend).__name__.lower()
    for suffix in ("backend",):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return name or "backend"


def _make_bottom_toolbar(facade: OsintFacade, session: Session) -> Callable[[], FormattedText]:
    """Return a callable prompt_toolkit invokes on every redraw."""
    backend = _backend_label(facade)

    def render() -> FormattedText:
        target = f"@{session.target}" if session.target else "(none)"
        body = f"target: {target} · backend: {backend} · {_format_quota(facade)}"
        return FormattedText([("class:bottom-toolbar", body)])

    return render


class Repl:
    """Stateful REPL session. Holds prompt_toolkit + facade + console wiring.

    Methods are split out (`quick_show_target`, `redraw_banner`, `prompt`,
    `run`) so unit tests can drive them directly without spinning up a full
    `Application`.
    """

    def __init__(
        self,
        *,
        facade: OsintFacade,
        config: Config,
        console: Console | None = None,
        history_path: Path | None = None,
        email: str | None = None,
    ) -> None:
        self.facade = facade
        self.config = config
        self.console = console or Console(theme=INSTO_THEME)
        self.session = Session()
        self.email = email
        self._log = logging.getLogger("insto.repl")
        self._history_path = history_path or cli_history_path()
        self._history_path.parent.mkdir(parents=True, exist_ok=True)
        self.bottom_toolbar = _make_bottom_toolbar(self.facade, self.session)
        self.key_bindings = self._build_key_bindings()
        self.prompt_session: PromptSession[str] = PromptSession(
            history=FileHistory(str(self._history_path)),
            completer=_completer(),
            complete_while_typing=True,
            enable_history_search=True,
            bottom_toolbar=self.bottom_toolbar,
            key_bindings=self.key_bindings,
        )

    # ------------------------------------------------------------------ ui

    def redraw_banner(self) -> None:
        """Re-render the welcome banner at the current console width."""
        width = self.console.size.width
        self.console.print(
            render_welcome(
                self.facade,
                width=width,
                email=self.email,
                target=self.session.target,
            )
        )

    def quick_show_target(self) -> None:
        """Print the active target on its own line (used by Ctrl+T)."""
        if self.session.target:
            self.console.print(f"target: @{self.session.target}", style="accent")
        else:
            self.console.print("target: (none)", style="muted")

    def _build_key_bindings(self) -> KeyBindings:
        kb = KeyBindings()

        @kb.add("c-t")
        def _(event: KeyPressEvent) -> None:
            self.quick_show_target()

        @kb.add("c-l")
        def _(event: KeyPressEvent) -> None:
            self.console.clear()
            self.redraw_banner()

        return kb

    # ----------------------------------------------------------------- io

    def _prompt_prefix(self) -> str:
        target = f"@{self.session.target}" if self.session.target else "@-"
        return f"insto {target}> "

    async def _read_line(self) -> str:
        with patch_stdout(raw=True):
            return await self.prompt_session.prompt_async(self._prompt_prefix())

    async def _execute(self, line: str) -> None:
        head = line.lstrip("/").split(maxsplit=1)[0].lower() if line.strip() else ""
        try:
            await dispatch(
                line,
                facade=self.facade,
                session=self.session,
                console=self.console,
            )
        except (BackendError, CommandUsageError) as exc:
            self._log.exception("repl command failed")
            self.console.print(redact_secrets(_format_error(exc)), style="err")
        except Exception as exc:  # pragma: no cover - safety net
            self._log.exception("repl crash")
            self.console.print(redact_secrets(f"unexpected error: {exc!r}"), style="err")
        finally:
            if head:
                with contextlib.suppress(Exception):
                    await self.facade.record_command(head, self.session.target)

    async def run(self) -> None:
        """Main loop: banner, prompt, dispatch, repeat. Returns on EOF / quit."""
        self.redraw_banner()
        while True:
            try:
                line = await self._read_line()
            except (EOFError, KeyboardInterrupt):
                self.console.print("bye", style="muted")
                return
            stripped = line.strip()
            if not stripped:
                continue
            head = stripped.lstrip("/").split(maxsplit=1)[0].lower()
            if head in EXIT_COMMANDS:
                self.console.print("bye", style="muted")
                return
            await self._execute(stripped)


# ---------------------------------------------------------------------------
# Bootstrap entry point used by `insto.cli.main`
# ---------------------------------------------------------------------------


async def _safe_prune(facade: OsintFacade) -> None:
    """Run retention prune in the background; swallow all errors."""
    with contextlib.suppress(Exception):
        await facade.history.prune_async()


def _bootstrap(config: Config | None = None) -> tuple[OsintFacade, Callable[[], Awaitable[None]]]:
    """Construct facade + cleanup closure. Separated so tests can stub it."""
    cfg = config if config is not None else load_config()

    from insto.backends import make_backend
    from insto.service.facade import OsintFacade
    from insto.service.history import HistoryStore

    backend = make_backend("hiker", token=cfg.hiker_token, proxy=cfg.hiker_proxy)
    history = HistoryStore(cfg.db_path)
    # Reuse a single httpx client across CDN downloads so HTTP/2 connection
    # reuse + TLS session resumption work for the whole REPL session. The
    # facade owns the client and closes it via aclose(). Same proxy as the
    # backend API client — see cli._run_oneshot for the rationale.
    import httpx as _httpx

    from insto.backends._cdn import DEFAULT_TIMEOUT as _CDN_TIMEOUT

    cdn_kwargs: dict[str, Any] = {"follow_redirects": False, "timeout": _CDN_TIMEOUT}
    if cfg.hiker_proxy:
        cdn_kwargs["proxy"] = cfg.hiker_proxy
    cdn_client = _httpx.AsyncClient(**cdn_kwargs)
    facade = OsintFacade(backend=backend, history=history, config=cfg, cdn_client=cdn_client)

    async def cleanup() -> None:
        # Cancel watches and close the backend BEFORE the history store —
        # an in-flight tick may still call into history.add_snapshot_async,
        # and closing the sqlite connection underneath it would error.
        with contextlib.suppress(Exception):
            await facade.aclose()
        with contextlib.suppress(Exception):
            history.close()

    return facade, cleanup


def run_repl(config: Config | None = None, *, email: str | None = None) -> None:
    """Synchronous entry point used by `cli.main` — runs `Repl.run` in asyncio."""
    facade, cleanup = _bootstrap(config)
    cfg = config if config is not None else facade.config

    async def _main() -> None:
        repl = Repl(facade=facade, config=cfg, email=email)
        # Best-effort retention prune on session start so the store does
        # not grow unbounded. Failures are non-fatal and silenced.
        prune_task = asyncio.create_task(_safe_prune(facade))
        try:
            await repl.run()
        finally:
            prune_task.cancel()
            with contextlib.suppress(BaseException):
                await prune_task
            await cleanup()

    asyncio.run(_main())


__all__ = [
    "EXIT_COMMANDS",
    "Repl",
    "run_repl",
]


def did_you_mean(name: str) -> str:
    """Public helper: '' or ' — did you mean /<x>?' for an unknown command name.

    Re-exports `_did_you_mean` so callers (and tests) do not import a private
    symbol from `insto.commands._base`.
    """
    from insto.commands._base import _did_you_mean

    return _did_you_mean(name)


def _format_unknown_command(name: str) -> str:
    """Used by tests to assert the unknown-command rendering shape."""
    return f"unknown command: /{name}{did_you_mean(name)}"
