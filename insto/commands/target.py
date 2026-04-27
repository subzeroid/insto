"""Target group: `/target`, `/current`, `/clear`.

`/target <user>` sets the active session target after pre-resolving its pk
(so a typo fails fast instead of poisoning the next command). `/current`
reports the active target. `/clear` drops it and also evicts the cached
pk from the facade.
"""

from __future__ import annotations

import argparse

from insto.commands._base import (
    CommandContext,
    CommandUsageError,
    command,
)


def _add_target_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "target",
        nargs="?",
        help="Instagram username (with or without leading @)",
    )


@command(
    "target",
    "Set the active session target (pre-resolves to validate username)",
    add_args=_add_target_arg,
)
async def target_cmd(ctx: CommandContext) -> str:
    raw = getattr(ctx.args, "target", None)
    if not raw:
        raise CommandUsageError("usage: /target <username>")
    username = str(raw).lstrip("@").strip()
    if not username:
        raise CommandUsageError("usage: /target <username>")
    # Resolve once so a typo raises immediately instead of breaking the
    # next command. The pk is cached on the facade for the rest of the session.
    await ctx.facade.resolve_pk(username)
    ctx.session.set_target(username)
    return username


@command("current", "Show the active session target")
async def current_cmd(ctx: CommandContext) -> str | None:
    return ctx.session.target


@command("clear", "Clear the active session target")
async def clear_cmd(ctx: CommandContext) -> None:
    name = ctx.session.target
    ctx.session.clear()
    ctx.facade.clear_target_cache(name)
    return None


__all__ = ["clear_cmd", "current_cmd", "target_cmd"]
