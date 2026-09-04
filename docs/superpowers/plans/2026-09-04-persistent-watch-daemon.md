# Persistent Watch Daemon Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish issue #15 with a foreground local watcher whose SQLite-backed registrations survive process exits and restarts.

**Architecture:** SQLite remains the cross-process source of truth, while one `WatchManager` per database owns both the POSIX process lock and all local scheduler tasks. A reusable `WatchDaemon` reconciles persisted rows into those tasks for foreground-daemon and REPL executor roles; one-shot commands only mutate/read SQLite. State callbacks are conditional on an opaque registration id so stale ticks cannot modify a deleted and re-added row.

**Tech Stack:** Python 3.11+, `asyncio`, `sqlite3` WAL, POSIX `fcntl.flock`, `pytest`, `pytest-asyncio`, existing `OsintFacade` and backend adapters.

---

## File map

- Modify `insto/models.py`: immutable persisted watch DTO and typed registration result.
- Modify `insto/service/history.py`: schema-v2 migration, atomic registry operations, conditional state writes, async wrappers.
- Create `insto/service/watch_lock.py`: canonical secure advisory-lock lifecycle.
- Modify `insto/service/watch.py`: recovered state, initial delay, callback/fatal supervision, executor ownership.
- Create `insto/service/watch_daemon.py`: source-of-truth reconciliation, tick construction, startup estimates, lifecycle supervision.
- Create `insto/service/runtime.py`: one resource-construction/cleanup path with explicit one-shot, REPL, and daemon roles.
- Modify `insto/service/facade.py`: accept the runtime-owned manager/coordinator and drain them during close.
- Modify `insto/commands/watch.py`: persistent `/watch`, `/unwatch`, `/watching` behavior and safe public serialization.
- Modify `insto/cli.py`: reserve `watch-daemon`, run it in the foreground, and use the shared runtime.
- Modify `insto/repl.py`: use shared runtime and supervise a lazy coordinator.
- Create `tests/test_watch_lock.py`, `tests/test_watch.py`, `tests/test_watch_daemon.py`, `tests/test_runtime.py`, `tests/e2e/test_watch_daemon.py`.
- Modify `tests/test_history.py`, `tests/test_commands_watch.py`, `tests/test_cli.py`, and documentation named in Task 8.

### Task 1: Persist restart-safe watch state

**Files:**
- Modify: `insto/models.py:265`
- Modify: `insto/service/history.py:52-218,406-490`
- Modify: `tests/test_history.py:205-250`

- [ ] **Step 1: Write failing DTO, migration, and registry tests**

Add tests that construct a v1 database, reopen it through `HistoryStore`, and assert:

```python
assert store.schema_version() == 2
watch = store.get_watch("alice")
assert watch is not None
assert watch.registration_id
assert watch.consecutive_errors == 0
```

Add two-store transaction coverage:

```python
for user in ("alice", "bob", "carol"):
    assert first.register_watch(user, 300).kind == "created"
assert second.register_watch("dave", 300).kind == "full"
assert second.register_watch("ALICE", 600).kind == "already_active"
```

Add delete/re-add protection and explicit-null coverage:

```python
old = store.register_watch("alice", 300).spec
assert old is not None
assert store.delete_watch("alice") is True
new = store.register_watch("alice", 600).spec
assert new is not None and new.registration_id != old.registration_id
assert store.update_watch_state(old, last_error="stale") is False
assert store.update_watch_state(new, last_ok=123, last_error=None) is True
assert store.get_watch("alice").last_error is None  # type: ignore[union-attr]
```

- [ ] **Step 2: Run the focused tests and observe the expected failure**

Run: `uv run pytest tests/test_history.py -q`

Expected: failures for missing `registration_id`, `consecutive_errors`, `register_watch`, and schema version 2.

- [ ] **Step 3: Add the immutable DTO and registration result**

Use these public shapes in `insto/models.py`:

```python
WatchRegistrationKind = Literal["created", "reactivated", "already_active", "full"]

@dataclass(frozen=True, slots=True)
class WatchSpec:
    user: str
    registration_id: str
    interval_seconds: int
    last_ok: int | None = None
    last_error: str | None = None
    consecutive_errors: int = 0
    status: WatchStatus = "active"

@dataclass(frozen=True, slots=True)
class WatchRegistration:
    kind: WatchRegistrationKind
    spec: WatchSpec | None
```

- [ ] **Step 4: Implement schema v2 and atomic CRUD**

Set `_SCHEMA_VERSION = 2`; represent each migration as a tuple of statements and execute statements with `cur.execute(statement)` inside the existing explicit transaction. Rebuild `watches` in migration 2 so its final shape enforces status, failure-count, and interval checks. Implement exact sync/async pairs named `register_watch`/`register_watch_async`, `get_watch`/`get_watch_async`, `list_watches`/`list_watches_async`, `update_watch_state`/`update_watch_state_async`, and `delete_watch`/`delete_watch_async`. Registration takes `(user: str, interval_seconds: int)` and returns `WatchRegistration`. Conditional update takes the original `WatchSpec`, accepts sentinel-aware `last_ok`, `last_error`, `consecutive_errors`, and `status` keyword fields, and returns whether the `user + registration_id` row matched. Async variants call only the matching sync method through `asyncio.to_thread`.

Normalize storage keys with `user.lstrip("@").strip().lower()`. Use `BEGIN IMMEDIATE` around duplicate/capacity checks plus insert/reactivation. Generate ids with `uuid.uuid4().hex`. Preserve `last_ok` on reactivation and reset error state/count.

- [ ] **Step 5: Run storage tests, lint, and type-check**

Run: `uv run pytest tests/test_history.py -q && uv run ruff check insto/models.py insto/service/history.py tests/test_history.py && uv run mypy insto`

Expected: all commands exit 0.

- [ ] **Step 6: Commit the storage increment**

```bash
git add insto/models.py insto/service/history.py tests/test_history.py
git commit -m "feat: persist restart-safe watch registrations"
```

### Task 2: Enforce one secure executor per database

**Files:**
- Create: `insto/service/watch_lock.py`
- Create: `tests/test_watch_lock.py`

- [ ] **Step 1: Write failing lock tests**

Cover free acquire, second-descriptor contention, idempotent release, distinct stores, path aliases, safe mode/owner/type checks, and crash release. The core assertion is:

```python
first = WatchProcessLock(db_path)
second = WatchProcessLock(db_path.parent / "." / db_path.name)
first.acquire()
with pytest.raises(WatchLockBusy):
    second.acquire()
first.release()
second.acquire()
second.release()
```

- [ ] **Step 2: Run the lock tests and observe the import failure**

Run: `uv run pytest tests/test_watch_lock.py -q`

Expected: collection fails because `insto.service.watch_lock` does not exist.

- [ ] **Step 3: Implement the focused lock helper**

Expose only `WatchLockError`, its `WatchLockBusy` subtype, and `WatchProcessLock`. The helper constructor takes `db_path: Path`; read-only `path: Path` and `acquired: bool` properties expose diagnostics; `acquire() -> None` and `release() -> None` own the descriptor lifecycle.

Use this concrete contention mapping:

```python
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError as exc:
    os.close(fd)
    raise WatchLockBusy(f"watch executor already active for {canonical_db}") from exc
```

Canonicalize the database with `expanduser().resolve(strict=False)` and derive `<canonical-db>.watch.lock`. Open with `O_RDWR | O_CREAT | O_CLOEXEC | O_NOFOLLOW` when available and mode `0o600`; validate `fstat` regular-file/current-uid properties before `fcntl.flock(fd, LOCK_EX | LOCK_NB)`. Write the PID only after ownership succeeds. Release with `LOCK_UN` and close; never unlink.

- [ ] **Step 4: Run lock tests and static checks**

Run: `uv run pytest tests/test_watch_lock.py -q && uv run ruff check insto/service/watch_lock.py tests/test_watch_lock.py && uv run mypy insto`

Expected: all commands exit 0.

- [ ] **Step 5: Commit the ownership primitive**

```bash
git add insto/service/watch_lock.py tests/test_watch_lock.py
git commit -m "feat: add per-store watcher process lock"
```

### Task 3: Make `WatchManager` persistence-aware and supervised

**Files:**
- Modify: `insto/service/watch.py`
- Create: `tests/test_watch.py`
- Modify: `tests/test_commands_watch.py`

- [ ] **Step 1: Move existing scheduler tests and add recovered-state failures**

Construct the manager with a fake lock and callbacks, then assert initial delay, persisted error streaks, false callback exit, callback exceptions on `fatal_error`, non-overlap, and drain-before-release:

```python
spec = WatchSpec("alice", "reg-1", 300, consecutive_errors=1)
manager.add(spec, tick=failing_tick, state_changed=state_changed, start=False)
updated = await manager.tick_once("alice")
assert updated.status == "paused"
assert updated.consecutive_errors == 2
```

- [ ] **Step 2: Run the scheduler tests and observe signature/state failures**

Run: `uv run pytest tests/test_watch.py -q`

Expected: failures because `WatchManager.add` does not accept a persisted `WatchSpec`, callback, or initial delay.

- [ ] **Step 3: Implement the manager contract**

Define `StateChangedFn = Callable[[WatchSpec], Awaitable[bool]]`. Construct the manager with `(process_lock: WatchProcessLock, *, release_when_empty: bool)`. Expose `fatal_error: asyncio.Future[BaseException]`, `acquire_executor()`, `release_executor()`, async `remove(user)` and `cancel_all()`, plus `add(spec, *, tick, state_changed, initial_delay=None, start=True) -> WatchSpec`.

The loop's scheduling core is:

```python
delay = entry.initial_delay
while entry.status == "active":
    await asyncio.sleep(delay)
    if not await self._do_tick(entry):
        return
    delay = float(entry.interval_seconds)
```

Keep mutable fields on private `_Entry` and emit a fresh immutable snapshot after every success, failed retry pair, or hard pause. A callback returning false ends that entry loop. A callback exception completes `fatal_error` and ends the loop. The periodic loop sleeps `initial_delay` once, then uses fixed delay after each tick. Do not emit state on cancellation.

- [ ] **Step 4: Run scheduler and regression tests**

Run: `uv run pytest tests/test_watch.py tests/test_commands_watch.py -q && uv run ruff check insto/service/watch.py tests/test_watch.py && uv run mypy insto`

Expected: all commands exit 0 after adapting command fixtures to the new constructor without changing `/diff` and `/history` behavior.

- [ ] **Step 5: Commit the scheduler increment**

```bash
git add insto/service/watch.py tests/test_watch.py tests/test_commands_watch.py
git commit -m "feat: supervise persistent watch tasks"
```

### Task 4: Reconcile persisted rows into local tasks

**Files:**
- Create: `insto/service/watch_daemon.py`
- Create: `tests/test_watch_daemon.py`
- Modify: `insto/service/facade.py`

- [ ] **Step 1: Write failing coordinator tests**

Test zero/multiple recovery, paused-row skip, add/remove/id/interval reconciliation, 0/2/4 staggering, another-store discovery, lifetime lock policy, fatal child propagation, and rate estimates. Drive a single cycle through a public test seam:

```python
await daemon.reconcile_once(recovering=True)
assert [spec.user for spec in manager.list()] == ["alice", "bob"]
assert delays == {"alice": 0.0, "bob": 2.0}
```

- [ ] **Step 2: Run coordinator tests and observe the import failure**

Run: `uv run pytest tests/test_watch_daemon.py -q`

Expected: collection fails because `insto.service.watch_daemon` does not exist.

- [ ] **Step 3: Implement the coordinator**

Construct `WatchDaemon` from keyword-only `history`, `manager`, `tick_factory`, `role: Literal["repl", "daemon"]`, `reconcile_seconds=2.0`, and an injectable wall clock. Expose async `start() -> int`, `reconcile_once(*, recovering=False) -> None`, `run(stop_event) -> None`, and `stop() -> None`.

The reconciliation comparison uses stable keys:

```python
persisted = {spec.user: spec for spec in await history.list_watches_async() if spec.status == "active"}
local = {spec.user: spec for spec in manager.list()}
replace = {
    user for user in persisted.keys() & local.keys()
    if persisted[user].registration_id != local[user].registration_id
    or persisted[user].interval_seconds != local[user].interval_seconds
}
```

`start()` acquires before reading rows for daemon role and lazily attempts in REPL role. Reconcile from one `list_watches_async()` result, cancel absent/paused/replaced local entries, and start new active entries. State callbacks redact `last_error` and call the conditional async update. Supervise stop-event, reconciliation task, and manager fatal future in one wait set; any infrastructure exception drains all tasks and is re-raised. Keep daemon ownership with zero rows; release REPL ownership once persisted and local active sets are empty.

Provide pure helpers `initial_delay(spec, now)`, `startup_offsets(specs, now)`, and `estimate_watch_load(specs)` for deterministic tests.

- [ ] **Step 4: Wire facade ownership without creating an import cycle**

Let `OsintFacade.__init__` accept a manager and later attach a coordinator:

```python
self.watches = watches
self.watch_daemon: WatchDaemon | None = None
```

Keep daemon imports under `TYPE_CHECKING`. `aclose()` stops an attached coordinator before closing CDN/backend resources.

- [ ] **Step 5: Run coordinator/facade tests and static checks**

Run: `uv run pytest tests/test_watch_daemon.py tests/test_facade.py -q && uv run ruff check insto/service/watch_daemon.py insto/service/facade.py tests/test_watch_daemon.py && uv run mypy insto`

Expected: all commands exit 0.

- [ ] **Step 6: Commit the reconciliation increment**

```bash
git add insto/service/watch_daemon.py insto/service/facade.py tests/test_watch_daemon.py tests/test_facade.py
git commit -m "feat: reconcile persistent watcher state"
```

### Task 5: Share runtime construction across execution roles

**Files:**
- Create: `insto/service/runtime.py`
- Create: `tests/test_runtime.py`
- Modify: `insto/repl.py:634-706`
- Modify: `insto/cli.py:629-706`

- [ ] **Step 1: Write failing runtime-role and cleanup tests**

Assert one-shot never acquires, daemon acquires before recovery, REPL remains lazy, and each injected construction failure closes already-created resources once in reverse order.

- [ ] **Step 2: Run runtime tests and observe the import failure**

Run: `uv run pytest tests/test_runtime.py -q`

Expected: collection fails because `insto.service.runtime` does not exist.

- [ ] **Step 3: Implement one async context factory**

Use explicit roles and a runtime object:

```python
RuntimeRole = Literal["oneshot", "repl", "daemon"]

@dataclass(slots=True)
class Runtime:
    config: Config
    history: HistoryStore
    facade: OsintFacade
    manager: WatchManager
    coordinator: WatchDaemon | None

@asynccontextmanager
async def open_runtime(
    config: Config,
    *,
    role: RuntimeRole,
    backend_factory: Callable[[Config], OSINTBackend],
) -> AsyncIterator[Runtime]:
    history = HistoryStore(config.db_path)
    runtime = build_runtime(config, history, role, backend_factory)
    try:
        if runtime.coordinator is not None and role == "daemon":
            await runtime.coordinator.start()
        yield runtime
    finally:
        await close_runtime_resources(runtime, history)
```

Open history, backend, CDN client, facade, manager, and coordinator in that order. For daemon, acquire and recover before yielding. On exit stop coordinator, drain manager, close facade clients, close SQLite, then release the descriptor in `finally` even when an earlier close fails.

- [ ] **Step 4: Replace CLI/REPL duplicate bootstrap with the factory**

Run one-shot dispatch within `async with open_runtime(config, role="oneshot", backend_factory=_build_backend)`; run the REPL with the same call and role `repl`. Keep `_bootstrap` as a thin compatibility seam only if existing tests import it; it must delegate to the shared runtime rather than construct resources itself.

- [ ] **Step 5: Run runtime, CLI, and REPL tests**

Run: `uv run pytest tests/test_runtime.py tests/test_cli.py tests/test_repl.py -q && uv run ruff check insto/service/runtime.py insto/cli.py insto/repl.py tests/test_runtime.py && uv run mypy insto`

Expected: all commands exit 0.

- [ ] **Step 6: Commit shared runtime wiring**

```bash
git add insto/service/runtime.py insto/cli.py insto/repl.py tests/test_runtime.py tests/test_cli.py tests/test_repl.py
git commit -m "refactor: share watcher runtime lifecycle"
```

### Task 6: Make slash commands persist and reconcile

**Files:**
- Modify: `insto/commands/watch.py:121-230`
- Modify: `tests/test_commands_watch.py`

- [ ] **Step 1: Write failing persistent command tests**

Assert canonical registration, typed duplicate/full errors, reactivation, source-of-truth listing, public serialization, one-shot reminder, immediate REPL reconcile request, persistent deletion, and error redaction:

```python
payload = await dispatch("/watch @Alice 600", facade=facade, session=session, console=console)
assert payload["user"] == "alice"
assert "registration_id" not in payload
assert history.get_watch("alice") is not None
```

- [ ] **Step 2: Run command tests and observe memory-only failures**

Run: `uv run pytest tests/test_commands_watch.py -q`

Expected: failures because commands still mutate `facade.watches` directly and serialize the internal DTO.

- [ ] **Step 3: Implement public serialization and persisted commands**

Add:

```python
def watch_public_dict(spec: WatchSpec) -> dict[str, object]:
    return {
        "user": spec.user,
        "interval_seconds": spec.interval_seconds,
        "last_ok": spec.last_ok,
        "last_error": spec.last_error,
        "consecutive_errors": spec.consecutive_errors,
        "status": spec.status,
    }
```

`/watch` calls `history.register_watch_async`; maps `already_active` and `full` to `CommandUsageError`; requests the attached REPL coordinator to reconcile without waiting for the polling interval; and prints a daemon reminder for one-shot role. `/unwatch` deletes through history then requests reconciliation. `/watching` reads `history.list_watches_async()` and exports only `watch_public_dict` rows.

- [ ] **Step 4: Run command and storage integration tests**

Run: `uv run pytest tests/test_commands_watch.py tests/test_history.py -q && uv run ruff check insto/commands/watch.py tests/test_commands_watch.py && uv run mypy insto`

Expected: all commands exit 0.

- [ ] **Step 5: Commit persistent controls**

```bash
git add insto/commands/watch.py tests/test_commands_watch.py
git commit -m "feat: persist watcher control commands"
```

### Task 7: Add foreground daemon CLI and process coverage

**Files:**
- Modify: `insto/cli.py:185-245,710-790`
- Modify: `tests/test_cli.py`
- Create: `tests/e2e/test_watch_daemon.py`

- [ ] **Step 1: Write failing parser/routing and subprocess tests**

Assert `watch-daemon` routing precedes normal target dispatch while `@watch-daemon` remains a valid username. Stub foreground runtime for return-code tests, then use a fake backend subprocess harness for registration → daemon tick → second-daemon rejection → SIGTERM → restart.

- [ ] **Step 2: Run CLI tests and observe missing route failures**

Run: `uv run pytest tests/test_cli.py tests/e2e/test_watch_daemon.py -q`

Expected: failures because `main()` treats `watch-daemon` as a profile target.

- [ ] **Step 3: Implement foreground daemon execution**

Add `async _run_watch_daemon(config, log) -> int`. Within daemon-role runtime, install `loop.add_signal_handler` for SIGINT/SIGTERM to set one event, print database/recovered/load estimates plus backend-specific risk text, then await `coordinator.run(stop_event)`. Map `WatchLockBusy` and infrastructure errors through centralized redaction to non-zero; graceful signals return zero. Route exact bare `watch-daemon` before command and REPL branches.

- [ ] **Step 4: Run CLI and POSIX process tests**

Run: `uv run pytest tests/test_cli.py tests/e2e/test_watch_daemon.py -q && uv run ruff check insto/cli.py tests/test_cli.py tests/e2e/test_watch_daemon.py && uv run mypy insto`

Expected: all commands exit 0 on POSIX.

- [ ] **Step 5: Commit daemon entrypoint**

```bash
git add insto/cli.py tests/test_cli.py tests/e2e/test_watch_daemon.py
git commit -m "feat: add foreground watch daemon"
```

### Task 8: Document, verify, and prepare issue #15 for closure

**Files:**
- Modify: `README.md`
- Modify: `docs/cli-reference.md`
- Modify: `docs/basic-usage.md`
- Modify: `docs/architecture.md`
- Modify: `docs/roadmap.md`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Update user and architecture documentation**

Document one-shot registration, foreground daemon startup, REPL ownership/handoff, persisted pause/error semantics, three-target/five-minute limits, shutdown/restart, backend-call estimates, and POSIX-only ownership/signal behavior. Mark only the implementation as shipped in the roadmap; issue closure waits for merge to the default branch.

- [ ] **Step 2: Run focused feature verification**

Run:

```bash
uv run pytest \
  tests/test_history.py \
  tests/test_watch_lock.py \
  tests/test_watch.py \
  tests/test_watch_daemon.py \
  tests/test_runtime.py \
  tests/test_commands_watch.py \
  tests/test_cli.py \
  tests/e2e/test_watch_daemon.py
```

Expected: all selected tests pass.

- [ ] **Step 3: Run repository quality gates**

Run:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy insto
uv run mkdocs build --strict
uv run pytest
```

Expected: lint, format, types, and docs exit 0. The full suite must have no new failure; if the documented Rich 15 welcome-shortcut mismatch remains, record exactly that one pre-existing failure and prove every watcher-focused test passes.

- [ ] **Step 4: Review the complete branch diff**

Run: `git diff --check origin/main HEAD && git status --short && git diff --stat origin/main HEAD`

Expected: no whitespace errors, only intended files, and a clean worktree after the final commit.

- [ ] **Step 5: Commit documentation**

```bash
git add README.md docs/cli-reference.md docs/basic-usage.md docs/architecture.md docs/roadmap.md CHANGELOG.md
git commit -m "docs: explain persistent watcher daemon"
```

- [ ] **Step 6: Hand off for review and merge**

Run the repository's pre-landing review workflow, then create the pull request. Close issue #15 only once the verified branch is merged into the default branch.
