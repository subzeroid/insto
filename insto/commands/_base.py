"""Command-layer plumbing: registry, decorators, parsers, helpers.

Each command function is registered via `@command(name, help, ...)`. The
registration step also stashes the per-command argparse builder so the
dispatcher can construct a parser on demand. Every command parser inherits
from a single `_GLOBAL_PARSER` (`parents=[...]`) so that flags like
`--json` / `--csv` / `--limit` / `--no-download` / `--yes` are defined in
exactly one place.

`with_target` and `with_pk` are thin async wrappers that resolve the active
target (positional arg or session-level `/target`) before calling the
underlying function. The session pk-cache lives on `OsintFacade`; the
helpers just route through `facade.resolve_pk(...)`.

Pipeline-friendly I/O lives here too: `--json -` (or `--csv -`) routes to
`sys.stdout.buffer` via `resolve_export_dest`. The exporter accepts either
a Path or a writable binary stream, so commands do not have to branch on
which one they were given.
"""

from __future__ import annotations

import argparse
import functools
import shlex
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from difflib import get_close_matches
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any

from insto.service.exporter import CSV_FLAT_COMMANDS

if TYPE_CHECKING:  # avoid import cycle at runtime; only used for typing
    from insto.service.facade import OsintFacade


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CommandUsageError(Exception):
    """Raised when a command line is malformed or violates global-flag rules.

    The cli/repl is expected to print `str(err)` and continue. argparse's
    own `SystemExit` path is suppressed by `_NonExitingParser` — every parse
    failure surfaces as this exception instead.
    """


class _NonExitingParser(argparse.ArgumentParser):
    """argparse parser that raises `CommandUsageError` instead of `SystemExit`."""

    def error(self, message: str) -> None:  # type: ignore[override]
        raise CommandUsageError(message)

    def exit(self, status: int = 0, message: str | None = None) -> None:  # type: ignore[override]
        if message:
            raise CommandUsageError(message)
        raise CommandUsageError(f"command exited with status {status}")


# ---------------------------------------------------------------------------
# Global flag parser (parent for every command)
# ---------------------------------------------------------------------------


def _build_global_parser() -> argparse.ArgumentParser:
    parser = _NonExitingParser(add_help=False)
    parser.add_argument(
        "--json",
        nargs="?",
        const="",
        default=None,
        metavar="DEST",
        help='write JSON to DEST (or "-" for stdout)',
    )
    parser.add_argument(
        "--csv",
        nargs="?",
        const="",
        default=None,
        metavar="DEST",
        help='write flat CSV to DEST (or "-" for stdout); only on flat-row commands',
    )
    parser.add_argument(
        "--maltego",
        action="store_true",
        help="alias for --output-format maltego",
    )
    parser.add_argument(
        "--output-format",
        choices=("json", "csv", "maltego"),
        default=None,
        help="explicit output format (overrides --json/--csv)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="cap the number of items fetched / rendered",
    )
    parser.add_argument(
        "--no-download",
        action="store_true",
        help="for media commands: print URLs instead of writing files",
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="skip confirmation prompts",
    )
    return parser


_GLOBAL_PARSER = _build_global_parser()


def global_parser() -> argparse.ArgumentParser:
    """Return the shared parent parser. Useful for tests / introspection."""
    return _GLOBAL_PARSER


# ---------------------------------------------------------------------------
# Session + context
# ---------------------------------------------------------------------------


@dataclass
class Session:
    """REPL-scope state shared across commands within one session.

    The CLI one-shot path constructs a fresh `Session()` per invocation;
    the REPL keeps a single instance for the lifetime of the prompt loop.
    """

    target: str | None = None

    def set_target(self, username: str) -> None:
        cleaned = username.lstrip("@").strip()
        if not cleaned:
            raise CommandUsageError("target username is empty")
        self.target = cleaned

    def clear(self) -> None:
        self.target = None


@dataclass
class CommandContext:
    """Per-call context passed to every command function.

    `facade` is the single dependency the command has on the rest of the
    system. `args` holds parsed argparse flags (both global and command-
    specific). `session` carries cross-command REPL state.
    """

    facade: OsintFacade
    args: argparse.Namespace
    session: Session

    @property
    def no_download(self) -> bool:
        return bool(getattr(self.args, "no_download", False))

    @property
    def yes(self) -> bool:
        return bool(getattr(self.args, "yes", False))

    @property
    def limit(self) -> int | None:
        return getattr(self.args, "limit", None)

    def output_format(self) -> str | None:
        """Return canonical export format: 'json', 'csv', 'maltego', or None."""
        if self.args.output_format:
            return str(self.args.output_format)
        if self.args.maltego:
            return "maltego"
        if self.args.json is not None:
            return "json"
        if self.args.csv is not None:
            return "csv"
        return None


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


CommandFn = Callable[[CommandContext], Awaitable[Any]]
ArgsBuilder = Callable[[argparse.ArgumentParser], None]


def _noop_args(_: argparse.ArgumentParser) -> None:
    return None


@dataclass(frozen=True)
class CommandSpec:
    """Static metadata for one command, stored in the global `COMMANDS` dict."""

    name: str
    help: str
    fn: CommandFn
    add_args: ArgsBuilder = field(default=_noop_args)
    csv: bool = False
    requires: tuple[str, ...] = ()


COMMANDS: dict[str, CommandSpec] = {}


def command(
    name: str,
    help: str,
    *,
    add_args: ArgsBuilder | None = None,
    csv: bool = False,
    requires: tuple[str, ...] = (),
) -> Callable[[CommandFn], CommandFn]:
    """Register `fn` in `COMMANDS` under `name`.

    `csv=True` means the command produces flat rows and is allowed to be
    exported as CSV — must also be listed in
    `insto.service.exporter.CSV_FLAT_COMMANDS`. `requires=("target",)` means
    the dispatcher must guarantee a target before invoking.
    """

    def decorator(fn: CommandFn) -> CommandFn:
        spec = CommandSpec(
            name=name,
            help=help,
            fn=fn,
            add_args=add_args or _noop_args,
            csv=csv,
            requires=requires,
        )
        COMMANDS[name] = spec
        return fn

    return decorator


def build_parser_for(spec: CommandSpec) -> argparse.ArgumentParser:
    """Construct the per-command parser, inheriting global flags."""
    parser = _NonExitingParser(
        prog=f"/{spec.name}",
        description=spec.help,
        parents=[_GLOBAL_PARSER],
        add_help=False,
    )
    spec.add_args(parser)
    return parser


# ---------------------------------------------------------------------------
# Validation + parsing
# ---------------------------------------------------------------------------


def validate_global_flags(name: str, args: argparse.Namespace) -> None:
    """Apply mutual-exclusion rules and flat-only CSV check."""
    if args.json is not None and args.csv is not None:
        raise CommandUsageError(
            "--json and --csv are mutually exclusive; pick one"
        )
    if args.maltego and args.output_format and args.output_format != "maltego":
        raise CommandUsageError(
            "--maltego conflicts with --output-format "
            f"{args.output_format!r}; --maltego is short for --output-format maltego"
        )
    fmt = args.output_format
    if args.maltego:
        fmt = "maltego"
    elif args.json is not None:
        fmt = "json"
    elif args.csv is not None:
        fmt = "csv"
    if fmt == "csv" and name not in CSV_FLAT_COMMANDS:
        flat = ", ".join(sorted(CSV_FLAT_COMMANDS))
        raise CommandUsageError(
            f"/{name} cannot be exported as CSV (output is not flat). "
            f"Use --json instead. Flat-row commands: {flat}"
        )


def parse_command_line(line: str) -> tuple[CommandSpec, argparse.Namespace]:
    """Parse a REPL-style command line into `(spec, args)`.

    A leading `/` is optional and stripped. Unknown commands raise
    `CommandUsageError` with a did-you-mean suggestion when there is one.
    """
    stripped = line.strip()
    if stripped.startswith("/"):
        stripped = stripped[1:].lstrip()
    if not stripped:
        raise CommandUsageError("empty command")
    try:
        tokens = shlex.split(stripped)
    except ValueError as exc:
        raise CommandUsageError(f"failed to parse command line: {exc}") from exc
    if not tokens:
        raise CommandUsageError("empty command")
    name, rest = tokens[0], tokens[1:]
    spec = COMMANDS.get(name)
    if spec is None:
        suggestion = _did_you_mean(name)
        raise CommandUsageError(f"unknown command: /{name}{suggestion}")
    parser = build_parser_for(spec)
    args = parser.parse_args(rest)
    validate_global_flags(spec.name, args)
    return spec, args


def _did_you_mean(name: str) -> str:
    matches = get_close_matches(name, list(COMMANDS), n=1, cutoff=0.6)
    if matches:
        return f" — did you mean /{matches[0]}?"
    return ""


async def dispatch(
    line: str,
    *,
    facade: OsintFacade,
    session: Session,
) -> Any:
    """Parse `line` and execute the matching command. Returns its return value."""
    spec, args = parse_command_line(line)
    ctx = CommandContext(facade=facade, args=args, session=session)
    return await spec.fn(ctx)


# ---------------------------------------------------------------------------
# Target resolution decorators
# ---------------------------------------------------------------------------


def _extract_target(ctx: CommandContext) -> str:
    """Pull a target username from positional args or session state.

    Priority: an explicit positional `target` on the command's parser wins
    over session state. Raises `CommandUsageError` if neither is set.
    """
    explicit = getattr(ctx.args, "target", None)
    if explicit:
        cleaned = str(explicit).lstrip("@").strip()
        if cleaned:
            return cleaned
    if ctx.session.target:
        return ctx.session.target
    raise CommandUsageError(
        "no target set — pass a username or run /target <user> first"
    )


def with_target(
    fn: Callable[[CommandContext, str], Awaitable[Any]],
) -> CommandFn:
    """Decorator: resolve target username and pass it as second arg."""

    @functools.wraps(fn)
    async def wrapper(ctx: CommandContext) -> Any:
        username = _extract_target(ctx)
        return await fn(ctx, username)

    return wrapper


def with_pk(
    fn: Callable[[CommandContext, str], Awaitable[Any]],
) -> CommandFn:
    """Decorator: resolve target username to a pk (cached) and pass as second arg."""

    @functools.wraps(fn)
    async def wrapper(ctx: CommandContext) -> Any:
        username = _extract_target(ctx)
        pk = await ctx.facade.resolve_pk(username)
        return await fn(ctx, pk)

    return wrapper


# ---------------------------------------------------------------------------
# Pipeline-friendly I/O helpers
# ---------------------------------------------------------------------------


def resolve_export_dest(dest_str: str | None) -> Path | IO[bytes] | None:
    """Translate the value of `--json DEST` / `--csv DEST` into something the
    exporter can consume.

    `None`     → flag was not set.
    `""`       → flag was set without a value (use facade default location).
    `"-"`      → write to `sys.stdout.buffer` (pipeline mode).
    other      → `Path(dest_str)` — caller passes it through to exporter.
    """
    if dest_str is None or dest_str == "":
        return None
    if dest_str == "-":
        return sys.stdout.buffer
    return Path(dest_str)


async def download_or_print_url(
    facade: OsintFacade,
    url: str,
    dest: Path,
    *,
    taken_at: float | int | None = None,
    no_download: bool = False,
) -> Path | None:
    """If `--no-download` is active, print `url` and return `None`.

    Otherwise stream the URL into `dest` via the facade's CDN streamer
    (which applies all the host / MIME / size / atomic-write protections).
    Used by every media command so the `--no-download` path stays in
    one place.
    """
    if no_download:
        print(url)
        return None
    return await facade._stream(url, dest, taken_at=taken_at)


__all__ = [
    "COMMANDS",
    "ArgsBuilder",
    "CommandContext",
    "CommandFn",
    "CommandSpec",
    "CommandUsageError",
    "Session",
    "build_parser_for",
    "command",
    "dispatch",
    "download_or_print_url",
    "global_parser",
    "parse_command_line",
    "resolve_export_dest",
    "validate_global_flags",
    "with_pk",
    "with_target",
]
