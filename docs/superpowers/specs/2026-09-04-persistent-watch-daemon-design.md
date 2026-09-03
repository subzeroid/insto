# Persistent Watch Daemon Design

## Goal

Finish GitHub issue #15 by turning the existing session-local watch scheduler
into a persistent, local-only daemon workflow. Registered watches survive REPL
exit and process restart, retain their status in SQLite, and remain controllable
through the existing slash-command grammar.

## Scope

The shipped workflow provides:

- a foreground `insto watch-daemon` process suitable for a user-managed shell,
  `tmux`, `systemd`, or `launchd`;
- recovery of active watches from the configured SQLite store;
- persistent registration, removal, interval, `last_ok`, `last_error`, and
  active/paused status;
- cross-process add, remove, list, and inspect behavior through `/watch`,
  `/unwatch`, and `/watching`;
- exactly one local watch executor for a configured store;
- conservative scheduling, retry, pause, and shutdown behavior;
- fully offline unit and end-to-end coverage.

The following remain out of scope:

- email, webhook, Discord, ntfy, or other outbound notifications;
- an HTTP API, web dashboard, IPC server, telemetry, or phone-home behavior;
- generated `systemd` or `launchd` unit files;
- more than three active watches or an interval below 300 seconds;
- broad crawling, discovery targets, or new backend surfaces.

## User Interface

### Daemon entrypoint

`insto watch-daemon` starts the daemon in the foreground. The existing parser
already reserves a positional word for `setup`; treating `watch-daemon` as a
second reserved word adds the daemon without introducing subparsers or changing
the established `insto @user -c ...` grammar.

Startup prints the configured database path, the number of recovered active
watches, and the estimated ticks/hour and backend calls/hour implied by their
intervals. It also prints a backend-specific quota/cost or rate-limit/account
risk reminder. If another process already executes watches for that database,
startup fails with a concise message and a non-zero exit code.

### Control commands

- `insto @user -c watch 600` and `/watch user 600` persist an active watch.
- `/watch` in a REPL continues to execute locally when it can become the watch
  executor. If another daemon or REPL owns execution, the command only persists
  the registration; the current owner discovers it through the same reconcile
  loop within two seconds.
- Re-registering a paused watch reactivates it and applies the supplied interval.
  Re-registering an already-active watch remains an error.
- `insto -c unwatch user` and `/unwatch user` delete the persisted registration
  and cancel a matching task in the process that observes the change.
- `insto -c watching` and `/watching` read the SQLite registry, so both modes
  report the same active/paused state and timestamps.

A successful one-shot `/watch` registration stays stored even though that
one-shot process immediately exits. A one-shot process never claims executor
ownership: it tells the operator that an existing executor will discover the
row, or to run `insto watch-daemon` when none is active.

## Architecture

### `WatchProcessLock`

A focused service helper owns a non-blocking POSIX advisory lock. Its path is
derived from the canonical database path (`expanduser().resolve(strict=False)`),
with the default resolving beside `~/.insto/store.db`. Canonicalizing before
derivation prevents relative, `..`, and symlink aliases for the same SQLite file
from producing independent locks. Different stores still receive different
locks.

The lock file is opened with owner-only permissions, close-on-exec, and
no-follow semantics where the platform exposes them. The helper verifies that
an existing path is a regular file owned by the current uid before locking or
writing diagnostics. The operating system releases the advisory lock when a
process exits unexpectedly, so stale PID-file cleanup is unnecessary. The file
may contain the owning PID for diagnostics, but PID contents never determine
ownership. Release closes the descriptor but never unlinks the lock path;
unlinking could let contenders lock different inodes for the same database.

`WatchManager` is the only component that acquires or releases the lock. The
foreground daemon calls an explicit `acquire_executor()` before recovery and
holds ownership for its full lifetime. A REPL coordinator calls the same method
lazily after the first persisted registration. The manager releases only its
own descriptor through an explicit `release_executor()` after tasks drain; a
one-shot process never acquires it. The daemon calls release only during
shutdown, even when the registry is empty. A REPL coordinator may release after
reconciliation confirms that both the persisted active set and local task set
are empty. Failure to acquire never deletes a successfully persisted watch.

### SQLite watch registry

The existing `watches` table remains the source of truth:

```text
user, registration_id, interval_seconds, last_ok, last_error,
consecutive_errors, status
```

No new scheduler database or network coordination layer is introduced.
Registration uses a SQLite `BEGIN IMMEDIATE` transaction to enforce the maximum
of three active watches across processes. Every new or reactivated registration
gets a new opaque `registration_id`; state updates include both `user` and
`registration_id` in their `WHERE` clause. An update from a deleted or replaced
task therefore affects zero rows and cannot resurrect `/unwatch` data or mutate
a newly re-added watch for the same username.

Schema version 2 adds `registration_id` and `consecutive_errors`, backfilling
existing rows during migration. The existing migration runner must first stop
using `sqlite3.Cursor.executescript()` inside its outer transaction because
`executescript()` commits that transaction implicitly. Migrations become ordered
statement tuples executed one statement at a time under the existing
`BEGIN IMMEDIATE`, with schema changes, backfill, and version update committed
atomically. A fixture database at schema version 1 verifies the upgrade path.

Reactivating a paused row assigns a fresh id, sets `status = active`, clears
`last_error`, resets `consecutive_errors` to zero, and preserves `last_ok` as
provenance. A successful tick clears `last_error` explicitly and resets the
counter. The store update API uses an explicit "unchanged" sentinel so writing
SQL `NULL` is distinct from omitting a field.

The history service exposes async wrappers for registration, deletion, listing,
and state updates. SQLite work continues to run through `asyncio.to_thread`, in
line with the existing architecture.

The registry stores canonical bare lowercase usernames. Registration performs
the same validation as the command layer, and the v2 schema adds checks for the
known statuses, non-negative failure counts, and production interval floor.
Typed registration outcomes distinguish an already-active row from a full
registry; callers never parse exception text to decide control flow.

### `WatchManager`

`WatchManager` remains the only per-process task scheduler. It gains:

- explicit and lazy acquisition paths for its sole ownership of
  `WatchProcessLock`;
- an optional initial delay for recovered watches;
- recovery of `registration_id` and `consecutive_errors` with each entry;
- an async state-change callback invoked after success, recoverable failure, or
  transition to paused;
- a fatal-error future that reports scheduler or state-callback failures to its
  coordinator instead of leaving an exception on a detached task;
- lock release after `remove()` or `cancel_all()` drains the last task.

The existing retry policy stays intact: one retry inside a failed tick, with a
watch paused after two consecutive failed ticks even when the process restarts
between them. `Banned` and `AuthInvalid` remain immediate hard pauses. A state
callback failure is infrastructure failure, not a backend tick failure, and is
reported through the fatal-error future without consuming retry budget.

`add()` accepts a complete stored `WatchSpec`, rather than a second parallel set
of state parameters. The state callback receives an immutable snapshot and
returns whether its conditional database update still matched the registration.
A false result means the task is stale and its loop exits normally; an exception
means infrastructure failed and completes the fatal-error future.

`WatchSpec` becomes a frozen storage DTO. Mutable scheduler-only fields stay on
the private `_Entry`, and every callback receives a fresh `WatchSpec` snapshot;
no layer shares a mutable state object across an `await` boundary.

### `WatchDaemon`

`WatchDaemon` is a small reusable coordinator around `HistoryStore`,
`WatchManager`, and `OsintFacade`. The CLI runs it in the foreground; a REPL that
wins executor ownership runs the same coordinator as a background task. It does
not create a server or expose backend SDK objects.

Every two seconds the current executor, whether daemon or REPL, reconciles
persisted active rows with local tasks:

- newly active rows start;
- deleted or paused rows stop;
- registration-id or interval changes replace the old task with a new schedule;
- manager state changes are written back to the corresponding row.

Paused rows remain visible but are never restarted automatically. Re-running
`/watch` explicitly reactivates one.

The coordinator waits for the stop event, reconcile-loop completion, and the
manager fatal-error future. An unexpected registry, scheduler, or state-callback
failure stops reconciliation, drains all tasks, releases the lock, and makes a
foreground daemon exit non-zero. In a REPL it prints a redacted warning and
leaves the persisted registrations intact for a later daemon restart.

```text
/watch, /unwatch, /watching
          |
          v
  SQLite registry  <--------------+
          |                         |
          | reconcile every 2s      | conditional state update
          v                         | (user + registration_id)
  WatchDaemon coordinator           |
          |                         |
          v                         |
  WatchManager -- tick --> OsintFacade.diff_and_snapshot
          |
          +-- sole owner of per-database process lock
```

### Implementation boundaries

- `service/watch.py` keeps only the task scheduler, state machine, shared watch
  constants, and public diff formatting.
- `service/watch_lock.py` contains the POSIX file-lock implementation.
- `service/watch_daemon.py` contains the reusable coordinator and no CLI parsing.
- A shared async runtime context factory constructs and tears down
  `HistoryStore`, backend, CDN client, facade, manager, and coordinator for the
  one-shot, REPL, and daemon entrypoints. This replaces the duplicated bootstrap
  sequences currently present in `cli.py` and `repl.py`; daemon setup must not
  become a third copy.
- The runtime configures an explicit role: one-shot is control-only, REPL may
  acquire execution lazily, and daemon must acquire before recovery. Commands do
  not infer their role from TTY state or console presence.

The internal `registration_id` is never included in command JSON or terminal
output. `/watch` and `/watching` build an explicit public payload containing
username, interval, timestamps, redacted error, failure count, and status instead
of applying `dataclasses.asdict()` to the storage DTO.

## Scheduling and Data Flow

For a recovered watch with `last_ok`, the initial delay is:

```text
max(0, last_ok + interval_seconds - current_time)
```

A row without `last_ok` waits one full interval before its first tick, matching
the existing session-local behavior. When several recovered rows are already
due, the daemon assigns offsets of 0, 2, and 4 seconds in username order. With
the three-watch ceiling this guarantees that no two restored watches start at
the same instant. Normal subsequent ticks use fixed-delay scheduling: the
configured interval starts after the preceding tick finishes. A slow request
therefore delays the next tick instead of creating overlap or catch-up bursts
for one target.

One tick performs the existing `diff_and_snapshot` flow:

1. fetch the current profile once;
2. compare it with the latest stored snapshot;
3. fetch the bounded recent-post set;
4. persist the fresh snapshot;
5. update the watch state.

The daemon emits changes and errors only to its normal terminal/log output.
Outbound notification transports are intentionally deferred.

### Performance budget

The deliberately small watch ceiling keeps all coordinator work bounded:

- reconciliation performs one SQLite list operation every two seconds, or at
  most 43,200 tiny reads per day, and examines no more than three active rows;
- the 300-second interval floor permits at most 36 total ticks/hour across
  three watches;
- each tick uses roughly two cached-identity backend calls, or three when the
  target id must first be resolved, for a displayed upper estimate of 72-108
  calls/hour (1,728-2,592/day at the minimum interval);
- one target has at most one tick in flight, and work is never queued to catch
  up after a slow request or process suspension.

The coordinator does not retain a long-lived registry cache because other
processes must remain visible. Reconciliation performs its SQLite operation in
`asyncio.to_thread`, returns the rows, and releases database/thread locks before
it awaits task cancellation, startup, or any backend operation. With the hard
three-row limit, a revision table or notification IPC would add complexity
without a material performance benefit.

## Error Handling

- `Banned` or `AuthInvalid`: persist `paused` and `last_error` immediately;
  schedule no further tick.
- Other tick exception: use the existing single retry. If both attempts fail,
  atomically persist the error and increment `consecutive_errors`. Pause after
  the second consecutive failed tick, including across process restarts.
- One target failure: keep reconciling and executing other targets.
- Registry, reconcile, scheduler, or state-callback failure: stop the current
  executor rather than leaving a detached failed task. A foreground daemon exits
  non-zero so a supervisor may restart it.
- Executor lock contention: exit daemon startup non-zero, or leave a `/watch`
  registration persisted for the current owner to discover.
- Cancellation: never turn a cancellation into `last_error` or `paused`.

Every error is passed through secret redaction before it reaches `last_error` in
SQLite. Terminal output then passes through the existing centralized formatter
as well. `/watching` therefore never prints a raw backend exception captured
before redaction.

## Shutdown

The CLI installs SIGINT and SIGTERM handlers that set one async stop event.
Shutdown order is:

1. stop the reconcile loop;
2. cancel and await scheduled and in-flight watch tasks;
3. close backend and CDN clients;
4. close SQLite;
5. release the process lock.

The order prevents detached ticks from writing through a closed connection.
Repeated signals remain idempotent.

## Testing

All tests use temporary databases, fake backends, injected clocks/delays, and no
live Instagram calls.

Pytest with `pytest-asyncio` is the detected project test framework. Existing
manager retry/cancellation coverage is retained; manager-only cases move from
the command test module into a focused service test rather than growing the
command fixture further.

### State and coverage map

```text
PERSISTED WATCH STATE

                 register / reactivate (new registration_id)
                                  |
                                  v
                         ACTIVE, errors=0
                          /      |       \
                    success   soft fail   Banned/AuthInvalid
                       |          |              |
                       |          v              v
                       +--- ACTIVE, errors=1   PAUSED
                              /       \
                         success     soft fail
                            |            |
                            v            v
                     ACTIVE, errors=0   PAUSED

Any state -- /unwatch --> deleted
Old callback -- mismatched registration_id --> no-op + old loop exits
```

```text
CODE PATHS                                      USER / PROCESS FLOWS
[+] WatchProcessLock                            [+] one-shot control
  |-- [GAP -> unit] acquire / busy / release      |-- [GAP -> E2E] /watch persists, exits
  |-- [GAP -> unit] canonical aliases             |-- [GAP -> unit] /watching reads DB
  |-- [GAP -> unit] unsafe path rejected           `-- [GAP -> unit] /unwatch deletes
  `-- [GAP -> subprocess] crash releases lock

[+] HistoryStore registry                       [+] REPL executor
  |-- [GAP -> unit] fresh v2 + v1 migration       |-- [GAP -> integration] wins lock, ticks
  |-- [GAP -> unit] atomic registration limit     |-- [GAP -> integration] loses lock, control-only
  |-- [GAP -> unit] reactivate / duplicate         `-- [GAP -> integration] sees other-process rows
  |-- [GAP -> unit] conditional token update
  `-- [GAP -> unit] clear NULL / persist streak  [+] foreground daemon
                                                    |-- [GAP -> E2E] recover + due tick
[+] WatchManager                                   |-- [GAP -> E2E] second daemon rejected
  |-- [★★★ EXISTING] retry / hard pause             |-- [GAP -> E2E] SIGTERM drains and exits 0
  |-- [★★★ EXISTING] cancel sleeping/in-flight      `-- [GAP -> E2E] restart resumes row
  |-- [GAP -> unit] initial delay / recovered state
  |-- [GAP -> unit] callback match / stale / fatal [+] visible state
  `-- [GAP -> unit] failure streak across restart   |-- [GAP -> unit] rate/cost startup summary
                                                    |-- [GAP -> unit] redacted last_error
[+] WatchDaemon reconcile                          `-- [GAP -> unit] internal id not exported
  |-- [GAP -> unit] add / remove / pause
  |-- [GAP -> unit] id / interval replacement
  |-- [GAP -> unit] deterministic staggering
  |-- [GAP -> unit] zero-row ownership policy
  `-- [GAP -> unit] fatal child propagation

EXISTING RELEVANT COVERAGE: 2 behavior groups at quality ★★★
PLAN GAPS FOUND: 18 requirement groups, all assigned below
E2E: 4 process flows | EVAL: none (no LLM or prompt changes)
```

Legend: `★★★` covers behavior, edge cases, and errors. A `GAP` is absent from
the current code/tests; every gap becomes an implementation requirement below.

### Required test files and assertions

`tests/test_watch_lock.py` (unit plus a bounded subprocess):

- acquire a free lock, reject a second descriptor, release, and reacquire;
- map relative, `..`, and symlink aliases of one database to one lock identity,
  while two distinct database paths do not block each other;
- create mode `0600`, reject a symlink/non-regular/wrong-owner existing path,
  set close-on-exec, and leave the inode present after release;
- let a child process acquire and exit without cleanup, then prove the parent
  can acquire within a bounded timeout.

`tests/test_history.py` (unit with separate SQLite connections):

- assert a fresh database is schema v2 with both new columns and constraints;
- open a hand-built schema-v1 fixture, verify atomic backfill and preserved
  watch data, then reopen to prove migration idempotency;
- inject a failing migration statement and assert schema/data/version roll back
  together rather than committing a half-migration;
- register three active rows through two stores, reject the fourth with the
  typed full outcome, and reject a case-variant duplicate as already active;
- reactivate a paused row with a new id, preserved `last_ok`, cleared error, and
  zero failure count;
- conditionally update the matching id, return false for a stale id after
  delete-and-re-add, and explicitly clear `last_error` to SQL `NULL` on success;
- reject invalid intervals/status/counts at the storage boundary and cover each
  async wrapper.

`tests/test_watch.py` (scheduler unit tests with injected clock and delay):

- retain retry-success, two failed ticks, immediate hard pause, sleeping-task
  cancellation, and in-flight cancellation coverage from the existing module;
- cover explicit daemon acquisition, lazy REPL acquisition, busy ownership,
  idempotent release, and drain-before-release ordering;
- start from a recovered spec with failure count one and prove the next failed
  tick pauses it; prove success resets the counter and clears the error;
- assert exact initial-delay values for no `last_ok`, future due time, overdue
  time, and wall-clock rollback;
- assert callback payloads after success/soft failure/hard pause, false callback
  result exits the stale loop, callback exception completes the fatal future,
  and cancellation emits no state change;
- block one tick longer than its interval and prove a second invocation neither
  overlaps nor queues a catch-up run.

`tests/test_watch_daemon.py` (async service integration with fake store/facade):

- recover zero and multiple active rows; skip paused rows;
- reconcile add, delete, pause, registration-id replacement, and interval
  replacement without duplicate local tasks;
- perform exactly one bounded registry read per reconcile cycle and never hold
  a store lock while awaiting manager or backend work;
- stagger only overdue recovered rows at 0/2/4 seconds in username order;
- keep daemon ownership with zero rows, release REPL ownership only after both
  persisted and local active sets are empty, and let a REPL owner discover a
  registration written through a second store;
- stop and drain all watches on registry, reconcile, scheduler, or callback
  failure; assert foreground mode returns failure while REPL mode emits one
  redacted warning and preserves rows;
- calculate startup ticks/hour and 2-3 calls/tick bounds for mixed intervals.

`tests/test_runtime.py` (shared resource-factory unit tests):

- construct the same resources for explicit one-shot, REPL, and daemon roles;
- assert one-shot never acquires, REPL acquires lazily, and daemon acquires before
  loading rows;
- inject failure after each resource is constructed and assert reverse-order,
  exactly-once cleanup; on normal shutdown assert tasks drain before clients and
  SQLite close.

`tests/test_commands_watch.py` (command integration):

- `/watch` stores a canonical username and returns typed duplicate/full errors;
- a paused registration reactivates with a new id while the public payload omits
  that id; `/watching --json` also omits it and includes status/error-count;
- REPL registration starts or joins its coordinator, while one-shot registration
  stays control-only and prints the daemon reminder;
- `/unwatch` deletes persistent state, requests immediate local reconciliation,
  and remains false for an unknown target;
- a secret-bearing backend failure is redacted in SQLite and terminal listing.

`tests/test_cli.py` (routing unit tests):

- parse `insto watch-daemon`, keep `insto @watch-daemon -c info` as a username,
  and route the reserved word before REPL startup;
- map lock contention and daemon runtime failure to concise non-zero exits, map
  graceful stop to zero, and keep all messages on the centralized redaction path.

`tests/e2e/test_watch_daemon.py` (offline fake-backend subprocess):

- register with the real one-shot CLI and verify the process exits while the row
  remains active;
- make the row due, launch the real daemon, wait boundedly for a snapshot and
  `last_ok`, then assert a second daemon exits non-zero on lock contention;
- send real SIGTERM, require a bounded zero exit with no detached writes, reopen
  SQLite, make the row due again, restart, and observe a second successful tick.

The subprocess test is POSIX-only because the first release explicitly uses
POSIX advisory locks and signals; all pure scheduling, persistence, and command
tests remain platform-neutral.

Coverage includes:

- process-lock exclusivity, release, and per-database isolation;
- canonical lock identity for relative, `..`, and symlink aliases;
- rejection of unsafe existing lock paths and release without unlinking;
- a daemon acquiring once without a second manager self-acquisition;
- daemon lifetime ownership with zero watches and REPL release after the last
  persisted active watch;
- transactional maximum-active-watch enforcement;
- canonical lowercase username storage, case-insensitive duplicate rejection,
  and typed already-active/full outcomes;
- schema-v1 to schema-v2 migration atomicity without `executescript()` commits;
- persistent `/watch`, `/unwatch`, and `/watching` behavior;
- local REPL execution when the lock is free and daemon handoff when busy;
- discovery of cross-process registrations by a REPL-owned executor;
- daemon recovery and initial-delay calculation;
- reconcile add, remove, pause, and interval replacement;
- delete-and-re-add protection against a stale in-flight state callback;
- successful tick state persistence;
- recoverable failure state and pause threshold across process restart;
- immediate hard-error pause;
- secret redaction before `last_error` persistence and `/watching` display;
- fatal callback/reconcile failure propagation to coordinator shutdown;
- one shared runtime bootstrap and the three explicit execution roles;
- public watch serialization that never exposes `registration_id`;
- cancellation without false failure state;
- deterministic startup staggering;
- SIGINT/SIGTERM-style stop-event shutdown;
- CLI parsing and an offline fake-backend daemon smoke flow.

The existing full offline suite remains the regression gate. Its current
baseline is 924 passing tests and one unrelated Rich 15 rendering failure in
`tests/test_render.py::test_welcome_shows_shortcuts`; the daemon work must not
introduce any additional failures.

## Engineering Review Findings

### What already exists

- `insto/service/watch.py` already provides the per-target async loop, retry,
  hard-pause, cancellation, and in-flight task draining. The design evolves this
  scheduler rather than replacing its tested state machine.
- `insto/service/history.py` already owns SQLite schema migration, lock retries,
  the `watches` table, and watch CRUD. The persistent registry stays in this
  service and tightens its transaction and update semantics.
- `insto/service/facade.py` already exposes `diff_and_snapshot`, which fetches a
  profile once, reuses its per-session target-id cache, reads bounded posts, and
  stores the snapshot. Each daemon tick reuses that exact flow.
- `insto/commands/watch.py` already defines validation, the five-minute floor,
  three-watch scheduler limit, diff formatting, and slash-command grammar. The
  commands switch their source of truth from process memory to SQLite.
- `insto/cli.py` and `insto/repl.py` already contain safe resource construction,
  redacted error formatting, and ordered cleanup, but duplicate the bootstrap.
  The shared runtime context extracts those behaviors without changing backend
  selection.

### NOT in scope

- Outbound notifications remain deferred because issue #15 is about durable
  scheduling and state, while transports require independent delivery policy.
- HTTP, IPC, dashboards, and remote control remain deferred because SQLite plus
  bounded reconciliation satisfies local cross-process control.
- Generated supervisor unit files remain deferred; foreground operation is
  sufficient for shell, `tmux`, `systemd`, and `launchd` users.
- Higher watch limits, shorter intervals, and broad discovery remain deferred
  to preserve the existing conservative account/rate posture.
- Windows-specific lock and signal parity remains deferred because the first
  release explicitly uses POSIX advisory locks and signals.

### Deferred-work disposition

No `TODOS.md` exists. Three potential follow-ups were evaluated and deliberately
not added: notification transports would need a separate delivery/retry design;
generated service definitions are packaging work with platform-specific policy;
and Windows parity needs a different ownership/signal implementation. Each is
already explicit above, none blocks issue #15, and a placeholder TODO would add
no context beyond this reviewed design.

### Failure modes

| New path | Realistic production failure | Test | Handling | User-visible result |
|---|---|---|---|---|
| Process lock | Alias or unsafe inode permits two executors | Unit + subprocess | Canonical path, ownership/type checks, advisory lock | Clear startup contention/path error |
| Schema v2 migration | Statement fails after a partial schema change | Migration unit | One explicit transaction and rollback | Clear store-open failure; old schema remains usable |
| Registration | Two processes race for the third slot | Multi-connection unit | `BEGIN IMMEDIATE` plus typed outcome | Clear capacity error |
| Reactivation | Old in-flight callback targets a new registration | Store/manager unit | Conditional update by registration id | Stale loop exits; no misleading mutation |
| Reconciliation | Registry read raises or returns inconsistent work | Coordinator unit | Supervised fatal path and full drain | Daemon exits non-zero; REPL warns once |
| Reconciliation | Add/remove happens in another process | Integration unit | Two-second source-of-truth refresh | Change becomes active without silent dormancy |
| Recovery | Several rows are overdue together | Coordinator unit | Deterministic 0/2/4-second staggering | Normal startup summary, no request burst |
| Scheduler | Tick runs longer than its interval | Scheduler unit | Fixed-delay loop, one in-flight task | Later tick is delayed, never overlapped |
| Soft backend failure | Retry pair fails across two process lifetimes | Scheduler/store unit | Persisted counter and pause threshold | Redacted error/status in `/watching` |
| Hard backend failure | Account is banned or authentication expires | Scheduler unit | Immediate persisted pause | Redacted paused state is visible |
| State callback | SQLite update raises after a successful tick | Manager/coordinator unit | Fatal future stops executor | Non-zero daemon exit or one REPL warning |
| Bootstrap | Backend/CDN construction fails after store open | Runtime unit | Reverse-order exactly-once cleanup | Centralized redacted startup error |
| Shutdown | Signal arrives during sleep or network work | Unit + E2E | Idempotent stop, cancel, await, then close | Graceful zero exit; no detached writes |
| Serialization | Internal token or raw secret reaches output | Command unit | Explicit public DTO plus pre-persistence redaction | Safe human and JSON output |

Every listed failure has a planned test and handling path; none is both
unhandled and silent, so the review found zero critical failure-mode gaps.

Inline ASCII comments should preserve the state-transition diagram beside the
manager state machine in `insto/service/watch.py` and the reconcile/conditional-
update pipeline beside the coordinator in `insto/service/watch_daemon.py`. The
lock helper is linear enough that a diagram would not improve it.

### Worktree parallelization strategy

Sequential implementation, no parallelization opportunity. Storage DTOs and
transaction outcomes define manager contracts; those define coordinator and
runtime behavior; commands and end-to-end tests depend on all three. Splitting
these shared service boundaries across worktrees would create more merge and
contract risk than latency savings. Documentation follows verified behavior.

## Implementation Tasks

Synthesized from this review's findings. Each task derives from a specific
finding above. Run with Claude Code or Codex; checkbox as you ship.

- [ ] **T1 (P1, human: ~3h / CC: ~35min)** — Persistence — make watch registration transactional and restart-safe
  - Surfaced by: Architecture review — schema migration atomicity, ABA-safe registration identity, and persistent failure streaks.
  - Files: `insto/models.py`, `insto/service/history.py`, `tests/test_history.py`
  - Verify: `uv run pytest tests/test_history.py`
- [ ] **T2 (P1, human: ~4h / CC: ~45min)** — Scheduler ownership — add the secure process lock and persistent manager state contract
  - Surfaced by: Architecture and performance reviews — one canonical executor, fixed-delay scheduling, callback supervision, and drain-before-release.
  - Files: `insto/service/watch_lock.py`, `insto/service/watch.py`, `tests/test_watch_lock.py`, `tests/test_watch.py`
  - Verify: `uv run pytest tests/test_watch_lock.py tests/test_watch.py`
- [ ] **T3 (P1, human: ~4h / CC: ~45min)** — Coordination/runtime — reconcile SQLite state and supervise daemon/REPL execution
  - Surfaced by: Architecture and code-quality reviews — shared coordinator, explicit runtime roles, cross-process discovery, and one bootstrap path.
  - Files: `insto/service/watch_daemon.py`, `insto/service/runtime.py`, `insto/service/facade.py`, `insto/repl.py`, `tests/test_watch_daemon.py`, `tests/test_runtime.py`
  - Verify: `uv run pytest tests/test_watch_daemon.py tests/test_runtime.py tests/test_repl.py tests/test_facade.py`
- [ ] **T4 (P1, human: ~3h / CC: ~35min)** — CLI controls — expose foreground daemon and persistent watch commands safely
  - Surfaced by: Code-quality review — typed outcomes, canonical usernames, explicit public serialization, and reserved daemon routing.
  - Files: `insto/commands/watch.py`, `insto/cli.py`, `tests/test_commands_watch.py`, `tests/test_cli.py`
  - Verify: `uv run pytest tests/test_commands_watch.py tests/test_cli.py`
- [ ] **T5 (P1, human: ~3h / CC: ~35min)** — Process verification — prove lifecycle, contention, restart, and signal behavior offline
  - Surfaced by: Test review — four uncovered critical user/process flows.
  - Files: `tests/e2e/test_watch_daemon.py`, test-only fake backend fixtures
  - Verify: `uv run pytest tests/e2e/test_watch_daemon.py`
- [ ] **T6 (P2, human: ~1h / CC: ~15min)** — Documentation — document the persistent workflow and mark issue #15 shipped
  - Surfaced by: Scope/completion review — foreground operation needs discoverable usage, risk, and recovery guidance.
  - Files: `README.md`, `docs/cli-reference.md`, `docs/basic-usage.md`, `docs/architecture.md`, `docs/roadmap.md`, `CHANGELOG.md`
  - Verify: `uv run mkdocs build --strict`

### Review completion summary

- Step 0, Scope Challenge: full issue #15 scope accepted.
- Architecture Review: 11 issues found and folded into the design.
- Code Quality Review: 7 issues found and folded into the design.
- Test Review: diagram produced, 18 gaps identified and assigned.
- Performance Review: 1 issue found and folded into fixed-delay/bounded-I/O rules.
- NOT in scope: written.
- What already exists: written.
- `TODOS.md` updates: 3 candidates evaluated, 0 added per recommended disposition.
- Failure modes: 0 critical gaps flagged.
- Outside voice: nested Codex skipped in a Codex host; requested Claude pass unavailable after bounded retries.
- Parallelization: 1 sequential lane, 0 parallel lanes.
- Lake Score: 37/37 findings resolved with the complete recommended option.

## Documentation and Completion

Update the CLI reference, basic usage, architecture notes, README, and roadmap.
The roadmap entry for the persistent daemon moves to shipped status. GitHub
issue #15 is closed only after the verified feature is merged into the default
branch.
