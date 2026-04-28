"""CLI entry point for insto: argparse-driven one-shot mode + REPL launcher.

Surface area:

- `insto`                            — interactive REPL (default).
- `insto setup`                      — interactive wizard, writes
  `~/.insto/config.toml` (mode 0600).
- `insto @user -c <cmd> [args]`      — one-shot: run a single slash-command
  with `@user` as the active target.
- `insto --print-completion {bash|zsh}` — emit a shell-completion script.
- `--verbose` / `--debug`            — set logging level for the rotating
  log file under `~/.insto/logs/insto.log`.

Every error string the user sees on stderr — from the wizard, from
one-shot dispatch, from the completion path — first goes through
`_format_error()`, which maps every backend exception into a one-line,
human-readable message and runs the result through
`insto._redact.redact_secrets()` so that an accidentally-leaked token in
an exception arg never makes it to a terminal or copy-pasted bug
report. The same redaction is applied by the logging formatter so log
files stay safe to share.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import getpass
import logging
import os
import shlex
import sys
from collections.abc import Callable
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import IO, Any

from insto import __version__
from insto._redact import redact_secrets
from insto.commands import (  # noqa: F401  — importing registers all commands
    COMMANDS,
    CommandUsageError,
    Session,
    dispatch,
    parse_command_line,
)
from insto.config import (
    Config,
    config_dir,
    load_config,
    write_config,
)
from insto.exceptions import (
    AuthInvalid,
    BackendError,
    Banned,
    PostNotFound,
    PostPrivate,
    ProfileBlocked,
    ProfileDeleted,
    ProfileNotFound,
    ProfilePrivate,
    QuotaExhausted,
    RateLimited,
    SchemaDrift,
    Transient,
)

LOG_FILENAME = "insto.log"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3
SETUP_HINT = "no HIKERAPI_TOKEN configured. Run `insto setup` to create one."


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


class RedactingFormatter(logging.Formatter):
    """Logging formatter that runs every record through `redact_secrets`.

    Wrapping `format()` (rather than the message templating) guarantees the
    full final string — including the rendered exception traceback — is
    redacted before it lands on disk.
    """

    def format(self, record: logging.LogRecord) -> str:
        return redact_secrets(super().format(record))


class _SecureRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler that creates and keeps the log file at mode 0600."""

    def _open(self) -> Any:
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        fd = os.open(self.baseFilename, flags, 0o600)
        with os.fdopen(fd, "a", encoding=self.encoding or "utf-8"):
            pass
        with contextlib.suppress(OSError):
            os.chmod(self.baseFilename, 0o600)
        return super()._open()


def setup_logging(level: int, *, log_dir: Path | None = None) -> Path:
    """Configure the `insto` logger to write to a 0600 rotating file."""
    target_dir = log_dir if log_dir is not None else (config_dir() / "logs")
    target_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        target_dir.chmod(0o700)
    log_path = target_dir / LOG_FILENAME

    root = logging.getLogger("insto")
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    root.setLevel(level)
    root.propagate = False

    handler = _SecureRotatingFileHandler(
        log_path,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setLevel(level)
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.addHandler(handler)
    return log_path


# ---------------------------------------------------------------------------
# Error formatting
# ---------------------------------------------------------------------------


def _format_error(exc: BaseException) -> str:
    """Return a redacted, one-line description of `exc` suitable for stderr."""
    if isinstance(exc, ProfileNotFound):
        msg = f"profile not found: @{exc.username}"
    elif isinstance(exc, ProfilePrivate):
        msg = f"profile is private: @{exc.username}"
    elif isinstance(exc, ProfileBlocked):
        msg = f"profile has blocked us: @{exc.username}"
    elif isinstance(exc, ProfileDeleted):
        msg = f"profile is deleted: @{exc.username}"
    elif isinstance(exc, PostNotFound):
        msg = f"post not found: {exc.ref}"
    elif isinstance(exc, PostPrivate):
        msg = f"post is private: {exc.ref}"
    elif isinstance(exc, AuthInvalid):
        msg = "auth invalid — run `insto setup` to refresh the HikerAPI token"
    elif isinstance(exc, QuotaExhausted):
        msg = "quota exhausted — wait for the next window or upgrade your HikerAPI plan"
    elif isinstance(exc, RateLimited):
        msg = f"rate limited — retry after {exc.retry_after:.1f}s"
    elif isinstance(exc, SchemaDrift):
        msg = f"schema drift in {exc.endpoint}: missing field {exc.missing_field!r}"
    elif isinstance(exc, Transient):
        msg = f"transient backend error: {exc.detail}"
    elif isinstance(exc, Banned):
        # The Banned class is reused for both "your aiograpi-logged-in
        # account is suspended" and "HikerAPI 403 forbidden for this
        # endpoint" — the message itself carries the diagnosis, no need
        # to bolt on a misleading prefix.
        msg = str(exc)
    elif isinstance(exc, BackendError):
        msg = f"backend error: {exc}"
    elif isinstance(exc, CommandUsageError):
        msg = f"usage: {exc}"
    else:
        msg = f"{type(exc).__name__}: {exc}"
    return redact_secrets(msg)


# ---------------------------------------------------------------------------
# Argparse
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Construct the top-level argparse parser used by `insto.cli.main`."""
    parser = argparse.ArgumentParser(
        prog="insto",
        description="Interactive Instagram OSINT CLI on the HikerAPI backend.",
    )
    parser.add_argument("--version", action="version", version=f"insto {__version__}")
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="enable INFO logging to ~/.insto/logs/insto.log",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="enable DEBUG logging to ~/.insto/logs/insto.log",
    )
    parser.add_argument(
        "--proxy",
        default=None,
        metavar="URL",
        help="HTTP/SOCKS5 proxy (overrides $HIKERAPI_PROXY and config.toml)",
    )
    parser.add_argument(
        "--hiker-token",
        dest="hiker_token",
        default=None,
        metavar="TOKEN",
        help="HikerAPI token (overrides $HIKERAPI_TOKEN and config.toml)",
    )
    parser.add_argument(
        "--print-completion",
        dest="print_completion",
        choices=("bash", "zsh"),
        default=None,
        metavar="SHELL",
        help="print a shell completion script and exit",
    )
    parser.add_argument(
        "--interactive",
        "-i",
        action="store_true",
        help="force the interactive REPL (default when no command is given)",
    )
    parser.add_argument(
        "target",
        nargs="?",
        default=None,
        metavar="TARGET",
        help="username (e.g. @ferrari) or the literal `setup` to run the wizard",
    )
    parser.add_argument(
        "-c",
        "--cmd",
        dest="cmd_argv",
        nargs=argparse.REMAINDER,
        default=None,
        metavar="CMD",
        help="one-shot: command name and its arguments (everything after -c)",
    )
    return parser


# ---------------------------------------------------------------------------
# Setup wizard
# ---------------------------------------------------------------------------


def _safe_load_config(hiker_token: str | None = None, proxy: str | None = None) -> Config | None:
    """Load config; surface security-relevant failures to stderr.

    A `BackendError` from `load_config()` typically means the config file
    is group/world-readable — exactly the security signal the operator
    needs to see. Swallowing it would degrade the message to the generic
    "no token configured" hint, masking a likely tampering or
    permissions-drift event. We print it (redacted) and return `None` so
    the caller can choose whether to bail or fall back to setup.
    """
    overrides: dict[str, Any] = {}
    if hiker_token is not None:
        overrides["hiker_token"] = hiker_token
    if proxy is not None:
        overrides["hiker_proxy"] = proxy
    try:
        return load_config(overrides or None)
    except BackendError as exc:
        print(redact_secrets(f"config error: {exc}"), file=sys.stderr)
        return None


def _run_setup(
    *,
    prompt: Callable[[str], str] = input,
    secret_prompt: Callable[[str], str] | None = None,
    out: IO[str] | None = None,
) -> int:
    """Interactive wizard. Writes `~/.insto/config.toml` (mode 0600).

    The token is read via `secret_prompt` (defaults to `getpass.getpass`)
    so it never echoes to the terminal or scrollback. Tests inject a
    scripted callable instead. If only `prompt` is overridden, the same
    callable handles the token line so existing scripted tests keep
    working.
    """
    stream = out if out is not None else sys.stdout
    existing = _safe_load_config()

    if secret_prompt is None:
        secret_prompt = prompt if prompt is not input else getpass.getpass

    print("insto setup — writes ~/.insto/config.toml (mode 0600)", file=stream)
    print("press Enter to keep the shown default; values are masked on display.", file=stream)

    token_default = existing.hiker_token if existing else None
    if token_default:
        token_disp = f"***{token_default[-4:]}" if len(token_default) >= 4 else "***"
        token_input = secret_prompt(f"hiker.token [{token_disp}] (input hidden): ").strip()
    else:
        token_input = secret_prompt("hiker.token (input hidden): ").strip()
    token = token_input or token_default

    out_default = str(
        existing.output_dir.expanduser().resolve()
        if existing
        else (Path.cwd() / "output").resolve()
    )
    out_input = prompt(f"output_dir [{out_default}]: ").strip()
    output_path = str(Path(out_input).expanduser().resolve()) if out_input else out_default

    db_default = str(
        existing.db_path.expanduser().resolve()
        if existing
        else (config_dir() / "store.db").expanduser().resolve()
    )
    db_input = prompt(f"db_path [{db_default}]: ").strip()
    db = str(Path(db_input).expanduser().resolve()) if db_input else db_default

    proxy_default = (existing.hiker_proxy or "") if existing else ""
    proxy_disp = proxy_default if proxy_default else "(none)"
    proxy_input = prompt(
        f"proxy URL (http://, https://, socks5h://) (optional, '-' to clear) [{proxy_disp}]: "
    ).strip()
    if proxy_input == "":
        proxy = proxy_default
    elif proxy_input == "-":
        proxy = ""
    else:
        proxy = proxy_input

    payload: dict[str, Any] = {}
    hiker: dict[str, Any] = {}
    if token:
        hiker["token"] = token
    if proxy:
        hiker["proxy"] = proxy
    if hiker:
        payload["hiker"] = hiker
    payload["output_dir"] = output_path
    payload["db_path"] = db

    path = write_config(payload)
    print(f"wrote {path}", file=stream)
    if not token:
        print(SETUP_HINT, file=stream)
    return 0


# ---------------------------------------------------------------------------
# Shell completion
# ---------------------------------------------------------------------------


def _print_completion(parser: argparse.ArgumentParser, shell: str) -> int:
    try:
        import shtab  # type: ignore[import-not-found,unused-ignore]
    except ImportError:
        print(
            "shell completion requires `pip install insto[completion]`",
            file=sys.stderr,
        )
        return 1
    script = shtab.complete(parser, shell=shell)
    sys.stdout.write(script)
    if not script.endswith("\n"):
        sys.stdout.write("\n")
    return 0


# ---------------------------------------------------------------------------
# One-shot dispatch
# ---------------------------------------------------------------------------


async def _run_oneshot(
    cmd_argv: list[str],
    target: str | None,
    proxy: str | None,
    hiker_token: str | None,
    log: logging.Logger,
) -> int:
    """Run a single command line against a freshly-constructed facade."""
    line = shlex.join(cmd_argv)
    log.debug("one-shot dispatch: %s", line)

    cli_overrides: dict[str, Any] = {}
    if proxy is not None:
        cli_overrides["hiker_proxy"] = proxy
    if hiker_token is not None:
        cli_overrides["hiker_token"] = hiker_token
    try:
        config = load_config(cli_overrides)
    except BackendError as exc:
        # `load_config` raises BackendError for security-relevant failures
        # (e.g. group/world-readable config). Surface the redacted message
        # instead of letting a raw traceback escape from `asyncio.run`.
        print(redact_secrets(f"config error: {exc}"), file=sys.stderr)
        return 1
    if not config.hiker_token:
        print(SETUP_HINT, file=sys.stderr)
        return 1

    # Reuse a single httpx client for every CDN download in the run so we do
    # not pay TCP/TLS handshake cost on each media URL. Closed by facade.aclose().
    # Route CDN downloads through the same proxy as backend API calls — an
    # operator who configured `hiker_proxy` for OSINT identity protection
    # expects media fetches (which hit `*.cdninstagram.com` / `*.fbcdn.net`)
    # to be proxied just like the API surface.
    import httpx as _httpx

    from insto.backends import make_backend
    from insto.backends._cdn import DEFAULT_TIMEOUT as _CDN_TIMEOUT
    from insto.service.facade import OsintFacade
    from insto.service.history import HistoryStore

    # Construct backend / history / cdn-client / facade through `_format_error`
    # so an OSError from the sqlite open (disk full, EACCES, read-only FS) or
    # a constructor failure does not escape `asyncio.run` as a raw traceback —
    # which would bypass `redact_secrets` and could leak path/secret info.
    # Open sqlite first — it is the only step likely to fail with a real
    # I/O error (permissions / disk / schema migration). Constructing the
    # backend and cdn clients afterwards means a HistoryStore failure does
    # not leak network sockets, and a later failure can be cleaned up with
    # the async aclose() calls below since we are already in an event loop.
    history: HistoryStore | None = None
    backend: Any = None
    cdn_client: Any = None
    try:
        history = HistoryStore(config.db_path)
        backend = make_backend("hiker", token=config.hiker_token, proxy=config.hiker_proxy)
        cdn_kwargs: dict[str, Any] = {"follow_redirects": False, "timeout": _CDN_TIMEOUT}
        if config.hiker_proxy:
            cdn_kwargs["proxy"] = config.hiker_proxy
        cdn_client = _httpx.AsyncClient(**cdn_kwargs)
        facade = OsintFacade(backend=backend, history=history, config=config, cdn_client=cdn_client)
    except Exception as exc:
        log.exception("one-shot bootstrap failed")
        print(_format_error(exc), file=sys.stderr)
        if cdn_client is not None:
            with contextlib.suppress(Exception):
                await cdn_client.aclose()
        if backend is not None:
            with contextlib.suppress(Exception):
                await backend.aclose()
        if history is not None:
            with contextlib.suppress(Exception):
                history.close()
        return 1
    assert history is not None  # for mypy: set above on the success path

    session = Session(target=target.lstrip("@") if target else None)
    head = cmd_argv[0].lstrip("/").lower() if cmd_argv else ""
    dispatch_ok = False
    try:
        from rich.console import Console

        from insto.ui.theme import get_theme

        console = Console(theme=get_theme(config.theme))
        await dispatch(line, facade=facade, session=session, console=console)
        dispatch_ok = True
        return 0
    except (BackendError, CommandUsageError) as exc:
        log.exception("one-shot failed")
        print(_format_error(exc), file=sys.stderr)
        return 1
    finally:
        # Only record successful dispatches in cli_history — typo'd / failed
        # invocations would otherwise pollute /history and the welcome screen's
        # "recent activity" with garbage rows.
        if head and dispatch_ok:
            with contextlib.suppress(Exception):
                await facade.record_command(head, session.target)
        # Close backend (cancels any pending tasks that may still touch
        # history) before tearing down the sqlite store.
        with contextlib.suppress(Exception):
            await facade.aclose()
        with contextlib.suppress(Exception):
            history.close()


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    if args.debug:
        level = logging.DEBUG
    elif args.verbose:
        level = logging.INFO
    else:
        level = logging.WARNING
    with contextlib.suppress(OSError):
        # Logging setup failures must never break the CLI itself.
        setup_logging(level)
    log = logging.getLogger("insto.cli")

    if args.print_completion:
        return _print_completion(parser, args.print_completion)

    if args.target == "setup":
        return _run_setup()

    if args.cmd_argv:
        return asyncio.run(
            _run_oneshot(args.cmd_argv, args.target, args.proxy, args.hiker_token, log)
        )

    config = _safe_load_config(args.hiker_token, args.proxy)
    if config is None or not config.hiker_token:
        print(SETUP_HINT, file=sys.stderr)
        if not args.interactive:
            return 1

    try:
        from insto.repl import run_repl
    except ImportError:
        log.exception("REPL import failed")
        print("interactive REPL is unavailable", file=sys.stderr)
        return 1
    try:
        run_repl(config=config)
    except NotImplementedError:
        print("interactive REPL is not implemented in this build", file=sys.stderr)
        return 1
    except (BackendError, CommandUsageError) as exc:
        print(_format_error(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        log.exception("REPL bootstrap failed")
        print(_format_error(exc), file=sys.stderr)
        return 1
    return 0


__all__ = [
    "LOG_BACKUP_COUNT",
    "LOG_FILENAME",
    "LOG_MAX_BYTES",
    "SETUP_HINT",
    "RedactingFormatter",
    "_format_error",
    "build_parser",
    "main",
    "setup_logging",
]
