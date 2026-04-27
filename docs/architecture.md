# Architecture

Six layers, top to bottom. The rule that holds the design together: each layer talks DTOs to the layer below, never raw API dicts.

```text
UI:        REPL (prompt_toolkit) │ one-shot CLI (argparse)
Dispatch:  parse → validate → run → render
Commands:  commands/{target,profile,media,network,content,interactions,batch,watch,operational,dossier}.py
Service:   facade · history · analytics · exporter · watch
Backends:  OSINTBackend ABC · HikerBackend (v0.1) · AiograpiBackend (v0.2)
Models:    @dataclass(slots=True) DTOs — Profile, Post, Story, User, Comment, Quota, ...
```

## Conventions

- **Async everywhere.** `httpx` (transitive via `hikerapi`), `asyncio` for fan-out, `asyncio.to_thread` for sqlite calls.
- **Backend boundary is a hard wall.** Raw HikerAPI / aiograpi dicts never leave `backends/`. Mappers in `_hiker_map.py` (and the future `_aiograpi_map.py`) are the only converters.
- **Lazy backend imports.** `import hikerapi` happens only inside `make_backend("hiker")`. v0.2's `import aiograpi` will be the same. Import errors stay localized.
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
_meta             schema_version, last_migrated_at
cli_history       cmd, target, ts            (90-day retention, indexed on ts)
watches           user, interval, last_ok, last_error, paused
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

Session-only in v0.1 (daemon mode is v0.2). `/watch <user> <interval>` registers an `asyncio.Task` on the same loop that runs `PromptSession.prompt_async()`. Each tick is wrapped in `asyncio.shield(...)` and a single retry; two consecutive failures mark the watch `paused`. Notifications go through `prompt_toolkit.patch_stdout` so the user's in-progress input line is not corrupted.

Session limits: max 3 active watches, 5-minute floor on the interval, all watches cancelled cleanly on REPL exit.

## Test strategy

- 700+ unit + integration tests, no live API calls in CI.
- Fixtures: one frozen HikerAPI dict per profile-access state (`public`, `private`, `deleted`, `empty`, `schema_drift`).
- `tests/fakes.py:FakeBackend` implements `OSINTBackend` from fixtures with per-method error injection covering every entry of the error taxonomy.
- 3 e2e flows under `tests/e2e/`: subprocess one-shot, prompt_toolkit pty REPL session, `/watch` tick with `patch_stdout` capture.
- Strict mypy + ruff format + ruff lint as CI gates.
