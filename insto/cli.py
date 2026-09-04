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
import importlib.util
import json
import logging
import os
import shlex
import signal
import sys
from collections.abc import Callable
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import IO, Any

from insto import __version__
from insto._redact import redact_secrets
from insto.backends import AIOGRAPI_INSTALL_HINT
from insto.commands import (  # noqa: F401  — importing registers all commands
    COMMANDS,
    CommandUsageError,
    Session,
    dispatch,
    parse_command_line,
)
from insto.config import (
    BACKEND_AIOGRAPI,
    BACKEND_HIKERAPI,
    Config,
    config_dir,
    load_config,
    normalize_backend,
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
from insto.service.watch_lock import WatchLockBusyError

LOG_FILENAME = "insto.log"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3
HIKERAPI_TOKENS_URL = "https://hikerapi.com/tokens"
SETUP_HINT = "no HIKERAPI_TOKEN configured. Run `insto setup` to create one."


def _is_aiograpi_installed() -> bool:
    """Return whether the optional aiograpi backend dependency is importable."""
    return importlib.util.find_spec("aiograpi") is not None


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
    elif isinstance(exc, WatchLockBusyError):
        msg = str(exc)
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
        "--backend",
        type=normalize_backend,
        choices=(BACKEND_HIKERAPI, BACKEND_AIOGRAPI),
        default=None,
        help="backend selector for this invocation (overrides $INSTO_BACKEND and config.toml)",
    )
    parser.add_argument(
        "--no-progress",
        dest="no_progress",
        action="store_true",
        help="suppress tqdm progress bars on long-running commands "
        "(/fans, /wliked, /wcommented, /dossier). bars auto-suppress "
        "on non-TTY anyway; this flag forces them off on a TTY too.",
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
        "--non-interactive",
        dest="non_interactive",
        action="store_true",
        help="`insto setup` non-interactively: take values from "
        "$HIKERAPI_TOKEN / $INSTO_BACKEND / $HIKERAPI_PROXY / "
        "$AIOGRAPI_USERNAME / $AIOGRAPI_PASSWORD / $AIOGRAPI_TOTP_SEED "
        "(plus any existing config.toml as the fallback). For CI / "
        "automation — skips every prompt, errors instead of waiting.",
    )
    parser.add_argument(
        "target",
        nargs="?",
        default=None,
        metavar="TARGET",
        help=(
            "username (e.g. @ferrari), `setup` to run the wizard, "
            "`watch-daemon` to execute persisted watches, "
            "or `watch-service` to manage the macOS user service"
        ),
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


def _build_backend(config: Config) -> Any:
    """Construct the backend the active config selects.

    Centralised so both the one-shot CLI and the REPL get the same
    selection logic (hikerapi / aiograpi). aiograpi import-failures bubble
    up as `RuntimeError` from `make_backend`; the caller's existing
    `_format_error` handles them.
    """
    from insto.backends import make_backend

    if config.backend == BACKEND_AIOGRAPI:
        return make_backend(
            BACKEND_AIOGRAPI,
            username=config.aiograpi_username,
            password=config.aiograpi_password,
            totp_seed=config.aiograpi_totp_seed,
            session_path=config.aiograpi_session_path,
            proxy=config.hiker_proxy,
        )
    return make_backend(
        BACKEND_HIKERAPI,
        token=config.hiker_token,
        proxy=config.hiker_proxy,
    )


def _safe_load_config(
    hiker_token: str | None = None,
    proxy: str | None = None,
    backend: str | None = None,
) -> Config | None:
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
    if backend is not None:
        overrides["backend"] = backend
    try:
        return load_config(overrides or None)
    except BackendError as exc:
        print(redact_secrets(f"config error: {exc}"), file=sys.stderr)
        return None


def _run_setup_non_interactive(*, out: IO[str] | None = None) -> int:
    """Setup driven entirely by env-vars + existing config — no prompts.

    Resolution order per field: env-var → existing config.toml → built-in
    default. Errors on missing required fields (hikerapi.token when
    backend=hikerapi, aiograpi credentials when backend=aiograpi) so a CI
    run fails loudly instead of writing a half-broken config.
    """
    stream = out if out is not None else sys.stdout
    existing = _safe_load_config()

    backend = normalize_backend(
        os.environ.get("INSTO_BACKEND") or (existing.backend if existing else None)
    )
    if backend not in {BACKEND_HIKERAPI, BACKEND_AIOGRAPI}:
        print(f"--non-interactive: unknown backend {backend!r}", file=sys.stderr)
        return 2
    if backend == BACKEND_AIOGRAPI and not _is_aiograpi_installed():
        print(AIOGRAPI_INSTALL_HINT, file=stream)

    token = os.environ.get("HIKERAPI_TOKEN") or (existing.hiker_token if existing else None)
    proxy = os.environ.get("HIKERAPI_PROXY") or (existing.hiker_proxy if existing else None)

    aio_user = os.environ.get("AIOGRAPI_USERNAME") or (
        existing.aiograpi_username if existing else None
    )
    aio_pass = os.environ.get("AIOGRAPI_PASSWORD") or (
        existing.aiograpi_password if existing else None
    )
    aio_totp = os.environ.get("AIOGRAPI_TOTP_SEED") or (
        existing.aiograpi_totp_seed if existing else None
    )

    output_path = os.environ.get("INSTO_OUTPUT_DIR") or (
        str(existing.output_dir.expanduser().resolve())
        if existing
        else str((Path.cwd() / "output").resolve())
    )
    db = os.environ.get("INSTO_DB_PATH") or (
        str(existing.db_path.expanduser().resolve())
        if existing
        else str((config_dir() / "store.db").expanduser().resolve())
    )

    # Required-field guard: fail loudly so CI catches missing secrets.
    if backend == BACKEND_HIKERAPI and not token:
        print(
            "--non-interactive: backend=hikerapi but HIKERAPI_TOKEN is unset and "
            "config.toml has no [hikerapi].token. Set the env var or run interactive setup.",
            file=sys.stderr,
        )
        return 2
    if backend == BACKEND_AIOGRAPI and not (aio_user and aio_pass):
        print(
            "--non-interactive: backend=aiograpi but AIOGRAPI_USERNAME / "
            "AIOGRAPI_PASSWORD are missing. Set both env vars or run interactive setup.",
            file=sys.stderr,
        )
        return 2

    payload: dict[str, Any] = {"backend": backend, "output_dir": output_path, "db_path": db}
    hiker: dict[str, Any] = {}
    if token:
        hiker["token"] = token
    if proxy:
        hiker["proxy"] = proxy
    if hiker:
        payload["hikerapi"] = hiker
    aio_section: dict[str, Any] = {}
    if aio_user:
        aio_section["username"] = aio_user
    if aio_pass:
        aio_section["password"] = aio_pass
    if aio_totp:
        aio_section["totp_seed"] = aio_totp
    if aio_section:
        payload["aiograpi"] = aio_section

    path = write_config(payload)
    print(f"wrote {path} (backend={backend}, non-interactive)", file=stream)
    return 0


def _run_setup(
    *,
    prompt: Callable[[str], str] = input,
    secret_prompt: Callable[[str], str] | None = None,
    out: IO[str] | None = None,
    non_interactive: bool = False,
) -> int:
    """Interactive wizard. Writes `~/.insto/config.toml` (mode 0600).

    The token is read via `secret_prompt` (defaults to `getpass.getpass`)
    so it never echoes to the terminal or scrollback. Tests inject a
    scripted callable instead. If only `prompt` is overridden, the same
    callable handles the token line so existing scripted tests keep
    working.

    With ``non_interactive=True`` (CLI: ``--non-interactive``), the
    wizard takes every value from environment variables + the existing
    config.toml without prompting — for CI / automation. Errors out
    when the chosen backend's required fields are missing instead of
    waiting on stdin.
    """
    if non_interactive:
        return _run_setup_non_interactive(out=out)

    stream = out if out is not None else sys.stdout
    existing = _safe_load_config()

    if secret_prompt is None:
        secret_prompt = prompt if prompt is not input else getpass.getpass

    print("insto setup — writes ~/.insto/config.toml (mode 0600)", file=stream)
    print("press Enter to keep the shown default; values are masked on display.", file=stream)

    backend_default = normalize_backend(existing.backend if existing else None)
    backend_input = prompt(f"backend (hikerapi | aiograpi) [{backend_default}]: ").strip().lower()
    backend = normalize_backend(backend_input or backend_default)
    if backend not in {BACKEND_HIKERAPI, BACKEND_AIOGRAPI}:
        print(f"unknown backend {backend!r}; falling back to hikerapi", file=stream)
        backend = BACKEND_HIKERAPI
    if backend == BACKEND_AIOGRAPI and not _is_aiograpi_installed():
        print(AIOGRAPI_INSTALL_HINT, file=stream)

    token_default = existing.hiker_token if existing else None
    if backend == BACKEND_HIKERAPI:
        if token_default:
            token_disp = f"***{token_default[-4:]}" if len(token_default) >= 4 else "***"
            token_input = secret_prompt(
                f"hikerapi.token [{token_disp}] (get one: {HIKERAPI_TOKENS_URL}) (input hidden): "
            ).strip()
        else:
            token_input = secret_prompt(
                f"hikerapi.token (get one: {HIKERAPI_TOKENS_URL}) (input hidden): "
            ).strip()
        token = token_input or token_default
    else:
        # Keep an existing hikerapi.token alive even when switching to aiograpi —
        # an operator may want to flip back without re-entering the secret.
        token = token_default

    aio_user = existing.aiograpi_username if existing else None
    aio_pass = existing.aiograpi_password if existing else None
    aio_totp = existing.aiograpi_totp_seed if existing else None
    if backend == BACKEND_AIOGRAPI:
        u_in = prompt(f"aiograpi.username [{aio_user or '(none)'}]: ").strip()
        if u_in:
            aio_user = u_in
        p_default_disp = f"***{aio_pass[-2:]}" if aio_pass and len(aio_pass) >= 2 else "(none)"
        p_in = secret_prompt(f"aiograpi.password [{p_default_disp}] (input hidden): ").strip()
        if p_in:
            aio_pass = p_in
        t_default_disp = "***" if aio_totp else "(none)"
        t_in = secret_prompt(
            f"aiograpi.totp_seed [{t_default_disp}] (optional, input hidden): "
        ).strip()
        if t_in:
            aio_totp = t_in
        if t_in == "-":
            aio_totp = None

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
        payload["hikerapi"] = hiker
    aio_section: dict[str, Any] = {}
    if aio_user:
        aio_section["username"] = aio_user
    if aio_pass:
        aio_section["password"] = aio_pass
    if aio_totp:
        aio_section["totp_seed"] = aio_totp
    if aio_section:
        payload["aiograpi"] = aio_section
    payload["backend"] = backend
    payload["output_dir"] = output_path
    payload["db_path"] = db

    path = write_config(payload)
    print(f"wrote {path}", file=stream)
    if backend == BACKEND_HIKERAPI and not token:
        print(SETUP_HINT, file=stream)
    if backend == BACKEND_AIOGRAPI and not (aio_user and aio_pass):
        print(
            "aiograpi backend selected but credentials are incomplete; "
            "re-run `insto setup` to add username/password.",
            file=stream,
        )
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

    if config.backend == BACKEND_HIKERAPI and not config.hiker_token:
        print(SETUP_HINT, file=sys.stderr)
        return 1
    if config.backend == BACKEND_AIOGRAPI and not (
        config.aiograpi_username and config.aiograpi_password
    ):
        print(
            "no aiograpi credentials configured. Run `insto setup` and pick the aiograpi backend.",
            file=sys.stderr,
        )
        return 1

    from insto.service.runtime import open_runtime

    session = Session(target=target.lstrip("@") if target else None)
    head = cmd_argv[0].lstrip("/").lower() if cmd_argv else ""
    dispatch_ok = False
    try:
        async with open_runtime(
            config,
            role="oneshot",
            backend_factory=_build_backend,
        ) as runtime:
            from rich.console import Console

            from insto.ui.theme import get_theme

            console = Console(theme=get_theme(config.theme))
            try:
                await dispatch(line, facade=runtime.facade, session=session, console=console)
                dispatch_ok = True
                return 0
            except (BackendError, CommandUsageError) as exc:
                log.exception("one-shot failed")
                print(_format_error(exc), file=sys.stderr)
                return 1
            finally:
                if head and dispatch_ok:
                    with contextlib.suppress(Exception):
                        await runtime.facade.record_command(head, session.target)
    except Exception as exc:
        log.exception("one-shot bootstrap failed")
        print(_format_error(exc), file=sys.stderr)
        return 1


# ---------------------------------------------------------------------------
# Foreground watcher daemon
# ---------------------------------------------------------------------------


async def _run_watch_daemon(
    config: Config,
    log: logging.Logger,
    *,
    output: Callable[[str], None] | None = None,
) -> int:
    """Execute persisted watches in the foreground until SIGINT/SIGTERM."""
    from insto.service.runtime import open_runtime
    from insto.service.watch_daemon import estimate_watch_load

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed_signals: list[signal.Signals] = []

    def daemon_output(message: str) -> None:
        if output is None:
            print(message, flush=True)
        else:
            output(message)

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signum, stop_event.set)
            except (NotImplementedError, RuntimeError):
                continue
            installed_signals.append(signum)

        async with open_runtime(
            config,
            role="daemon",
            backend_factory=_build_backend,
            watch_output=daemon_output,
        ) as runtime:
            coordinator = runtime.coordinator
            assert coordinator is not None
            specs = await runtime.history.list_watches_async()
            estimate = estimate_watch_load(specs)

            daemon_output(f"watch daemon started · database: {config.db_path}")
            daemon_output(f"recovered active watches: {len(runtime.manager)}")
            daemon_output(
                "estimated load: "
                f"{estimate.ticks_per_hour:g} ticks/hour · "
                f"{estimate.backend_calls_per_hour_low:g} to "
                f"{estimate.backend_calls_per_hour_high:g} backend calls/hour"
            )
            if config.backend == BACKEND_AIOGRAPI:
                daemon_output("risk: polling can trigger rate limits or account restrictions")
            else:
                daemon_output("risk: polling consumes API quota and may incur provider cost")
            daemon_output("press Ctrl-C to stop")

            await coordinator.run(stop_event)
        return 0
    except WatchLockBusyError as exc:
        log.warning("watch daemon lock contention: %s", redact_secrets(str(exc)))
        print(_format_error(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        log.exception("watch daemon failed")
        print(_format_error(exc), file=sys.stderr)
        return 1
    finally:
        for signum in installed_signals:
            with contextlib.suppress(Exception):
                loop.remove_signal_handler(signum)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def _run_watch_service(argv: list[str]) -> int:
    """Manage the service before logging/config initialization can touch disk."""
    parser = argparse.ArgumentParser(
        prog="insto watch-service",
        description="Manage a macOS user watcher service without sudo.",
        allow_abbrev=False,
    )
    subparsers = parser.add_subparsers(dest="action", required=True)
    install = subparsers.add_parser("install", help="install and load the user service")
    install.add_argument("--env-file", type=Path, help="explicit private service TOML file")
    status = subparsers.add_parser("status", help="inspect service and persisted watches")
    status.add_argument("--json", action="store_true", help="emit versioned status JSON")
    subparsers.add_parser("uninstall", help="unload service, preserving all monitoring data")
    for arg in argv:
        if arg.partition("=")[0] in ("--hiker-token", "--proxy", "--backend"):
            parser.error("service overrides belong in protected config or an explicit --env-file")
    args = parser.parse_args(argv)
    from insto.service.watch_service import (
        install_service,
        service_status,
        uninstall_service,
    )

    try:
        if args.action == "install":
            result = asyncio.run(install_service(env_file=args.env_file))
        elif args.action == "uninstall":
            result = asyncio.run(uninstall_service())
        else:
            result = asyncio.run(service_status())
        if getattr(args, "json", False):
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        else:
            _print_service_result(result)
        return 0
    except (BackendError, OSError) as exc:
        print(_format_error(exc), file=sys.stderr)
        return 1


def _print_service_result(result: dict[str, Any]) -> None:
    print(f"watch service: {result.get('installation', 'unknown')}")
    if "registration" in result:
        print(f"registration: {result['registration']}")
    if "process" in result:
        process = result["process"]
        print(
            f"process: {process.get('state') or 'unknown'} · "
            f"PID: {process.get('pid') or 'unknown'} · "
            f"last exit: {process.get('last_exit_code')}"
        )
    for key, value in result.get("paths", {}).items():
        print(f"{key}: {value}")
    if result.get("paths", {}).get("python") and result.get("interpreter_available") is False:
        print("interpreter unavailable; reinstall the service from a durable Python environment")
    if "database_state" in result:
        print(f"database: {result['database_state']}")
    if result.get("database_error"):
        print(redact_secrets(str(result["database_error"])))
    if result.get("error"):
        print(redact_secrets(str(result["error"])))
    for watch in result.get("watches", []):
        print(
            f"@{watch.get('username', watch.get('user', '?'))} · {watch['status']} · "
            f"interval {watch['interval_seconds']}s · "
            f"last success: {watch.get('last_ok') or 'never'} · "
            f"recorded error: {'yes' if watch.get('has_error') else 'no'}"
        )


def main(argv: list[str] | None = None) -> int:
    raw_argv = argv if argv is not None else sys.argv[1:]
    if raw_argv and raw_argv[0] == "watch-service":
        return _run_watch_service(raw_argv[1:])
    parser = build_parser()
    args = parser.parse_args(raw_argv)

    if args.target == "watch-service" and args.cmd_argv is None:
        parser.error(
            "use `insto watch-service --help`; service commands do not take global options"
        )

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

    if args.no_progress:
        from insto.ui import progress

        progress.disable()

    if args.print_completion:
        return _print_completion(parser, args.print_completion)

    if args.target == "setup":
        return _run_setup(non_interactive=args.non_interactive)

    if args.target == "watch-daemon" and args.cmd_argv is None:
        config = _safe_load_config(args.hiker_token, args.proxy, args.backend)
        if config is None or (config.backend == BACKEND_HIKERAPI and not config.hiker_token):
            print(SETUP_HINT, file=sys.stderr)
            return 1
        if config.backend == BACKEND_AIOGRAPI and not (
            config.aiograpi_username and config.aiograpi_password
        ):
            print(
                "no aiograpi credentials configured. "
                "Run `insto setup` and pick the aiograpi backend.",
                file=sys.stderr,
            )
            return 1
        return asyncio.run(_run_watch_daemon(config, log))

    if args.cmd_argv:
        return asyncio.run(
            _run_oneshot(args.cmd_argv, args.target, args.proxy, args.hiker_token, log)
        )

    config = _safe_load_config(args.hiker_token, args.proxy, args.backend)
    missing_hikerapi_config = config is None or (
        config.backend == BACKEND_HIKERAPI and not config.hiker_token
    )
    if missing_hikerapi_config:
        print(SETUP_HINT, file=sys.stderr)
        if not args.interactive:
            return 1
    elif (
        config is not None
        and config.backend == BACKEND_AIOGRAPI
        and not (config.aiograpi_username and config.aiograpi_password)
    ):
        print(
            "no aiograpi credentials configured. Run `insto setup` and pick the aiograpi backend.",
            file=sys.stderr,
        )
        if not args.interactive:
            return 1

    try:
        from insto.repl import run_repl
    except ImportError:
        log.exception("REPL import failed")
        print("interactive REPL is unavailable", file=sys.stderr)
        return 1
    try:
        # `args.target` is the positional username (`insto @user`); `setup` is
        # already intercepted above, so anything here is a real target or None.
        run_repl(config=config, target=args.target)
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
    "_run_watch_daemon",
    "build_parser",
    "main",
    "setup_logging",
]
