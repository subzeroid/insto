# Architecture

Six layers, top to bottom. The rule that holds the design together: each layer talks DTOs to the layer below, never raw API dicts.

```text
UI:        REPL (prompt_toolkit) │ one-shot CLI │ foreground watch daemon
Dispatch:  parse → validate → run → render
Commands:  commands/{target,profile,media,network,content,interactions,discovery,
                    direct,saved,places,batch,watch,operational,dossier}.py
Service:   runtime · facade · history · analytics · exporter · watch_daemon · watch · watch_lock · watch_webhook
Backends:  OSINTBackend ABC · HikerBackend · AiograpiBackend
Models:    @dataclass(slots=True) DTOs — Profile, Post, Story, User, Comment, Quota, ...
```

## Conventions

- **Async everywhere.** `httpx` (transitive via `hikerapi`), `asyncio` for fan-out, `asyncio.to_thread` for sqlite calls.
- **Backend boundary is a hard wall.** Raw HikerAPI / aiograpi dicts never leave `backends/`. Mappers in `_hiker_map.py` and `_aiograpi_map.py` are the only converters.
- **Lazy backend imports.** `import hikerapi` and `import aiograpi` happen only inside `make_backend(...)`. Missing optional dependencies stay localized and surface with install hints.
- **Retry / backoff lives in one place.** `backends/_retry.py` decorates SDK-method calls inside `HikerBackend`; commands never know retries exist.
- **CDN streaming through a single helper.** `backends/_cdn.py` is the only code that pulls untrusted bytes off the network. Host allowlist, MIME sniff, byte budget, atomic write — every download passes through it.
- **Pagination as `AsyncIterator[T]` + `limit: int | None`.** Every collection method is an async generator. Cursor management lives inside the backend; commands consume one item at a time and stop on `limit`.
- **Identity by `pk`, not username.** Usernames are mutable; `Profile.previous_usernames` accumulates renames. The session caches `username → pk` so a typo fails fast and downstream commands don't re-resolve.

## Errors

`insto/exceptions.py` defines the taxonomy. Every backend error subclasses `BackendError`:

| Exception | Retryable? | User-visible message via `_format_error` |
|---|---|---|
| `ProfileNotFound` | no | `profile not found: @<user>` |
| `ProfilePrivate` | no | `profile is private: @<user>` |
| `ProfileBlocked` | no | `profile blocked: @<user>` (aiograpi) |
| `ProfileDeleted` | no | `account no longer exists: @<user>` |
| `PostNotFound` / `PostPrivate` | no | similarly direct |
| `AuthInvalid` | no | `auth invalid — refresh your token / re-login` |
| `QuotaExhausted` | no, terminal | `HikerAPI quota exhausted` |
| `RateLimited(retry_after)` | yes | sleeps `retry_after` and retries |
| `Transient` | yes | exponential backoff + jitter |
| `SchemaDrift(endpoint, field)` | no | `schema drift in <endpoint>: missing field "<f>"` |
| `Banned` | no | account-level block (aiograpi) |

Commands never `except BackendError` themselves. The dispatcher catches everything at the boundary, runs `_format_error` (which redacts secrets via `_redact.redact_secrets`), and prints a single line. The same redactor runs in the rotating-file logger so stack traces in `~/.insto/logs/insto.log` are also scrubbed.

## Sqlite store

All persistent state lives in one DB at `~/.insto/store.db` (mode `0600`):

```text
_meta             schema_version
cli_history       cmd, target, ts            (90-day retention, indexed on ts)
watches           user, registration_id, interval_seconds, last_ok, last_error,
                  consecutive_errors, status
snapshots         target_pk, captured_at, profile_fields_json, last_post_pks_json,
                  avatar_url_hash, banner_url_hash    (30-day retention, max 100/target)
```

- One `sqlite3.Connection` per session, owned by the facade.
- `asyncio.to_thread` wraps every sync call from async contexts so the event loop never blocks.
- `migrate_to_latest()` runs on startup under `BEGIN IMMEDIATE` so two `insto` processes don't race a schema bump.
- URLs (avatar / banner) are SHA256-hashed before write — diffing checks hash inequality, not the URL.

## Output / export

```text
output/
  <user>/
    info.json
    posts.json
    posts/<pk>.<ext>
    stories/<pk>.<ext>
    highlights/<highlight_pk>/<item_pk>.<ext>
    dossier/<iso_ts>/...     (one self-contained intel package per /dossier run)
  .batch-<sha>.jsonl         (per-input-file resume state)
  .insto-cdn-budget.lock     (per-command 5 GB CDN ceiling)
```

JSON exports are versioned: every file has `{"_schema": "insto.v1", "command": ..., "target": ..., "captured_at": ..., "data": ...}`. CSV is flat rows with no envelope. Maltego CSV uses `Type, Value, Weight, Properties` with Properties JSON-encoded into one column.

`mtime` of every downloaded media file is set from the source's `taken_at` so Photos / Finder sort chronologically.

## Watch

SQLite is the source of truth; the local scheduler is disposable execution
state. `/watch` and `/unwatch` transactionally mutate the registry. A
`WatchDaemon` reconciles it every two seconds into a `WatchManager`, which owns
one task per active target and the only `WatchProcessLock` for that database.

```text
/watch, /unwatch, /watching
          |
          v
 SQLite registry <---------------------------+
          |                                   |
          | reconcile                         | conditional state update
          v                                   | (user + registration_id)
 WatchDaemon -> WatchManager -> diff_and_snapshot
                     |
                     +-- <db>.watch.lock (one executor)
```

One-shot mode only controls persisted rows. REPL mode attempts ownership lazily
and remains control-only when another executor owns the lock. `insto
watch-daemon` acquires before recovery and keeps ownership even with zero rows.
All three entrypoints use `service/runtime.py`, which constructs and closes the
history store, backend, CDN client, facade, manager, and coordinator in one
order-controlled lifecycle.

`registration_id` is an opaque generation token: a stale in-flight tick cannot
update a row that was deleted and re-added. It is never exposed in terminal or
JSON output. New rows without a prior success wait one interval; recovered due
rows start at offsets 0/2/4 seconds. Subsequent polling is fixed-delay, so one
target never overlaps itself.

A tick retries once. Two consecutive failed ticks persist `paused`, including
across restarts; `Banned` and `AuthInvalid` pause immediately. Success clears the
error and counter. Coordinator/storage failures are fatal to the executor and
drain all tasks. SIGINT/SIGTERM shutdown drains reconcile and tick tasks before
clients, sqlite, and the POSIX advisory lock are released.
Concurrent removal and shutdown paths share one drain per watch; entries stay
registered until their tasks finish, so an empty registry cannot release the
executor while another caller is still draining a tick.

The foreground daemon runs best-effort retention at startup and hourly using
`HistoryStore.prune_async()`. Its supervised maintenance task finishes any
started SQLite operation before shutdown closes the store. Retention failures
do not pause watches and are retried at the next interval. Daemon output is
flushed per message so service managers and redirected logs see it immediately.

Limits remain three active watches and a 300-second floor: at the ceiling this
is 36 ticks/hour and an estimated 72-108 backend calls/hour. The lock/signal
implementation is POSIX-only.

### Webhook delivery

The runtime creates a webhook notifier only for persistent REPL and daemon
roles. One-shot mode does not validate or allocate it. Delivery is downstream
of the existing persistence and terminal-output boundaries:

```text
WatchManager -> diff_and_snapshot -> SQLite snapshot
                                |
                                +-> terminal output
                                `-> build event -> bounded webhook retry -> warning only
```

Event conversion permits delivery only for non-empty current `changes`;
historical `previous_usernames` is context and cannot trigger it. The immutable
version-1 payload contains only `schema_version`, `event`, `event_id`,
`username`, `observed_at`, `changes`, and `previous_usernames`. One event id is
reused for every attempt.

The notifier's pooled HTTP client has redirects disabled and `trust_env=False`.
It treats `2xx` as success; transport errors, timeouts, `408`, `429`, and `5xx`
get three total attempts separated by 0.25 and 1 second. Other `4xx` and all
`3xx` responses fail after one attempt. A hard five-second timeout bounds each
attempt, and streamed response bodies are closed without being read or logged.
The endpoint is environment-only, secret-redacted, never persisted, and limited
to HTTPS except for localhost and loopback HTTP.

Delivery is observational: its final failure emits a sanitized warning without
changing successful watch state. This is best-effort rather than transactional
delivery. Receivers deduplicate possible retries with `event_id`; a crash after
the snapshot commit and before delivery can lose an event because there is no
outbox.

## Test strategy

- 900+ unit + integration tests, no live API calls in CI.
- Fixtures: one frozen HikerAPI dict per profile-access state (`public`, `private`, `deleted`, `empty`, `schema_drift`).
- `tests/fakes.py:FakeBackend` implements `OSINTBackend` from fixtures with per-method error injection covering every entry of the error taxonomy.
- E2E flows cover subprocess one-shot, prompt_toolkit REPL, a local watch tick,
  and daemon persistence/lock/SIGTERM/restart behavior with an offline backend.
- Strict mypy + ruff format + ruff lint as CI gates; pytest coverage must stay at or above the current CI floor.
