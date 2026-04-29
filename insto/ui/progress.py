"""Progress UI for command-layer waits — spinner + tqdm.

Two complementary pieces:

- ``spinner(label)`` — async context manager that paints a one-line
  Braille spinner on stderr while the body runs. Used by every
  command via ``dispatch`` so commands that don't show a tqdm bar
  (``/info``, ``/resolve``, ``/recommended``, the resolve+user_posts
  setup of ``/fans``) at least show movement during the silent
  HTTP wait — pipx-style.

- ``track(iterable, ...)`` — wraps an iterable in a tqdm bar. Used
  by ``/fans``, ``/wliked``, ``/wcommented`` for the per-post loop.
  Calling ``track()`` automatically stops the active spinner (tqdm
  takes over the same stderr line; running both at once would
  scribble over each other).

Both honour ``disable()`` (CLI's ``--no-progress``) and auto-suppress
on non-TTY (CI logs, piped invocations) without configuration.
stdout (JSON / CSV / table output) stays clean — every animation is
on stderr.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from collections.abc import AsyncIterator, Iterable, Iterator
from typing import TypeVar

T = TypeVar("T")

_DISABLED: bool = False
_SPINNER_TASK: asyncio.Task[None] | None = None
_SPINNER_STOP: asyncio.Event | None = None

# Braille spinner frames — same set tqdm uses internally; reads as a
# rotating wheel on monospaced terminal fonts.
_FRAMES: str = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_FRAME_INTERVAL_S: float = 0.08


def disable() -> None:
    """Suppress every subsequent spinner / track bar in this process.

    Called by the CLI when ``--no-progress`` is set. Idempotent —
    re-enabling is intentionally not exposed.
    """
    global _DISABLED
    _DISABLED = True


@contextlib.asynccontextmanager
async def spinner(label: str) -> AsyncIterator[None]:
    """Show a Braille spinner on stderr until the with-block exits.

    No-op when ``--no-progress`` is set, when stderr is not a TTY,
    or when an outer ``spinner(...)`` is already active (nested
    dispatch like ``/batch`` calling sub-commands keeps a single
    outer bar).

    The spinner self-erases when ``track(...)`` is called inside
    the body — tqdm needs the same stderr line.
    """
    global _SPINNER_TASK, _SPINNER_STOP
    if _DISABLED or not sys.stderr.isatty() or _SPINNER_TASK is not None:
        yield
        return

    stop = asyncio.Event()
    _SPINNER_STOP = stop
    _SPINNER_TASK = asyncio.create_task(_spinner_loop(label, stop))
    try:
        yield
    finally:
        _stop_spinner()


async def _spinner_loop(label: str, stop: asyncio.Event) -> None:
    """Render one frame every ``_FRAME_INTERVAL_S`` until ``stop`` fires."""
    i = 0
    try:
        while not stop.is_set():
            sys.stderr.write(f"\r{_FRAMES[i % len(_FRAMES)]} {label}...")
            sys.stderr.flush()
            # `wait_for` raises TimeoutError on the cycle interval —
            # suppress it (that's the whole point of the timeout) and
            # let the next loop iteration redraw the next frame.
            with contextlib.suppress(asyncio.TimeoutError, TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=_FRAME_INTERVAL_S)
            i += 1
    finally:
        # ANSI: \r — cursor to col 0; \033[K — erase to end of line.
        sys.stderr.write("\r\033[K")
        sys.stderr.flush()


def _stop_spinner() -> None:
    """Idempotent: signal the loop to exit, drop the global handles."""
    global _SPINNER_TASK, _SPINNER_STOP
    if _SPINNER_STOP is not None and not _SPINNER_STOP.is_set():
        _SPINNER_STOP.set()
    _SPINNER_TASK = None
    _SPINNER_STOP = None


def track(
    iterable: Iterable[T],
    *,
    desc: str,
    total: int | None = None,
) -> Iterator[T]:
    """Wrap ``iterable`` with a tqdm progress bar.

    Stops the active spinner (if any) before starting the bar — tqdm
    takes over the stderr line. No-op (returns the underlying
    iterator) when ``--no-progress`` has been set or the calling
    context is non-TTY. Bar leaves stderr cleanly on completion
    (``leave=False``) so it doesn't pile up after several commands
    in the same REPL session.
    """
    _stop_spinner()
    from tqdm import tqdm  # lazy: cheap startup when not iterating

    return tqdm(  # type: ignore[no-any-return]
        iterable,
        desc=desc,
        total=total,
        disable=_DISABLED,
        leave=False,
    )
