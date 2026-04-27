"""Tests for `insto.commands.batch`: /batch fan-out command.

Covers:

- Concurrency lid is honoured (counter never exceeds the requested cap).
- Confirmation prompt fires on > CONFIRM_THRESHOLD pending targets.
- Resume state is read on restart and `--restart` clears it.
- `QuotaExhausted` halts the batch cleanly, saves progress, returns success.
- Dedup of input lines emits a warning.
- Stdin mode requires `--yes`; empty stdin / file errors clearly.
- CRLF input is stripped, whitespace-only lines are skipped with a warning.

Tests pin `START_DELAY = 0.0` via monkeypatch to keep the suite fast — the
stagger gate behaviour is exercised separately in
`test_stagger_gate_enforces_minimum_gap`.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

# Importing the package registers /batch and the rest of the registry.
import insto.commands  # noqa: F401  (side-effect import)
from insto.commands import batch as batch_module
from insto.commands._base import (
    COMMANDS,
    CommandContext,
    CommandSpec,
    CommandUsageError,
    Session,
    command,
    dispatch,
)
from insto.config import Config
from insto.exceptions import QuotaExhausted, Transient
from insto.models import Profile
from insto.service.facade import OsintFacade
from insto.service.history import HistoryStore
from tests.fakes import FakeBackend

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_stagger(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable the inter-worker stagger so the suite runs quickly."""
    monkeypatch.setattr(batch_module, "START_DELAY", 0.0)


@pytest.fixture
def history(tmp_path: Path) -> Generator[HistoryStore, None, None]:
    s = HistoryStore(tmp_path / "store.db")
    yield s
    s.close()


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(output_dir=tmp_path / "output", db_path=tmp_path / "store.db")


@pytest.fixture
def backend() -> FakeBackend:
    profiles = {str(i): Profile(pk=str(i), username=f"u{i}", access="public") for i in range(1, 60)}
    return FakeBackend(profiles=profiles)


@pytest.fixture
def facade(backend: FakeBackend, history: HistoryStore, config: Config) -> OsintFacade:
    return OsintFacade(backend=backend, history=history, config=config)


@pytest.fixture
def session() -> Session:
    return Session()


@pytest.fixture
def isolated_registry() -> Generator[dict[str, CommandSpec], None, None]:
    """Snapshot/restore the registry so test commands don't leak."""
    snapshot = COMMANDS.copy()
    yield COMMANDS
    COMMANDS.clear()
    COMMANDS.update(snapshot)


# ---------------------------------------------------------------------------
# Pure-function helpers
# ---------------------------------------------------------------------------


def test_parse_target_lines_strips_crlf_and_at() -> None:
    text = "alice\r\n@bob\r\n  carol  \n"
    targets, blank = batch_module._parse_target_lines(text)
    assert targets == ["alice", "bob", "carol"]
    assert blank == 0


def test_parse_target_lines_counts_blanks() -> None:
    text = "alice\n\n\t\n@bob\n   \n"
    targets, blank = batch_module._parse_target_lines(text)
    assert targets == ["alice", "bob"]
    assert blank == 3


def test_dedup_preserves_order_and_counts() -> None:
    out, dups = batch_module._dedup(["a", "b", "a", "c", "b"])
    assert out == ["a", "b", "c"]
    assert dups == 2


def test_input_sha_is_stable_under_reorder() -> None:
    a = batch_module._input_sha(["alice", "bob", "carol"])
    b = batch_module._input_sha(["carol", "alice", "bob"])
    assert a == b
    # ...but changes when the set changes.
    c = batch_module._input_sha(["alice", "bob"])
    assert c != a


def test_resume_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / ".batch-test.jsonl"
    batch_module._append_resume(path, "alice")
    batch_module._append_resume(path, "bob")
    done = batch_module._read_resume(path)
    assert done == {"alice", "bob"}


def test_read_resume_skips_garbage_lines(tmp_path: Path) -> None:
    path = tmp_path / "x.jsonl"
    path.write_text(
        '{"target": "alice", "ts": 1}\n'
        "not-json\n"
        "\n"
        '{"no_target": true}\n'
        '{"target": "bob", "ts": 2}\n',
        encoding="utf-8",
    )
    assert batch_module._read_resume(path) == {"alice", "bob"}


# ---------------------------------------------------------------------------
# Stagger gate
# ---------------------------------------------------------------------------


async def test_stagger_gate_enforces_minimum_gap() -> None:
    gate = batch_module._StaggerGate(base_delay=0.05, jitter_fraction=0.0)
    times: list[float] = []

    async def hit() -> None:
        await gate.wait()
        times.append(time.monotonic())

    await asyncio.gather(*(hit() for _ in range(3)))
    times.sort()
    # First entry has no gap; second and third should be ≥ ~0.05s apart.
    assert times[1] - times[0] >= 0.04
    assert times[2] - times[1] >= 0.04


async def test_stagger_gate_zero_delay_is_noop() -> None:
    gate = batch_module._StaggerGate(base_delay=0.0, jitter_fraction=0.0)
    start = time.monotonic()
    await asyncio.gather(*(gate.wait() for _ in range(5)))
    assert time.monotonic() - start < 0.05


# ---------------------------------------------------------------------------
# Helper: register a counted async sub-command via the isolated registry
# ---------------------------------------------------------------------------


def _register_counter_cmd(
    isolated: dict[str, CommandSpec],
    *,
    name: str = "noop",
    delay: float = 0.05,
    fail_on: set[str] | None = None,
    quota_exhausted_on: set[str] | None = None,
    record_in: list[str] | None = None,
    counters: dict[str, int] | None = None,
) -> None:
    """Register a fake command that respects the active session target.

    `counters` (dict with `current` / `max`) lets tests observe the
    in-flight worker count to assert that the semaphore is honoured.
    """
    fail = fail_on or set()
    quota = quota_exhausted_on or set()
    seen = record_in if record_in is not None else []
    state = counters if counters is not None else {"current": 0, "max": 0}
    state.setdefault("current", 0)
    state.setdefault("max", 0)

    @command(name, "test fake")
    async def fn(ctx: CommandContext) -> str:
        target = ctx.session.target or ""
        state["current"] += 1
        state["max"] = max(state["max"], state["current"])
        try:
            await asyncio.sleep(delay)
            if target in quota:
                raise QuotaExhausted(f"out of quota at {target}")
            if target in fail:
                raise Transient(f"flaky at {target}")
            seen.append(target)
            return target
        finally:
            state["current"] -= 1


def _file_with_targets(tmp_path: Path, targets: list[str]) -> Path:
    p = tmp_path / "targets.txt"
    p.write_text("\n".join(targets) + "\n", encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# End-to-end: dispatch /batch
# ---------------------------------------------------------------------------


async def test_batch_runs_command_for_each_target(
    isolated_registry: dict[str, CommandSpec],
    facade: OsintFacade,
    session: Session,
    tmp_path: Path,
) -> None:
    seen: list[str] = []
    _register_counter_cmd(isolated_registry, record_in=seen, delay=0.0)
    targets_file = _file_with_targets(tmp_path, ["u1", "u2", "u3"])

    result = await dispatch(
        f"/batch --concurrency 2 {targets_file} noop",
        facade=facade,
        session=session,
    )
    assert isinstance(result, dict)
    assert sorted(seen) == ["u1", "u2", "u3"]
    assert sorted(result["completed"]) == ["u1", "u2", "u3"]
    assert result["failed"] == []
    assert result["quota_exhausted"] is False


async def test_batch_concurrency_is_capped(
    isolated_registry: dict[str, CommandSpec],
    facade: OsintFacade,
    session: Session,
    tmp_path: Path,
) -> None:
    counters: dict[str, int] = {"current": 0, "max": 0}
    _register_counter_cmd(isolated_registry, counters=counters, delay=0.05)
    targets_file = _file_with_targets(tmp_path, [f"u{i}" for i in range(1, 13)])

    await dispatch(
        f"/batch --concurrency 3 {targets_file} noop",
        facade=facade,
        session=session,
    )
    assert counters["max"] <= 3
    assert counters["max"] >= 2  # would be 3 but allow 2 for slow CI


async def test_batch_clamps_concurrency_to_ceiling(
    isolated_registry: dict[str, CommandSpec],
    facade: OsintFacade,
    session: Session,
    tmp_path: Path,
) -> None:
    counters: dict[str, int] = {"current": 0, "max": 0}
    _register_counter_cmd(isolated_registry, counters=counters, delay=0.02)
    targets_file = _file_with_targets(tmp_path, [f"u{i}" for i in range(1, 30)])

    await dispatch(
        f"/batch --concurrency 50 --yes {targets_file} noop",
        facade=facade,
        session=session,
    )
    assert counters["max"] <= batch_module.MAX_CONCURRENCY


async def test_batch_dedups_with_warning(
    isolated_registry: dict[str, CommandSpec],
    facade: OsintFacade,
    session: Session,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: list[str] = []
    _register_counter_cmd(isolated_registry, record_in=seen, delay=0.0)
    targets_file = _file_with_targets(tmp_path, ["alice", "bob", "alice", "alice"])

    from rich.console import Console

    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False)
    spec, args = _parse_with_console("/batch --concurrency 1 " + str(targets_file) + " noop")
    ctx = CommandContext(facade=facade, args=args, session=session, console=console)
    result = await spec.fn(ctx)

    assert sorted(seen) == ["alice", "bob"]
    assert "removed 2 duplicate" in buf.getvalue()
    assert sorted(result["completed"]) == ["alice", "bob"]


async def test_batch_warns_on_blank_lines(
    isolated_registry: dict[str, CommandSpec],
    facade: OsintFacade,
    session: Session,
    tmp_path: Path,
) -> None:
    _register_counter_cmd(isolated_registry, delay=0.0)
    p = tmp_path / "t.txt"
    p.write_text("alice\n\n   \n@bob\r\n", encoding="utf-8")

    from rich.console import Console

    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False)
    spec, args = _parse_with_console(f"/batch --concurrency 1 {p} noop")
    ctx = CommandContext(facade=facade, args=args, session=session, console=console)
    result = await spec.fn(ctx)

    assert sorted(result["completed"]) == ["alice", "bob"]
    assert "skipped 2 empty" in buf.getvalue()


# ---------------------------------------------------------------------------
# Resume
# ---------------------------------------------------------------------------


async def test_batch_resume_skips_already_done(
    isolated_registry: dict[str, CommandSpec],
    facade: OsintFacade,
    session: Session,
    tmp_path: Path,
    config: Config,
) -> None:
    seen_first: list[str] = []
    _register_counter_cmd(isolated_registry, record_in=seen_first, delay=0.0)
    targets_file = _file_with_targets(tmp_path, ["alice", "bob", "carol"])

    await dispatch(
        f"/batch --concurrency 1 {targets_file} noop",
        facade=facade,
        session=session,
    )
    assert sorted(seen_first) == ["alice", "bob", "carol"]

    # Replace the noop with a fresh recorder for the second run.
    COMMANDS.pop("noop")
    seen_second: list[str] = []
    _register_counter_cmd(isolated_registry, record_in=seen_second, delay=0.0)

    result = await dispatch(
        f"/batch --concurrency 1 {targets_file} noop",
        facade=facade,
        session=session,
    )
    # Nothing actually runs on resume — all already done.
    assert seen_second == []
    assert result["skipped_done"] == 3


async def test_batch_restart_clears_resume(
    isolated_registry: dict[str, CommandSpec],
    facade: OsintFacade,
    session: Session,
    tmp_path: Path,
) -> None:
    seen: list[str] = []
    _register_counter_cmd(isolated_registry, record_in=seen, delay=0.0)
    targets_file = _file_with_targets(tmp_path, ["alice", "bob"])

    await dispatch(
        f"/batch --concurrency 1 {targets_file} noop",
        facade=facade,
        session=session,
    )
    assert sorted(seen) == ["alice", "bob"]

    COMMANDS.pop("noop")
    seen2: list[str] = []
    _register_counter_cmd(isolated_registry, record_in=seen2, delay=0.0)

    await dispatch(
        f"/batch --concurrency 1 --restart {targets_file} noop",
        facade=facade,
        session=session,
    )
    assert sorted(seen2) == ["alice", "bob"]


# ---------------------------------------------------------------------------
# Confirmation
# ---------------------------------------------------------------------------


async def test_batch_confirmation_required_above_threshold(
    isolated_registry: dict[str, CommandSpec],
    facade: OsintFacade,
    session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []
    _register_counter_cmd(isolated_registry, record_in=seen, delay=0.0)
    targets_file = _file_with_targets(
        tmp_path, [f"u{i}" for i in range(1, batch_module.CONFIRM_THRESHOLD + 5)]
    )

    declined: list[str] = []

    async def fake_confirm(ctx: CommandContext, msg: str) -> bool:
        declined.append(msg)
        return False

    monkeypatch.setattr(batch_module, "_confirm", fake_confirm)
    result = await dispatch(
        f"/batch --concurrency 2 {targets_file} noop",
        facade=facade,
        session=session,
    )
    assert declined  # confirmation was prompted
    assert seen == []
    assert result["completed"] == []
    assert len(result["remaining"]) == batch_module.CONFIRM_THRESHOLD + 4


async def test_batch_yes_flag_skips_confirmation(
    isolated_registry: dict[str, CommandSpec],
    facade: OsintFacade,
    session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []
    _register_counter_cmd(isolated_registry, record_in=seen, delay=0.0)
    targets_file = _file_with_targets(
        tmp_path, [f"u{i}" for i in range(1, batch_module.CONFIRM_THRESHOLD + 3)]
    )

    async def boom_confirm(ctx: CommandContext, msg: str) -> bool:
        raise AssertionError("should not prompt with --yes")

    monkeypatch.setattr(batch_module, "_confirm", boom_confirm)
    await dispatch(
        f"/batch --concurrency 2 --yes {targets_file} noop",
        facade=facade,
        session=session,
    )
    assert len(seen) == batch_module.CONFIRM_THRESHOLD + 2


# ---------------------------------------------------------------------------
# QuotaExhausted: clean exit
# ---------------------------------------------------------------------------


async def test_batch_quota_exhausted_stops_cleanly(
    isolated_registry: dict[str, CommandSpec],
    facade: OsintFacade,
    session: Session,
    tmp_path: Path,
    config: Config,
) -> None:
    seen: list[str] = []
    _register_counter_cmd(
        isolated_registry,
        record_in=seen,
        delay=0.01,
        quota_exhausted_on={"u3"},
    )
    targets_file = _file_with_targets(tmp_path, [f"u{i}" for i in range(1, 8)])

    result = await dispatch(
        f"/batch --concurrency 1 {targets_file} noop",
        facade=facade,
        session=session,
    )
    assert result["quota_exhausted"] is True
    # u3 raised quota; everything before it should have completed.
    assert "u1" in seen and "u2" in seen
    assert "u3" not in seen
    # Resume file should record u1, u2 (not u3 — it failed).
    sha = batch_module._input_sha([f"u{i}" for i in range(1, 8)])
    resume_path = config.output_dir / f".batch-{sha}.jsonl"
    assert resume_path.exists()
    done = batch_module._read_resume(resume_path)
    assert "u1" in done and "u2" in done
    assert "u3" not in done


async def test_batch_per_target_failure_is_logged_not_raised(
    isolated_registry: dict[str, CommandSpec],
    facade: OsintFacade,
    session: Session,
    tmp_path: Path,
) -> None:
    seen: list[str] = []
    _register_counter_cmd(
        isolated_registry,
        record_in=seen,
        delay=0.0,
        fail_on={"u2"},
    )
    targets_file = _file_with_targets(tmp_path, ["u1", "u2", "u3"])
    result = await dispatch(
        f"/batch --concurrency 1 {targets_file} noop",
        facade=facade,
        session=session,
    )
    failed = result["failed"]
    assert isinstance(failed, list)
    assert len(failed) == 1 and failed[0][0] == "u2"
    assert sorted(seen) == ["u1", "u3"]


# ---------------------------------------------------------------------------
# stdin mode
# ---------------------------------------------------------------------------


async def test_batch_stdin_requires_yes(
    isolated_registry: dict[str, CommandSpec],
    facade: OsintFacade,
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _register_counter_cmd(isolated_registry, delay=0.0)
    monkeypatch.setattr("sys.stdin", io.StringIO("alice\nbob\n"))
    with pytest.raises(CommandUsageError, match="requires --yes"):
        await dispatch("/batch - noop", facade=facade, session=session)


async def test_batch_stdin_with_yes_runs(
    isolated_registry: dict[str, CommandSpec],
    facade: OsintFacade,
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []
    _register_counter_cmd(isolated_registry, record_in=seen, delay=0.0)
    monkeypatch.setattr("sys.stdin", io.StringIO("alice\nbob\n"))
    await dispatch("/batch --yes - noop", facade=facade, session=session)
    assert sorted(seen) == ["alice", "bob"]


async def test_batch_stdin_empty_errors(
    isolated_registry: dict[str, CommandSpec],
    facade: OsintFacade,
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _register_counter_cmd(isolated_registry, delay=0.0)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    with pytest.raises(CommandUsageError, match="no targets provided on stdin"):
        await dispatch("/batch --yes - noop", facade=facade, session=session)


async def test_batch_file_not_found_errors(
    isolated_registry: dict[str, CommandSpec],
    facade: OsintFacade,
    session: Session,
    tmp_path: Path,
) -> None:
    _register_counter_cmd(isolated_registry, delay=0.0)
    missing = tmp_path / "nope.txt"
    with pytest.raises(CommandUsageError, match="input file not found"):
        await dispatch(f"/batch {missing} noop", facade=facade, session=session)


async def test_batch_empty_file_errors(
    isolated_registry: dict[str, CommandSpec],
    facade: OsintFacade,
    session: Session,
    tmp_path: Path,
) -> None:
    _register_counter_cmd(isolated_registry, delay=0.0)
    p = tmp_path / "empty.txt"
    p.write_text("", encoding="utf-8")
    with pytest.raises(CommandUsageError, match="no targets found"):
        await dispatch(f"/batch {p} noop", facade=facade, session=session)


# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------


async def test_batch_missing_cmd_errors(
    isolated_registry: dict[str, CommandSpec],
    facade: OsintFacade,
    session: Session,
    tmp_path: Path,
) -> None:
    p = _file_with_targets(tmp_path, ["alice"])
    with pytest.raises(CommandUsageError, match="usage:"):
        await dispatch(f"/batch {p}", facade=facade, session=session)


async def test_batch_invalid_subcommand_errors(
    isolated_registry: dict[str, CommandSpec],
    facade: OsintFacade,
    session: Session,
    tmp_path: Path,
) -> None:
    p = _file_with_targets(tmp_path, ["alice"])
    with pytest.raises(CommandUsageError, match="invalid sub-command"):
        await dispatch(f"/batch {p} not-a-real-command", facade=facade, session=session)


# ---------------------------------------------------------------------------
# Worker-failure path redacts secrets in the captured exception message
# ---------------------------------------------------------------------------


async def test_batch_worker_failure_message_is_redacted(
    isolated_registry: dict[str, CommandSpec],
    facade: OsintFacade,
    session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-quota exception's message must flow through `redact_secrets`."""
    monkeypatch.setenv("HIKERAPI_TOKEN", "tok-very-secret-1234")

    @command("leaky", "test fake that raises with a secret in the message")
    async def fn(ctx: CommandContext) -> None:
        raise Transient(
            "fetch failed: https://cdn.example.com/x.jpg?signature=SECRET_SIG_42 "
            "(token leaked: tok-very-secret-1234)"
        )

    targets_file = _file_with_targets(tmp_path, ["alice"])
    result = await dispatch(
        f"/batch --concurrency 1 {targets_file} leaky",
        facade=facade,
        session=session,
    )
    assert isinstance(result, dict)
    assert len(result["failed"]) == 1
    _, msg = result["failed"][0]
    assert "tok-very-secret-1234" not in msg
    assert "SECRET_SIG_42" not in msg
    assert "***" in msg
    assert "signature=***" in msg


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _parse_with_console(line: str) -> tuple[CommandSpec, argparse.Namespace]:
    """Like `parse_command_line` but exposed for tests that want a console."""
    from insto.commands._base import parse_command_line

    return parse_command_line(line)


# ---------------------------------------------------------------------------
# Resume file writes a parseable JSON record
# ---------------------------------------------------------------------------


async def test_batch_resume_file_format_is_jsonl(
    isolated_registry: dict[str, CommandSpec],
    facade: OsintFacade,
    session: Session,
    tmp_path: Path,
    config: Config,
) -> None:
    _register_counter_cmd(isolated_registry, delay=0.0)
    targets_file = _file_with_targets(tmp_path, ["alice"])
    await dispatch(
        f"/batch --concurrency 1 {targets_file} noop",
        facade=facade,
        session=session,
    )
    sha = batch_module._input_sha(["alice"])
    resume_path = config.output_dir / f".batch-{sha}.jsonl"
    rec: dict[str, Any] = json.loads(resume_path.read_text().strip())
    assert rec["target"] == "alice"
    assert isinstance(rec["ts"], int)
