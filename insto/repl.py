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
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import CompleteEvent, Completer, Completion, ThreadedCompleter
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.shortcuts.prompt import CompleteStyle
from prompt_toolkit.styles import Style
from rich.console import Console

from insto._redact import redact_secrets
from insto.cli import _format_error
from insto.commands import COMMANDS, CommandUsageError, Session, dispatch
from insto.config import Config, cli_history_path, load_config
from insto.exceptions import BackendError
from insto.ui.banner import render_welcome
from insto.ui.theme import get_palette, get_theme

if TYPE_CHECKING:
    from prompt_toolkit.key_binding import KeyPressEvent

    from insto.service.facade import OsintFacade

EXIT_COMMANDS = frozenset({"quit", "exit", "q"})


def _build_prompt_style(theme_name: str) -> Style:
    """Per-theme popup style. Uses the active palette's `accent` for the
    current selection so the slash-popup picks up theme switches."""
    palette = get_palette(theme_name)
    accent = palette.accent
    return Style.from_dict(
        {
            "completion-menu": "bg:#1f2228",
            "completion-menu.completion": "bg:#1f2228 #d2cdb6",
            "completion-menu.completion.current": f"bg:#3a2a14 {accent} bold",
            "completion-menu.meta.completion": "bg:#1f2228 #6f7280",
            "completion-menu.meta.completion.current": "bg:#3a2a14 #d8c9a3",
            "scrollbar.background": "bg:#1f2228",
            "scrollbar.button": "bg:#3a2a14",
        }
    )


class _SlashCommandCompleter(Completer):
    """Slack/Claude-Code-style command completer.

    Triggers on the *first token* of the line:

    - `/`           → list every command, popup opens the moment `/` is typed.
    - `/in`         → narrow to commands starting with "in".
    - `inf`         → no leading slash also works (REPL strips `/` anyway).

    Once the first token is finished (cursor is past whitespace), the completer
    stays silent so per-command argument completion can plug in later without
    fighting this layer.
    """

    def __init__(self) -> None:
        from insto.commands._base import command_signature

        # Pre-compute (name, signature, help) so the menu shows positional
        # arguments next to the command name (`/theme <name>`, `/info [target]`).
        self._items: list[tuple[str, str, str]] = sorted(
            (name, command_signature(spec), spec.help) for name, spec in COMMANDS.items()
        )

    def get_completions(
        self, document: Document, complete_event: CompleteEvent
    ) -> Iterable[Completion]:
        text = document.text_before_cursor
        stripped = text.lstrip()

        # ── Argument completion: cursor is past the first whitespace ─────────
        if " " in stripped:
            yield from self._argument_completions(text, stripped)
            return

        # ── Command-name completion: still in the first token ────────────────
        prefix = stripped
        if prefix.startswith("/"):
            user_typed = prefix[1:].lower()
            slash = "/"
        else:
            user_typed = prefix.lower()
            slash = ""
        for name, signature, help_text in self._items:
            if not name.lower().startswith(user_typed):
                continue
            yield Completion(
                text=f"{slash}{name}",
                start_position=-len(prefix),
                display=signature,  # e.g. "/theme [name]" — args visible in popup
                display_meta=help_text,
            )

        # ── Bonus: if the typed token is *exactly* a command name with
        #    positional choices (`/theme`, `/purge`), also yield those
        #    choices in the same popup. Without this, the user has to type
        #    a trailing space before Tab does anything useful — confusing
        #    on first use because the popup shows `/theme [name]` but
        #    pressing Tab just re-inserts `/theme` and stops.
        from insto.commands._base import COMMANDS

        spec = COMMANDS.get(user_typed)
        if spec is not None:
            yield from self._argument_completions(
                text=f"{text} ",
                stripped=f"{stripped} ",
            )

    def _argument_completions(self, text: str, stripped: str) -> Iterable[Completion]:
        """Per-command positional-argument completion.

        Reads `choices=` off the command's argparse parser. `/theme <Tab>`
        offers `claude / instagram / aiograpi`; `/purge <Tab>` offers
        `history / snapshots / cache`. Commands whose positionals have no
        `choices` (free-form usernames, file paths, post codes) yield no
        completions and the popup quietly stays empty.
        """
        from insto.commands._base import COMMANDS, build_parser_for

        tokens = stripped.split()
        cmd_name = tokens[0].lstrip("/").lower()
        spec = COMMANDS.get(cmd_name)
        if spec is None:
            return

        # If the cursor sits right after a space, the user is starting a
        # fresh word; otherwise the last token is what's being typed.
        if text.endswith(" "):
            current_word = ""
            consumed = len(tokens) - 1  # everything after the command
        else:
            current_word = tokens[-1]
            consumed = len(tokens) - 2  # last token is the in-progress word

        parser = build_parser_for(spec)
        pos_index = 0
        for action in parser._actions:
            if action.option_strings or action.dest in ("help",):
                continue
            if pos_index == consumed:
                choices = action.choices
                if not choices:
                    return
                lower = current_word.lower()
                for choice in choices:
                    s = str(choice)
                    if not s.lower().startswith(lower):
                        continue
                    yield Completion(
                        text=s,
                        start_position=-len(current_word),
                        display=s,
                        display_meta=str(action.help or ""),
                    )
                return
            pos_index += 1


def _completer() -> Completer:
    """Slash-aware command completer (popup opens on `/`)."""
    return _SlashCommandCompleter()


def _format_quota(facade: OsintFacade) -> str:
    """Render the toolbar quota fragment lazily."""
    try:
        quota = facade.backend.get_quota()
    except Exception:
        return "quota: ?"
    if quota is None or quota.remaining is None:
        return "quota: ?"
    parts = [_format_count(quota.remaining) + " req"]
    if quota.amount is not None and quota.currency:
        parts.append(_format_money(quota.amount, quota.currency))
    if quota.rate is not None:
        parts.append(f"{quota.rate} rps")
    return " · ".join(parts)


def _format_count(n: int) -> str:
    """Render a request count: 14722577 -> '14.7M', 1500 -> '1.5K'."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _format_money(amount: float, currency: str) -> str:
    sym = {"USD": "$", "EUR": "€", "GBP": "£"}.get(currency.upper(), currency + " ")
    return f"{sym}{amount:,.0f}" if amount >= 100 else f"{sym}{amount:,.2f}"


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
        self.console = console or Console(theme=get_theme(config.theme))
        self.session = Session()
        self.email = email
        self._log = logging.getLogger("insto.repl")
        self._history_path = history_path or cli_history_path()
        self._history_path.parent.mkdir(parents=True, exist_ok=True)
        self.bottom_toolbar = _make_bottom_toolbar(self.facade, self.session)
        self.key_bindings = self._build_key_bindings()
        self.prompt_session: PromptSession[str] = PromptSession(
            history=FileHistory(str(self._history_path)),
            completer=ThreadedCompleter(_completer()),
            complete_while_typing=True,
            complete_style=CompleteStyle.COLUMN,
            reserve_space_for_menu=10,
            style=_build_prompt_style(config.theme),
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

        # Slash-popup keystroke handling.
        #
        # `complete_while_typing=True` *is* enabled, but prompt_toolkit
        # debounces it — the popup briefly collapses on each keystroke before
        # re-opening, which reads as flicker / "popup disappeared" when the
        # user types '/i'. Bind every character that can appear in a command
        # name (the registered names are `[a-z0-9_-]+`) plus '/' so each
        # keystroke synchronously inserts the char *and* re-opens the menu.
        #
        # Filter: only fires while the cursor is still inside the first
        # token. Once the user types a space (entering arguments), regular
        # input flow resumes — no completer fights with positional parsing.
        from prompt_toolkit.application import get_app
        from prompt_toolkit.filters import Condition

        @Condition
        def _in_first_token() -> bool:
            text = get_app().current_buffer.document.text_before_cursor
            return " " not in text

        def _make_keep_popup(ch: str) -> Callable[[KeyPressEvent], None]:
            def handler(event: KeyPressEvent) -> None:
                buf = event.current_buffer
                buf.insert_text(ch)
                buf.start_completion(select_first=False)

            return handler

        command_name_chars = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-/"
        for ch in command_name_chars:
            kb.add(ch, filter=_in_first_token)(_make_keep_popup(ch))

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
        ok = False
        try:
            await dispatch(
                line,
                facade=self.facade,
                session=self.session,
                console=self.console,
            )
            ok = True
        except (BackendError, CommandUsageError) as exc:
            self._log.exception("repl command failed")
            self.console.print(_format_error(exc), style="err")
        except Exception as exc:  # pragma: no cover - safety net
            self._log.exception("repl crash")
            self.console.print(redact_secrets(f"unexpected error: {exc!r}"), style="err")
        finally:
            # Skip recording when dispatch failed — typo'd commands like
            # `/inof` would otherwise pollute /history.
            if head and ok:
                with contextlib.suppress(Exception):
                    await self.facade.record_command(head, self.session.target)

    async def run(self) -> None:
        """Main loop: banner, prompt, dispatch, repeat. Returns on EOF / quit.

        Ctrl-C cancels the in-progress line and re-prompts (shell-style),
        matching bash / zsh / Python REPL conventions. Ctrl-D on an empty
        line exits, as does typing /exit, /quit, or just /q.
        """
        self.redraw_banner()
        while True:
            try:
                line = await self._read_line()
            except KeyboardInterrupt:
                # Ctrl-C: drop the current input, stay in the REPL.
                continue
            except EOFError:
                # Ctrl-D on an empty line: exit the REPL gracefully.
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


async def _safe_refresh_quota(facade: OsintFacade, *, timeout: float = 2.0) -> None:
    """Refresh backend quota; swallow all errors and bound by timeout.

    Bounded so a dead network at startup adds at most `timeout` seconds to
    REPL spin-up before the welcome banner falls back to "balance: pending".
    """
    backend = facade.backend
    refresh = getattr(backend, "refresh_quota", None)
    if refresh is None:
        return
    with contextlib.suppress(Exception):
        await asyncio.wait_for(refresh(), timeout=timeout)


def _bootstrap(config: Config | None = None) -> tuple[OsintFacade, Callable[[], Awaitable[None]]]:
    """Construct facade + cleanup closure. Separated so tests can stub it."""
    cfg = config if config is not None else load_config()

    # Reuse a single httpx client across CDN downloads so HTTP/2 connection
    # reuse + TLS session resumption work for the whole REPL session. The
    # facade owns the client and closes it via aclose(). Same proxy as the
    # backend API client — see cli._run_oneshot for the rationale.
    import httpx as _httpx

    from insto.backends._cdn import DEFAULT_TIMEOUT as _CDN_TIMEOUT
    from insto.cli import _build_backend
    from insto.service.facade import OsintFacade
    from insto.service.history import HistoryStore

    # Open the sqlite store first: it is the most likely step to raise
    # (permissions, disk, schema migration) and is the only resource that
    # needs sync cleanup. Backend and cdn clients are constructed last so
    # a `HistoryStore(...)` failure does not leak network sockets.
    history = HistoryStore(cfg.db_path)
    try:
        backend = _build_backend(cfg)
        cdn_kwargs: dict[str, Any] = {"follow_redirects": False, "timeout": _CDN_TIMEOUT}
        if cfg.hiker_proxy:
            cdn_kwargs["proxy"] = cfg.hiker_proxy
        cdn_client = _httpx.AsyncClient(**cdn_kwargs)
        facade = OsintFacade(backend=backend, history=history, config=cfg, cdn_client=cdn_client)
    except BaseException:
        with contextlib.suppress(Exception):
            history.close()
        raise

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
        # Refresh quota *before* the welcome screen renders, so the banner
        # shows real numbers ("14.7M requests left · $4,417") instead of
        # "balance: pending". The roundtrip costs ~200ms and is bounded by a
        # short timeout in _safe_refresh_quota; if the network is dead, the
        # banner falls back to the "pending" label and the REPL still opens.
        await _safe_refresh_quota(facade)

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
