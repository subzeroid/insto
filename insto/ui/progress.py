"""Thin wrapper around `tqdm` for command-layer progress bars.

`/fans`, `/wliked`, `/wcommented`, and `/dossier` all loop over posts
issuing one (or two) backend calls per iteration. With a 50-post
window that's 50-100 silent seconds of HTTP — long enough that
operators reach for Ctrl-C wondering if it hung. A tqdm bar makes the
progress visible and the ETA actionable.

Why tqdm and not `rich.progress`:

- tqdm auto-disables on non-TTY (CI logs, piped invocations) without
  configuration. `rich.progress` requires explicit `disable=` checks.
- tqdm writes to stderr; the command's stdout (JSON / CSV) stays
  clean. `rich.progress` shares the console and would interleave.
- One small dep, well-known UI; no risk of conflicting with the REPL's
  own `rich.Console`.

The module exposes a single ``track(...)`` helper plus the global
``disable()`` toggle that the CLI flips on when ``--no-progress`` is
set. Tests that don't pass through the CLI never enable it, so output
stays clean by default.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import TypeVar

T = TypeVar("T")

_DISABLED: bool = False


def disable() -> None:
    """Suppress every subsequent ``track(...)`` bar in this process.

    Called by the CLI when ``--no-progress`` is set. Idempotent —
    re-enabling is intentionally not exposed; the flag is per-
    invocation, and a second call is always a no-op.
    """
    global _DISABLED
    _DISABLED = True


def track(
    iterable: Iterable[T],
    *,
    desc: str,
    total: int | None = None,
) -> Iterator[T]:
    """Wrap ``iterable`` with a tqdm progress bar.

    No-op (returns the underlying iterator unchanged) when ``--no-progress``
    has been set or the calling context is non-TTY. Bar leaves stderr
    cleanly on completion (``leave=False``) so it doesn't pile up after
    several commands in the same REPL session.

    Parameters
    ----------
    iterable:
        Source iterable; tqdm will tick once per item yielded.
    desc:
        Short label shown to the left of the bar (e.g.
        ``"fetching likers"``). Keep it under ~30 chars; longer
        descriptions get truncated by narrow terminals.
    total:
        Length hint for the bar. Pass when ``iterable`` is not a
        list / tuple — tqdm cannot compute the percentage from a
        generator otherwise.
    """
    from tqdm import tqdm  # lazy: cheap startup when not iterating

    return tqdm(  # type: ignore[no-any-return]
        iterable,
        desc=desc,
        total=total,
        disable=_DISABLED,
        leave=False,
    )
