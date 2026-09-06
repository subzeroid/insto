# Desktop setup protocol

`python -I -B -m insto.desktop` accepts one UTF-8 JSON line on stdin, followed
by EOF, and emits one JSON line. The caller owns the process timeout and must
close stdin after the request. There is no network listener or shell transport.

This private API supports desktop profile setup and macOS service management.
It is not an app installer or a complete GUI. Account management and monitoring
history are separate delivery stages; these operations do not add watches.

## Request and response

The request has exactly `protocol_version`, `request_id`, `operation`, and
`params`. Version is integer `1`, not a boolean. IDs contain 1–64 ASCII letters,
digits, underscores or hyphens. Input is limited to 64 KiB, output to 2 MiB,
including the terminating newline. Duplicate JSON keys and nonfinite numbers
are rejected. JSON must occupy a single line.

```json
{"protocol_version":1,"request_id":"hello-1","operation":"hello","params":{}}
```

`hello` requires empty params. Its result includes `core_version`,
`schema_version_supported`, and the capabilities below. Clients should check
capabilities, not assume every operation exists merely because protocol major
version 1 is supported. The handshake does not load credentials, open a database
or construct a provider.

Responses carry `protocol_version` and `request_id`, plus either `result` or
`error {code, message, retryable}`. Error messages are static and never echo
request values or exception text. Invalid requests have a null request ID;
unsupported protocol versions preserve a validated ID. Once dispatch starts,
errors preserve the decoded ID. `retryable` is advisory: after a transport
timeout or interrupted mutation, inspect state before submitting another change.

## Operations

| Operation | Exact params | Effect |
| --- | --- | --- |
| `hello` | `{}` | Version, supported database schema, implemented capabilities. |
| `setup.inspect`, `settings.inspect` | `{}` | Read profile, pending recovery, cached quota and observed service state. |
| `setup.configure` | `{"token":"..."}` | Validate and save first credentials; initialize the database and service. |
| `credentials.replace` | `{"token":"..."}` | Validate a candidate and replace credentials with durable rollback. |
| `service.start` | `{}` | Persist running intent and start the owned service. |
| `service.stop` | `{}` | Persist stopped intent, disable and unload; retain config, database and service files. |
| `service.repair` | `{}` | Reconcile pending recovery, or bring an owned service to its saved intent. |

Tokens contain 4–4096 ASCII characters in the range 33–126: no spaces or control
characters. The minimum matches the redactor's secret registration floor.
Extra keys, including paths, backend selection and native arguments, are rejected
before profile or operation loading. There is no production fake-provider option.

Setup/settings and successful mutation results contain `configured`, `status`,
`desired_service`, `service_running`, `quota_remaining`, `quota_checked_at`, and
`revision`. Unconfigured fields are null where appropriate. Status is one of
`unconfigured`, `recovery_required`, `quota_exhausted`, `running`, `stopped`, or
`service_error`. Ownership/schema failures use an error response rather than an
apparently healthy empty profile. Tokens, token fingerprints, raw native output,
paths and exception details never appear in these results.

Quota is the last explicit credential validation result, with a Unix-seconds
timestamp, not a live balance. `service_running` describes the matching native
process/executor lock, not end-to-end monitoring health. Zero quota can be saved
but always reports `quota_exhausted`, even with a running local service.

Read operations do not initialize directories, migrate databases, change service
intent, construct providers or make provider requests. Existing WAL-backed schema
checks use a private disposable snapshot to avoid changing source sidecars;
concurrent database changes fail closed and may require a fresh inspection.

## Errors

| Code | Meaning |
| --- | --- |
| `invalid_request` | Invalid JSON, envelope, field type, ID or input budget. |
| `unsupported_protocol` | A different integer protocol version was requested. |
| `unsupported_operation` | The operation is not implemented. |
| `invalid_params` | The operation received unsupported parameters. |
| `internal_error` | The operation or response serialization failed. |
| `invalid_token` | Provider rejected the candidate credentials. |
| `quota_exhausted` | Provider rejected access due to exhausted quota. |
| `rate_limited`, `network_error`, `access_unconfirmed` | Validation did not establish access; no candidate is saved. |
| `operation_timeout` | The budget expired; inspect before retrying. |
| `profile_busy` | Another profile operation holds the lock. |
| `profile_ownership` | Profile paths, permissions or ownership cannot be trusted. |
| `not_configured`, `already_configured` | Setup state does not match the requested operation. |
| `recovery_required` | A protected incomplete transition needs reconciliation. |
| `service_error`, `storage_error`, `schema_mismatch` | Service, private storage or database preflight failed. |
| `unsupported_platform` | Desktop service management requires macOS. |
| `home_invalid` | The path, its ownership or its contents cannot be used as a profile home. |
| `home_backend_unsupported` | The home's backend is not hikerapi. |
| `service_ownership_unknown` | Manifest, plist or job ownership cannot be proven; mutations refuse. |
| `service_config_mismatch` | The registration's operational pins differ from the home's configuration; migrate refuses. |

`profile_busy`, `rate_limited`, `network_error` and `access_unconfirmed` are marked
retryable. Other errors are not. No exception text is forwarded to the client.

A protocol error is a JSON envelope with exit 0. A nonzero exit, malformed
stdout or caller timeout is a transport failure, not a successful operation.
The caller must independently bound stdout/stderr and process lifetime; a
10-second deadline is sufficient for `hello`. Credential operations have one
120-second composite budget including validation, with 35 seconds reserved for
rollback. Provider validation and close share a maximum 30-second wait. Started
native workers drain before management/profile locks release. Cancellation-
resistant third-party tasks or synchronous blocking still require the caller's
hard process deadline; a JSON error alone is not proof that the child exited.
Child stderr is not a UI message.

If a kickstart client times out, the controller does not repeat the command: it
observes the owned service within the remaining budget. Only verified native
running state and the matching busy executor lock can turn that uncertain result
into success. Startup/termination transitions alone are never readiness.

## Profile ownership and recovery

The trusted launcher supplies an absolute canonical `INSTO_DESKTOP_ROOT`, or
the default is `~/Library/Application Support/insto-gui`. The fixed `profile/`
child belongs only to this desktop installation. The application root's parent
must already exist. JSON cannot select another home, and `INSTO_HOME` is ignored.
External CLI-home adoption and interpreter migration are not repair operations.

The application root/profile are private (0700); config, ownership state, journal,
backup and locks are private owned ordinary files (0600). Symlink, hardlink,
foreign-ownership and unsafe writable-ancestor cases are refused. An unmarked
populated profile is not adopted. Atomic publication stages are equally private
and may remain in the application root after a crash; they are never treated as
committed credentials or recovery authority. Do not manually rename them into
the profile. The token is stored in private TOML, not Keychain in this stage.

First setup publishes a journal, validated config and bound state before service
startup. A startup failure retains the validated config so Repair can finish
local setup without another provider request. Repeating the same token validates
it again but does not unnecessarily restart the service; a different token must
use credential replacement.

Replacement validates before disk/service changes, then holds the profile lock
and service management lock. It durably saves the previous config and journal,
confirms the service is stopped, and holds the idle executor lock while publishing
credentials. Only a previously running service restarts; a stopped or already
cleanly exited service stays nonrunning. Failure/crash recovery stops any candidate
before restoring the exact old config/state, and never restores beneath an
unconfirmed executor. Missing required backup data cannot become successful recovery.

Terminal `committed`/`rolled_back` records only finish cleanup. Repair with pending
recovery reports the reconciled state; explicit Start is a separate action. With
a nonterminal journal, Stop returns `recovery_required` before native work rather
than starting a service as a recovery side effect: repair first, then stop.
Reopening the client never changes persisted stopped intent. Ordinary Start/Repair
can kickstart an exactly owned loaded-but-cleanly-exited job without forced restart.

## Strict HikerAPI access primitive

`HikerBackend.validate_access()` is backend-only. It uses the existing HTTP
transport and retry policy to check `/sys/balance`, returning a `Quota` with a
nonnegative integer `remaining`. Zero confirms an exhausted balance, not a
healthy monitor. Invalid credentials, quota/rate limits, temporary failures and
unknown response schemas remain errors. Non-success HTTP responses, including
redirects with balance-shaped JSON, never confirm access. Balance 403/404 are unconfirmed access,
not Instagram profile banned/not-found results.

The existing soft `refresh_quota()` behavior is unchanged. Desktop validation
registers a candidate before client construction, uses the packaged provider host
and an HTTP transport with environment lookup disabled, and attempts close once
on every constructed-client outcome. Inherited provider token/host/backend and
proxy/CA settings do not override that explicit validation client.

## Service compatibility

Installations initiated by an interpreter with bytecode writes disabled preserve
that setting in their LaunchAgent: `python -I -B -m
insto.service.watch_service_runner <manifest>`. Ordinary CLI installations retain
the legacy launch form without `-B`.

An existing registration must match the requested form exactly for installation
to be a no-op. Switching interpreter mode does not silently replace its plist.
Removal accepts either exact owned form, regardless of the uninstalling
interpreter's bytecode setting; other ownership checks remain in force.

This foundation neither bundles Python nor installs an app. Portable-runtime
and installed-app verification are separate delivery stages.

## C2a local watch operations

The original eight C1 capabilities remain supported. New capabilities are
overview, watches.list, watches.add, watches.update, watches.pause,
watches.resume and watches.remove. They never validate a token or contact HikerAPI.

overview takes empty params and returns configured, desired_service,
service_state (running/stopped/unknown), quota_remaining, quota_checked_at,
watches and next_cursor. The quota fields are the saved balance and its saved
check time (UTC Unix epoch seconds), not a live provider balance. Both are null
before configuration. Display them as the last confirmed balance; a local
overview refresh does not update either value or call the provider.
It reads at most one watch page and inspects the owned service after closing the
SQLite transaction. Unknown service state is not treated as stopped or healthy.

watches.list accepts optional limit (integer1..50, default50) and cursor. It returns
items and next_cursor. Three is the active-watch cap, not the total-list limit.
Following cursors does not promise a database snapshot across requests.

watches.add accepts user and optional interval_seconds (default300) and only
creates an absent watch. Existing paused watches require explicit resume.
update requires user/revision/interval_seconds. pause, resume and remove require
user/revision. Input usernames are canonicalized and bounded to255 ASCII
characters; intervals are integer seconds300..2147483647, never booleans/floats.

A watch DTO contains user, status, interval_seconds, last_ok (UTC epoch seconds or
null), waiting_first_check, has_error, consecutive_errors and an opaque revision.
It contains neither the internal generation identifier nor raw historical errors.
Mutation returns watch, except remove returns removed_user and preserves snapshots.

Stale revisions fail as watch_conflict. A missing row gives watch_not_found,
duplicate add watch_exists, and exceeding three active rows watch_limit.
No-op state/interval commands keep revision. Real state/interval changes rotate
generation and fence late daemon status writes in the same SQLite transaction.
Pausing cannot undo a snapshot already committed by a running tick.

Read operations never initialize profiles, take a profile lease, change config,
migrate schema, copy all history or invoke service mutations. SQLite may maintain
normal private WAL/SHM files. WAL can contain committed data and is never manually
deleted or ignored by the reader. C1 check_database retains its stricter
source-file behavior. Busy/recovery/ownership/schema failures remain explicit.

Local read and mutation budgets are10seconds, including validation, SQLite and DTO
work. Busy waits are at most1second. Timeout or transport loss after a mutation
requires reading current state; do not automatically retry a non-idempotent add.

## C2b saved history operations

The additional capabilities are `snapshots.targets`, `snapshots.list`,
`snapshots.compare` and `changes.list`. They inspect saved SQLite snapshots only.
They never construct a provider, call live `/diff` or command `/history`, start a
scheduler, download historical media, change a watch, or prune the database.

Exact parameters:

| Operation | Required | Optional |
| --- | --- | --- |
| `snapshots.targets` | `username` | `limit`, `cursor` |
| `snapshots.list` | `target_pk` | `limit`, `cursor` |
| `snapshots.compare` | `target_pk`, `older_id`, `newer_id` | none |
| `changes.list` | none | `target_pk`, `limit`, `cursor` |

Username accepts one optional leading `@`, followed by 1–255 ASCII letters,
digits, periods or underscores; `.` and `..` and whitespace are rejected.
It is normalized to lowercase without `@`. This matches C2a's protocol storage
bound and does not assert provider existence. G1 passes the canonical username
returned by watch operations; history itself rejects raw whitespace. `target_pk` is a canonical positive
decimal string of at most 64 digits. Snapshot IDs are canonical positive decimal
strings through `9223372036854775807`; JSON numeric IDs and leading zeroes are
rejected. `limit` is an actual integer from 1 through 50, default 50. Optional
keys are omitted when unused, not null. Extra keys and invalid cursor bindings
return `invalid_params` before profile or history loading.

Pages contain `items`, `next_cursor`, `scan_complete`, and `scanned`. Cursors are
opaque, canonical, unpadded base64url strings of at most 1,024 characters. They
bind protocol-internal cursor version, operation, normalized filter, initial
maximum snapshot ID, and descending `(captured_at,id)` frontier. A caller may
change the page limit while following a cursor, but may not change its filter
or operation. New normal AUTOINCREMENT rows are excluded from that traversal,
including rows inserted with an older timestamp. Retention can remove rows
between requests. An exact-limit page can conservatively have a cursor whose
next page is empty and complete.

Snapshot metadata is `{id,target_pk,captured_at}`. `id` and `target_pk` are always
strings. `captured_at` is UTC Unix epoch seconds, from 0 through 253402300799;
the GUI renders local time. Every operation orders by `(captured_at,id)`, with
newest first for pages and increasing order for an explicitly selected pair.

`snapshots.targets` canonicalizes `username` exactly like `watches.add` and the
CLI, in the same order: leading `@` characters are removed first, then surrounding
whitespace, then the value is lowercased before the 255-character bound applies,
so one GUI field means one thing everywhere (a space before `@` stays invalid). It returns `{kind:"target",target_pk,snapshot}` historical
evidence from saved username values. Deduplication is page-local. The caller
unions PKs across all pages and retains warnings. One result before
`scan_complete` never establishes unique identity. Even after completion,
diagnostic rows mean the evidence is incomplete: no exhaustive zero/one-PK
claim is permitted. A missing/null old username produces the result-local
diagnostic `history_identity_unknown`. Multiple PKs require the user to select
the desired saved history; a newest match is never chosen automatically.
Renaming an account does not rename its watch registration.

`snapshots.list` returns `{kind:"snapshot",snapshot}` metadata only, or a safe
diagnostic for an unreadable saved record. Profile fields are checked within
the raw byte cap but are not included in list items. An empty list means no
retained snapshots for that PK; one snapshot is an initial retained baseline.

`snapshots.compare` returns `{kind:"comparison",older,newer,changes,unknown_fields}`.
`changes` contains `{field,old,new}` for known values that differ. The field set
is the existing tracked profile fields plus `avatar` and `banner` stored hashes.
Absent fields are listed in `unknown_fields`; explicit JSON null is a known
value. Missing selected IDs return `snapshot_unavailable`, prompting a list
refresh. A different PK returns `snapshot_identity_mismatch`; reversed/equal
pair ordering returns `invalid_params`. The API does not manufacture a prior
snapshot from null.

`changes.list` compares each candidate with its immediately preceding retained
snapshot within the same PK and initial ID ceiling. Its earliest retained
snapshot is `{kind:"baseline",snapshot}`. Fully known unchanged comparisons
are omitted. A comparison with any unknown field has `kind:"incomplete"` and
the same `older,newer,changes,unknown_fields` shape. Other changed pairs have
`kind:"comparison"`. These are observations between capture times, not exact
Instagram event times; follower counts do not identify individual followers.

Paginated JSON failures appear as `{kind:"diagnostic",snapshot,code}` with
`history_corrupt` or `history_oversized`, and the cursor can advance past them.
Invalid identity/order metadata fails the operation with `history_corrupt`
because a safe continuation cannot be constructed. Pair-read JSON failures
are static errors with the same code. Raw JSON and exception text are never
included in diagnostics. A malformed snapshot is not silently an empty result.

Each row's two raw JSON columns together may occupy at most 65,536 UTF-8 bytes.
SQL uses `length(CAST(... AS BLOB))` and CASE projection to suppress oversized
columns before they reach Python. Duplicate keys, NaN/infinity, wrong scalar
types, invalid identifiers and count coercions are rejected. Batches contain
at most 16 raw rows. The feed selects at most 200 candidates per request plus
one predecessor lookup per inspected candidate; username discovery selects at
most 2,000 candidates. `scanned` counts inspected candidates, including a row
deferred by the byte budget; the cursor advances only through completed rows.
An unchanged feed page can have no visible items and still have continuation.
No page contains more than 50 visible items.

All local reads share a 10-second deadline started before parameter validation,
with SQLite busy timeout at most one second and a progress handler. Decoding,
comparison and encoding also check that deadline. Timeout discards a partial
page and returns `operation_timeout`. The deadline covers reading, assembling and
byte-budgeting the page; the transport's final serialization of an already
complete result is bounded by the same 2 MiB budget and is deliberately not
interrupted, so a response may arrive a few milliseconds after the deadline but
is never partial. Bounded pages reserve worst-case envelope
and cursor bytes and count actual ASCII JSON escaping; the entire response,
including request ID and newline, is strictly below 2 MiB. A full byte budget
shortens the page with continuation instead of becoming `internal_error`.

C2 uses its separate trusted `mode=ro`, `query_only`, explicit-read-transaction
path with schema validation in the same transaction. It never creates profile
directories or application locks, migrates schema, calls C1 `check_database`,
changes settings, or repairs ownership. SQLite may normally maintain its own
private WAL/SHM files. WAL can contain committed data and is never deleted or
ignored. Sidecars do not prove a daemon is running. Main-file byte invariance
is tested without a concurrent writer; concurrent tests check consistency and
transaction release. C1 setup/settings checks keep their existing separate
copying and byte-invariance behavior.

The four added static error codes are `history_corrupt`, `history_oversized`,
`snapshot_unavailable`, and `snapshot_identity_mismatch`, all non-retryable
without a user decision or refreshed selection. SQLite contention propagates
the shared reader's `profile_busy`; ownership/config/schema/recovery failures
remain explicit. The existing retention stays 30 days and at most 100 snapshots
per PK; the count cap can shorten that period. There is no event archive, cache,
new schema version or additional index in C2b.

## C3 migration and adoption

Capabilities become the 19 C2 names plus service.inspect, service.migrate,
service.uninstall, home.inspect and home.select (24). The core is insto 0.7.22.

| Operation | Exact params | Budget | Effect |
| --- | --- | --- | --- |
| `service.inspect` | `{}` | read, 10 s | Registration facts for the current profile: `registration` (none/owned/unknown), `interpreter` (current/other/null), `interpreter_exists`, `loaded`, `settings` (matching/different/null: whether the registered pins equal what this interpreter would register for the home's configuration). Never changes files or jobs. |
| `service.migrate` | `{}` | mutation, 120 s | Rewrite an owned registration into exactly the form this interpreter would register (interpreter and launch form), with rollback; no-op when the registration already is that form, or nothing is registered. Returns the profile DTO. |
| `service.uninstall` | `{}` | mutation, 120 s | Persist stopped intent, then remove the exact owned registration (plist first, then manifest) once the job is unloaded and no executor runs; config, database, state and history are kept. Needs no credentials or database. |
| `home.inspect` | `{"path":"..."}` | read, 10 s | Read-only report on an external home (below). Never creates, changes or locks anything. |
| `home.select` | `{"path":"..."}` or `{"path":null}` | mutation, 120 s | Bind the desktop to that home, or back to its own profile. Returns the profile DTO of the new binding. |

The desktop root may contain `desktop-home.json` (`schema_version` 1,
`managed_by`, `uid`, absolute canonical `home`). While it exists, the profile
is that home: config is its `config.toml`, the database is the one its config
resolves to, desktop intent is `home/desktop-state.json`, and recovery files
keep their names inside it. The desktop never migrates or chmods an adopted
directory and never touches its other files; `home.select` creates only
`desktop-state.json` (quota fields null, `desired_service` running when the
home's registration is loaded and running), `.desktop.lock` (the home's own
lease, so two desktop roots never write one home at once), a missing database
with the current schema, and the `services/watch` directories the service
controller needs (the same ones the CLI's install creates). An adopted home
must sit under trusted parents (owned by root or the user, not world-writable
unless sticky), exactly like the desktop root. Relative paths in the home's
config resolve against the home, the service's working directory; an absolute
`db_path` outside the home is honoured (the CLI's own semantics) and its file
must pass the same private-file checks as any profile database.
Configured adopted profiles report null quota fields until the next credential
validation; `quota_exhausted` applies only to a saved zero.

`home.inspect` returns `path`, `exists`, `private`, `config` (ok/missing/
invalid), `backend` (hikerapi/aiograpi/fake/null), `database` (ok/missing/
schema_mismatch/unreadable), `registration`, `interpreter`, `loaded`,
`process` (running/stopped/unknown), `adoptable` and `reason` (a static error
code or null). `adoptable` requires a private owned real directory, `config`
ok, `backend` hikerapi and `database` ok or missing. Nothing inside a
non-private path is read: such a report says invalid, unreadable and unknown.
Paths are absolute or `~`/`~/…` (this account's home, expanded by the core),
at most 1024 UTF-8 bytes, no NUL, normalized and free of symlinks; anything
else is `home_invalid` (`invalid_params` for a wrong type, empty or oversized
value) before any read. The config file must be exactly what the profile
reader accepts later (regular, owned, mode 0600, one link), otherwise
`config` is `invalid`. Tokens never appear in the report.

`home.select` refuses `recovery_required` while the current profile has a
pending journal or backup, and is a no-op for the current binding. Before
switching away from the own profile it stops the desktop-owned service and
persists stopped intent; an adopted home's service is never touched by
selection, so a CLI-registered service keeps running until `service.migrate`
takes it over. Returning to the own profile never starts its service.

`service.migrate` is a journaled transaction (kind `migrate`): both sides
(previous and candidate bytes) are retained in one durable document beside
the registration, the old job is stopped, the registration is replaced with
the calling interpreter's, and the job is started only when saved intent is
running. Every publication and removal is followed by a directory fsync, so a
durable journal phase never claims a file that did not survive. A migration
keeps every operational pin (backend, database, output, session and env-file
paths) byte-for-byte; a registration whose pins differ from the home's
current configuration is refused with `service_config_mismatch` before
anything is stopped. Any failure or crash rolls back to the retained bytes and
the previous intent; `service.repair` finishes an interrupted rollback and
never completes a migration forward, and it touches only bytes recorded by the
migration (previous, candidate, or the mixed pair a death between the two file
replacements leaves) — anything else is `service_ownership_unknown` and stays
untouched. A registration whose manifest, plist or loaded job (program and
arguments) cannot be proven owned is `service_ownership_unknown` for every
mutation; reads still describe it. A manifest without its plist is owned but
incomplete: migration completes it, uninstall removes it, and a loaded job
without its plist is unknown.

Every operation re-validates the binding after taking the profile lock: a
request resolved against one home while another process selected a different
one fails with `profile_busy` (retryable) instead of acting on the wrong home.

`setup.inspect`/`settings.inspect` keep their C1 meaning: while the
registration names another interpreter (a CLI-registered service, or a
migration that was rolled back) they report `service_error`, because the
owned lifecycle cannot manage that registration; `service.inspect` shows
`interpreter: other` so a client can offer migration instead of repair.
`service.repair` after such a rollback returns the same `service_error` DTO
once the journal is settled. `home.inspect` inspects a WAL-backed database
through a private disposable copy inside its 10-second budget; a very large
uncheckpointed store can therefore report `operation_timeout`, which is not
an adoption verdict.

New static codes: `home_invalid`, `home_backend_unsupported`,
`service_ownership_unknown` and `service_config_mismatch` (none retryable).
Existing codes keep their meaning; adopted homes with an incompatible database
report `schema_mismatch`.
