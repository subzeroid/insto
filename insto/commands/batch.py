"""`/batch <file> <cmd>` — fan out a command across many targets.

The command reads target usernames from a file (or `stdin` when `<file>` is
`-`), de-duplicates, optionally confirms with the user, then dispatches
`<cmd>` once per target through the same registry the REPL uses. Each
worker runs with its own `Session(target=...)` so the per-target state never
crosses workers.

Concurrency / pacing:

- An `asyncio.Semaphore` caps in-flight workers (`--concurrency N`,
  default 3, hard ceiling `MAX_CONCURRENCY=10`).
- A serialised stagger gate enforces ~1s ± 25 % jitter between worker
  starts so we do not slam the backend with N concurrent requests on the
  first tick.

Resume:

- A SHA256 of the canonical (sorted, deduplicated) target list keys a
  resume file at `~/.insto/batch-resume/<sha>.jsonl`. After each successful
  target the worker appends one JSON line; a follow-up `/batch` invocation
  with the same input skips already-done targets. `--restart` deletes the
  resume file before starting. Stored under the config dir (not the output
  dir) so `/purge cache` does not silently delete in-progress resume state.

Graceful failure:

- `QuotaExhausted` raised by any worker sets a shared event. Already-running
  workers finish; pending ones short-circuit. The command returns 0 with a
  message; resume state on disk lets the next run pick up where we left off.
- Any other exception per target is captured, logged, and counted in
  `failed`; the batch keeps going.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
import shlex
import sys
import time
from pathlib import Path

from insto._redact import redact_secrets
from insto.commands._base import (
    CommandContext,
    CommandUsageError,
    Session,
    command,
    dispatch,
    parse_command_line,
)
from insto.config import config_dir
from insto.exceptions import QuotaExhausted

MAX_CONCURRENCY = 10
DEFAULT_CONCURRENCY = 3
CONFIRM_THRESHOLD = 25
JITTER_FRACTION = 0.25
START_DELAY = 1.0


def _add_batch_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "file",
        help="path to a file with one target per line, or '-' to read from stdin",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        metavar="N",
        help=(
            f"max concurrent workers (default {DEFAULT_CONCURRENCY}, "
            f"hard ceiling {MAX_CONCURRENCY})"
        ),
    )
    parser.add_argument(
        "--restart",
        action="store_true",
        help="discard prior resume state and start from scratch",
    )
    parser.add_argument(
        "cmd",
        nargs=argparse.REMAINDER,
        help="sub-command to run for each target (e.g. info, followers --limit 50)",
    )


# ---------------------------------------------------------------------------
# Input parsing
# ---------------------------------------------------------------------------


def _parse_target_lines(text: str) -> tuple[list[str], int]:
    """Strip + clean lines, return `(targets, blank_skipped)`.

    Splits on universal newlines (so CRLF is handled), strips whitespace and
    leading `@`, and drops empty lines (counted separately so the caller can
    surface a warning).
    """
    raw_lines = text.splitlines()
    targets: list[str] = []
    blank = 0
    for line in raw_lines:
        cleaned = line.strip().lstrip("@").strip()
        if not cleaned:
            blank += 1
            continue
        targets.append(cleaned)
    return targets, blank


def _read_targets(file_arg: str, *, yes: bool) -> tuple[list[str], int]:
    """Read targets from `file_arg`. `-` reads from `sys.stdin` (requires `--yes`)."""
    if file_arg == "-":
        if not yes:
            raise CommandUsageError(
                "/batch reading from stdin requires --yes "
                "(stdin is consumed by input data, no interactive confirm possible)"
            )
        text = sys.stdin.read()
    else:
        path = Path(file_arg)
        if not path.exists():
            raise CommandUsageError(f"input file not found: {path}")
        text = path.read_text(encoding="utf-8")
    return _parse_target_lines(text)


def _dedup(targets: list[str]) -> tuple[list[str], int]:
    """Return `(deduped_in_input_order, dup_count)`."""
    seen: set[str] = set()
    out: list[str] = []
    dups = 0
    for t in targets:
        if t in seen:
            dups += 1
            continue
        seen.add(t)
        out.append(t)
    return out, dups


def _input_sha(targets: list[str]) -> str:
    """Stable short hash of the (sorted, deduplicated) target list.

    Sorting before hashing means that re-ordering the input file does not
    invalidate the resume state — the workload is the same set of targets.
    """
    canonical = "\n".join(sorted(targets)).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Resume state
# ---------------------------------------------------------------------------


def _resume_path(sha: str) -> Path:
    """Resume jsonl path under `~/.insto/batch-resume/`.

    Lives under the config dir (not the output dir) so `/purge cache` —
    which recursively wipes the output dir — never silently destroys
    in-progress batch state. The directory is created with mode 0700
    inherited from `~/.insto`.
    """
    base = config_dir() / "batch-resume"
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{sha}.jsonl"


def _read_resume(path: Path) -> set[str]:
    if not path.exists():
        return set()
    done: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        target = rec.get("target")
        if isinstance(target, str):
            done.add(target)
    return done


def _append_resume(path: Path, target: str) -> None:
    record = {"target": target, "ts": int(time.time())}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


# ---------------------------------------------------------------------------
# Confirmation
# ---------------------------------------------------------------------------


async def _confirm(ctx: CommandContext, message: str) -> bool:
    """Interactive y/N prompt. Caller must short-circuit on `--yes` first."""
    ctx.print(message + " [y/N]")
    answer = await asyncio.to_thread(input, "")
    return answer.strip().lower() in {"y", "yes"}


# ---------------------------------------------------------------------------
# Stagger gate
# ---------------------------------------------------------------------------


class _StaggerGate:
    """Serialised gate that enforces a minimum delay between worker starts.

    `wait()` blocks until at least `START_DELAY * (1 ± JITTER_FRACTION)`
    seconds have elapsed since the previous successful `wait()`. The lock
    serialises the wait — without it, two workers calling `wait()`
    simultaneously would both observe the same `last` and drop their delays
    to zero.
    """

    def __init__(self, base_delay: float, jitter_fraction: float) -> None:
        self._base = base_delay
        self._jitter = jitter_fraction
        self._last: float = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        if self._base <= 0.0:
            return
        async with self._lock:
            now = time.monotonic()
            jitter = self._base * self._jitter
            target_gap = self._base + random.uniform(-jitter, jitter)
            if self._last > 0.0:
                elapsed = now - self._last
                remaining = target_gap - elapsed
                if remaining > 0.0:
                    await asyncio.sleep(remaining)
            self._last = time.monotonic()


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


@command(
    "batch",
    "Run a sub-command for each target listed in <file> (or stdin)",
    add_args=_add_batch_args,
)
async def batch_cmd(ctx: CommandContext) -> dict[str, object]:
    file_arg = ctx.args.file
    cmd_tokens: list[str] = list(ctx.args.cmd or [])
    if not cmd_tokens:
        raise CommandUsageError(
            "usage: /batch [--concurrency N] [--restart] <file> <cmd> [args...]"
        )

    cmd_line = shlex.join(cmd_tokens)
    try:
        parse_command_line(cmd_line)
    except CommandUsageError as exc:
        raise CommandUsageError(f"invalid sub-command: {exc}") from exc

    raw, blank_skipped = _read_targets(file_arg, yes=ctx.yes)
    if not raw:
        if file_arg == "-":
            raise CommandUsageError("no targets provided on stdin")
        raise CommandUsageError(f"no targets found in {file_arg}")
    if blank_skipped:
        ctx.print(f"warning: skipped {blank_skipped} empty/whitespace-only line(s)")

    targets, dup_count = _dedup(raw)
    if dup_count:
        ctx.print(f"warning: removed {dup_count} duplicate target(s)")

    requested = int(ctx.args.concurrency)
    concurrency = max(1, min(requested, MAX_CONCURRENCY))
    if requested > MAX_CONCURRENCY:
        ctx.print(
            f"warning: --concurrency {requested} exceeds ceiling {MAX_CONCURRENCY}; "
            f"clamped to {MAX_CONCURRENCY}"
        )

    sha = _input_sha(targets)
    resume_file = _resume_path(sha)
    if ctx.args.restart and resume_file.exists():
        resume_file.unlink()

    done = _read_resume(resume_file)
    pending = [t for t in targets if t not in done]

    if done and pending:
        ctx.print(f"resuming: {len(done)} already done, {len(pending)} remaining (sha={sha})")
    elif done and not pending:
        ctx.print(f"all {len(done)} targets already complete (sha={sha}); nothing to do")
        return {
            "completed": list(done),
            "skipped_done": len(done),
            "failed": [],
            "remaining": [],
            "sha": sha,
            "quota_exhausted": False,
        }

    if len(pending) > CONFIRM_THRESHOLD and not ctx.yes:
        quota = ctx.facade.quota()
        remaining_q = "?" if quota.remaining is None else str(quota.remaining)
        msg = (
            f"about to run /{cmd_tokens[0]} on {len(pending)} target(s); "
            f"quota remaining: {remaining_q}, estimated cost: ~{len(pending)} call(s)"
        )
        if not await _confirm(ctx, msg):
            ctx.print("aborted")
            return {
                "completed": list(done),
                "skipped_done": len(done),
                "failed": [],
                "remaining": pending,
                "sha": sha,
                "quota_exhausted": False,
            }

    sem = asyncio.Semaphore(concurrency)
    gate = _StaggerGate(START_DELAY, JITTER_FRACTION)
    completed: list[str] = []
    failed: list[tuple[str, str]] = []
    quota_hit = asyncio.Event()
    state_lock = asyncio.Lock()

    async def worker(target: str) -> None:
        if quota_hit.is_set():
            return
        async with sem:
            if quota_hit.is_set():
                return
            await gate.wait()
            if quota_hit.is_set():
                return
            try:
                session = Session(target=target)
                await dispatch(cmd_line, facade=ctx.facade, session=session, console=ctx.console)
            except QuotaExhausted as exc:
                quota_hit.set()
                ctx.print(
                    f"quota exhausted at @{target}: {redact_secrets(str(exc))}; "
                    "saving progress and exiting"
                )
                return
            except Exception as exc:
                safe_msg = redact_secrets(str(exc))
                async with state_lock:
                    failed.append((target, safe_msg))
                ctx.print(f"@{target}: failed — {safe_msg}")
                return
            async with state_lock:
                _append_resume(resume_file, target)
                completed.append(target)

    tasks = [asyncio.create_task(worker(t)) for t in pending]
    await asyncio.gather(*tasks, return_exceptions=False)

    finished = set(completed) | {t for t, _ in failed}
    remaining = [t for t in pending if t not in finished]

    summary = {
        "completed": [*done, *completed],
        "skipped_done": len(done),
        "failed": failed,
        "remaining": remaining,
        "sha": sha,
        "quota_exhausted": quota_hit.is_set(),
    }
    ctx.print(
        f"batch done: {len(completed)} ok, {len(failed)} failed, "
        f"{len(remaining)} not run (sha={sha})"
    )
    return summary


__all__ = [
    "CONFIRM_THRESHOLD",
    "DEFAULT_CONCURRENCY",
    "JITTER_FRACTION",
    "MAX_CONCURRENCY",
    "START_DELAY",
    "batch_cmd",
]
