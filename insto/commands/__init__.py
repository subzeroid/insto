"""Command layer for insto.

Each command module declares its functions with `@command(...)` from
`insto.commands._base`. Importing the module registers the commands in
the global `COMMANDS` dict; the CLI and REPL build their dispatch tables
by importing every command module once at startup.

Commands talk only to `OsintFacade` (never directly to backend / analytics
/ history) and never import `_cdn` directly — media commands route through
`download_or_print_url`, which in turn delegates to the facade.
"""

from __future__ import annotations

from insto.commands import target as _target  # noqa: F401  (registers commands)
from insto.commands._base import (
    COMMANDS,
    CommandContext,
    CommandSpec,
    CommandUsageError,
    Session,
    build_parser_for,
    command,
    dispatch,
    download_or_print_url,
    parse_command_line,
    resolve_export_dest,
    with_pk,
    with_target,
)

__all__ = [
    "COMMANDS",
    "CommandContext",
    "CommandSpec",
    "CommandUsageError",
    "Session",
    "build_parser_for",
    "command",
    "dispatch",
    "download_or_print_url",
    "parse_command_line",
    "resolve_export_dest",
    "with_pk",
    "with_target",
]
