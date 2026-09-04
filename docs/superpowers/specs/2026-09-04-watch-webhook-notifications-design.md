# Watch Webhook Notifications Design

**Date:** 2026-09-04

**Status:** approved; engineering review complete

## Summary

Add an opt-in generic webhook sink to the existing persistent watcher. The
process that owns the SQLite watcher lock sends one JSON request when a
successful tick finds a real account change. Terminal output remains the
default behavior, and webhook delivery can never change watcher state or undo
an already-persisted snapshot.

## Goals

- Deliver useful watcher changes outside the foreground terminal.
- Keep configuration safe for shells and service managers.
- Give receivers a small, versioned, stable JSON contract.
- Bound network latency and retries.
- Preserve the current scheduler, persistence, and terminal behavior when the
  feature is disabled or delivery fails.

## NOT in scope

- Per-watch endpoints or notification preferences.
- A persistent delivery outbox or guaranteed exactly-once delivery.
- Email, chat-specific, push, or shell-command transports.
- Signing, authentication headers, or webhook management commands.
- Storing the webhook URL in SQLite or accepting it as a CLI argument.
- Installing or managing system services.
- Following ambient proxy or private-CA environment settings.
- Honoring `Retry-After` beyond the fixed retry delays; deterministic bounded
  tick latency wins for this low-volume first slice.

## What already exists

- `insto.config` already resolves environment values, records their origins,
  registers secrets centrally, and renders safe `/config` rows. The feature
  extends that flow instead of creating a second configuration loader.
- `OsintFacade.diff_and_snapshot` already persists a snapshot before returning
  its structured diff. Webhook delivery consumes that result rather than
  fetching or storing account data again.
- `open_runtime` already owns ordered construction and teardown for one-shot,
  REPL, and daemon resources. It will own the notifier too.
- `WatchManager` and `WatchDaemon` already guarantee one executor per database,
  cancellation of in-flight ticks, retry-safe watcher state, and best-effort
  terminal output. Notification delivery does not duplicate those concerns.
- HTTPX is already a direct dependency and provides the async pooled client and
  mock transport needed here. The backend-specific retry helper is not reused
  because its exception taxonomy and delay policy are intentionally different.

## User interface and configuration

`INSTO_WATCH_WEBHOOK_URL` enables webhook delivery. It is intentionally
environment-only in this slice: service managers can inject it without putting
it in the command line, SQLite registry, or the project's config file. An empty
value is treated as disabled.

Long-running runtime initialization validates a configured endpoint before it
acquires the watcher executor lock. The URL must be absolute, have a host, have
no fragment, and use HTTPS. Plain HTTP is accepted only for `localhost` or a
loopback IP so local receivers and tests remain possible. Invalid configuration
fails startup with an actionable message that never includes the URL. One-shot
commands do not construct or validate the notifier because they never deliver
watch events.

The resolved `Config` contains `watch_webhook_url: str | None` and records its
origin as `watch.webhook_url = env | default`. The URL is registered with the
central secret redactor as soon as it is loaded. `/config` reports the key as
`configured` or `disabled`; it never prints any part of the URL.

The feature is active in whichever long-running process owns execution: a REPL
that acquired the watcher lock or `insto watch-daemon`. One-shot commands never
send watcher notifications.

## Components

### Event conversion

`insto/service/watch_webhook.py` owns the outbound contract. A pure
`build_watch_event(username, diff, *, event_id, observed_at)` function returns
`None` when `first_seen` is true or `changes` is empty. Otherwise it returns the
versioned payload. `previous_usernames` is payload context only; the existing
diff contract returns the complete alias history on later unchanged ticks, so
that list must never trigger an event by itself.

Keeping conversion pure makes filtering and payload compatibility independent
of networking and the scheduler.

### Delivery

`WebhookNotifier` owns an `httpx.AsyncClient`, the configured endpoint, retry
classification, and delay policy. It posts JSON with redirects disabled and a
hard five-second wall-clock deadline around each attempt. It opens the response
in streaming mode, inspects only the status and headers, and closes it without
reading the body. The runtime owns the notifier lifecycle and closes its client
during normal or exceptional teardown.

The webhook client uses `trust_env=False`. Ambient `HTTP_PROXY`, `HTTPS_PROXY`,
`ALL_PROXY`, `NO_PROXY`, and CA override variables therefore cannot silently
change the delivery route or trust boundary. The existing backend proxy remains
provider-specific and is never reused for webhook delivery. Explicit webhook
proxy and private-CA support are deferred until a concrete deployment needs
them.

One event receives one UUID event id. The same id and payload are reused across
all attempts for that event, allowing receivers to deduplicate ambiguous
network outcomes.

### Runtime integration

The existing runtime tick remains responsible for `diff_and_snapshot`. After
that call has persisted the new snapshot, it continues to emit the existing
terminal line. If a webhook is configured, the runtime converts the diff to an
event and attempts delivery.

The delivery call is wrapped at the integration boundary. Exhausted delivery
errors produce one redacted watcher-output warning and are not re-raised. The
`WatchManager` therefore records the backend tick as successful, clears its
backend error streak, and does not pause the watch.

## Data flow

```text
INSTO_WATCH_WEBHOOK_URL
          |
          v
       Config -------------------- role=oneshot -----------------> disabled
          |
          +-- role=repl|daemon --> validate URL --> WebhookNotifier
                                                     |
SQLite watch --> WatchManager tick --> diff_and_snapshot --> committed snapshot
                                                     |
                                                     +--> terminal output
                                                     |
                                                     +--> changes empty --> stop
                                                     |
                                                     +--> build event --> POST attempt
                                                                         |-- 2xx: done
                                                                         |-- retryable: wait/retry
                                                                         `-- final/permanent: safe warning
```

## Payload contract

Each request has `Content-Type: application/json` and this shape:

```json
{
  "schema_version": 1,
  "event": "watch.changed",
  "event_id": "550e8400-e29b-41d4-a716-446655440000",
  "username": "target",
  "observed_at": "2026-09-04T04:30:00Z",
  "changes": {
    "biography": {"old": "before", "new": "after"}
  },
  "previous_usernames": ["old_target"]
}
```

`observed_at` is generated in UTC after the snapshot succeeds. `changes` and
`previous_usernames` preserve the existing diff semantics. The payload contains
no backend credentials, proxy values, local paths, or webhook URL. Account
fields in a diff may still be sensitive, so documentation tells operators to
choose an endpoint they trust.

## Delivery and failure semantics

- HTTP `2xx`: success, no further attempts.
- Transport errors, timeouts, `408`, `429`, and `5xx`: retry up to three total
  attempts with delays of 0.25 seconds and 1 second.
- Other `4xx` and `3xx`: fail immediately without retry.
- `asyncio.timeout(5)` bounds each complete attempt rather than relying only on
  HTTPX's per-operation inactivity timeouts. Three timed-out attempts plus both
  retry delays therefore take no more than 16.25 seconds.
- Redirects are not followed, preventing the configured endpoint from silently
  forwarding the payload elsewhere.
- Response bodies are never buffered, included in watcher output, or logged.
- After the final failure, emit one sanitized summary containing the target and
  failure class/status, not the endpoint or response body.
- Cancellation propagates immediately so SIGINT/SIGTERM shutdown is not delayed
  by retries.

Delivery is best-effort. A success response whose connection result is lost may
produce a duplicate retry; `event_id` is the receiver's deduplication key. A
process crash after snapshot persistence but before delivery may lose that
notification. A durable outbox is explicitly outside this slice.

## Security

- The endpoint is read only from the environment and never persisted.
- The full value is registered with the existing redaction system.
- No CLI flag exposes it through process listings or shell history.
- Non-loopback endpoints require HTTPS, while malformed URLs and fragments fail
  before watcher execution starts.
- Ambient proxy and CA environment variables are ignored by the webhook client.
- Redirect following is disabled.
- Error reports omit URLs and response bodies.
- The existing secure config and SQLite permissions remain unchanged.

## Testing

- Config tests cover unset, empty, and configured environment values, origin
  reporting, redactor registration, and proof that a TOML `[watch]` value does
  not enable this environment-only option. The actual `/config` command is
  tested in terminal and JSON modes; both show only `configured` or `disabled`.
- Shared test and E2E environment fixtures explicitly remove
  `INSTO_WATCH_WEBHOOK_URL` before each test so a developer or CI environment
  cannot make unrelated tests send external requests.
- URL tests cover absolute HTTPS, loopback HTTP, missing hosts, unsupported or
  insecure schemes, fragments, safe error text, and one-shot non-validation.
- Pure event tests assert the exact payload allowlist, first-snapshot and
  no-change suppression, field changes, aliases as context, stable event ids,
  and UTC timestamps. A regression case covers an unchanged profile with
  historical aliases and proves that no event is built.
- `httpx.MockTransport` tests cover all `2xx`, retryable transport failures,
  `408`, `429`, and `5xx`, mixed retry then success, immediate `3xx` and other
  `4xx` failures, exact attempts and delays, and reuse of the identical payload
  and event id. They also prove redirect refusal and `trust_env=False`.
- Delivery cancellation is tested separately while a request is pending and
  while each retry sleep is pending. Deadline coverage includes a response that
  never completes, and streaming coverage proves a large body is not buffered.
- Runtime tests cover disabled and configured one-shot roles, proving no notifier
  client is allocated for one-shot use. Long-running tests cover first,
  unchanged, changed, and failed-backend branches.
- A runtime ordering spy observes the database from inside notifier delivery and
  proves `snapshot committed -> terminal output -> delivery`. Delivery failure
  and warning-output failure remain observational: the watch stays active with a
  successful timestamp, zero backend errors, and no stored delivery error.
- Runtime teardown tests close notifier resources after normal execution,
  partial construction, coordinator failure, and cancellation.
- The POSIX daemon subprocess test uses a loopback HTTP server and a pre-seeded
  changed snapshot to prove a configured daemon emits exactly one JSON POST and
  exits cleanly on SIGTERM. Distinct endpoint and response-body sentinels must
  not appear in stdout, stderr, or the rotating log.
- Full pytest, Ruff, strict mypy, build, and distribution-content checks remain
  release gates.

## Test coverage map

```text
CODE PATHS                                      USER FLOWS
[PLAN ★★★] Config                               [PLAN ★★★] Enable notifications
  |-- unset / empty -> disabled                   |-- set environment variable
  |-- env -> configured + redacted                |-- start REPL or daemon
  |-- TOML value -> ignored                       `-- receive one change event
  `-- invalid URL -> startup error              [PLAN ★★★] No false alerts

[PLAN ★★★] Runtime                               |-- first snapshot -> none
  |-- one-shot -> no notifier                    |-- unchanged -> none
  |-- backend failure -> no delivery             `-- historical aliases only -> none
  |-- snapshot -> output -> delivery            [PLAN ★★★] Delivery failure
  |-- delivery failure -> healthy watch          |-- one safe warning
  `-- normal/error/cancel -> close                `-- snapshot and watch stay healthy

[PLAN ★★★] HTTP                                 [PLAN ★★★] Daemon lifecycle [-> E2E]
  |-- 2xx -> success                             |-- isolated env + loopback receiver
  |-- transport/408/429/5xx -> retry             |-- exactly one JSON POST
  |-- 3xx/other 4xx -> fail once                 |-- endpoint/body absent from output/log
  |-- hard deadline / no body buffering          `-- SIGTERM exits cleanly
  `-- cancellation in request/sleep -> propagate

PLANNED COVERAGE: 26/26 branches specified | E2E: 1 | EVAL: none
```

Legend: `★★★` covers behavior, boundaries, and error paths. No LLM or prompt
surface changes, so an eval suite does not apply.

## Failure modes

| Path | Production failure | Test | Handling | User-visible result |
|------|--------------------|------|----------|---------------------|
| Configuration | Typo, insecure remote HTTP, missing host, or fragment | URL matrix | Reject before executor lock | Clear safe startup error |
| Event conversion | Historical alias retriggers an unchanged event | Alias regression | Require non-empty current `changes` | No false notification |
| Backend tick | Profile fetch or snapshot fails | Runtime failure path | Existing retry/pause state machine | Existing watcher error |
| Delivery | DNS, connect, read, write, `408`, `429`, or `5xx` | Retry matrix | Three bounded attempts | One sanitized warning |
| Permanent response | Redirect or non-retryable `4xx` | Status matrix | Stop after first attempt | One sanitized warning |
| Hostile response | Slow-drip or huge response body | Deadline/stream tests | Hard deadline; never read body | One sanitized warning |
| Shutdown | Signal arrives during request or retry sleep | Two cancellation tests | Propagate cancellation and close client | Clean prompt exit |
| Best-effort gap | Process exits after snapshot but before successful delivery | Ordering test documents boundary | Snapshot remains durable; no outbox | Warning when process survives |
| Environment | Ambient proxy reroutes sensitive payload | Client construction test | `trust_env=False` | Direct configured route only |
| Test isolation | Parent shell contains a real endpoint | Fixture regression | Remove env by default | No accidental external POST |

No path is both untested, unhandled, and silent. The accepted best-effort crash
window is explicit rather than hidden.

## Worktree parallelization strategy

| Step | Modules touched | Depends on |
|------|-----------------|------------|
| Configuration boundary | `insto/config.py`, config/command tests | - |
| Event and HTTP delivery | `insto/service/watch_webhook.py`, focused tests | - |
| User documentation | README and selected docs | Approved payload contract |
| Runtime integration | `insto/service/runtime.py`, runtime tests | Configuration + delivery |
| Daemon E2E | E2E fixtures and daemon test | Runtime integration |

Lane A runs configuration work. Lane B runs event and delivery work. Lane C
updates the three selected documents from this contract. Launch A, B, and C in
parallel worktrees; merge A and B before the sequential runtime and daemon E2E
lane. The initial lanes share no modules. Runtime and E2E stay sequential to
avoid tests racing ahead of the final lifecycle API.

## Implementation tasks

- [ ] **T1 (P1, human: ~2h / Codex: ~15min)** - Configuration - add the
  environment-only secret, safe report value, and test isolation.
  Verify with `uv run --frozen pytest tests/test_config.py tests/test_commands_operational.py`.
- [ ] **T2 (P1, human: ~4h / Codex: ~30min)** - Delivery - implement exact event
  conversion, endpoint validation, hard-deadline streaming POST, retries, safe
  errors, and cleanup.
  Verify with `uv run --frozen pytest tests/test_watch_webhook.py`.
- [ ] **T3 (P1, human: ~3h / Codex: ~20min)** - Runtime - wire delivery after
  snapshot/output without changing watch state on failure.
  Verify with `uv run --frozen pytest tests/test_runtime.py tests/test_watch.py`.
- [ ] **T4 (P1, human: ~3h / Codex: ~20min)** - E2E - prove isolated config,
  one loopback POST, no leaks, and clean signal shutdown.
  Verify with `uv run --frozen pytest tests/e2e/test_watch_daemon.py -q`.
- [ ] **T5 (P2, human: ~1h / Codex: ~10min)** - Documentation - update README,
  basic usage, and architecture without duplicating into other pages.
  Verify with `uv run --frozen mkdocs build --strict`.

## TODO decisions

No `TODOS.md` entry is added by this review:

- **Durable outbox: skip for now.** It would close the accepted crash window and
  support stronger delivery guarantees, but requires persisted delivery state,
  retry scheduling, retention, and duplicate recovery. Reconsider after users
  require at-least-once delivery and the version-1 payload is proven stable.
- **Explicit webhook proxy or private CA: skip for now.** It would support
  restricted corporate networks, but expands the secret and trust configuration
  surface. Reconsider when a concrete deployment cannot use a public-CA HTTPS
  endpoint or direct route.
- **Service installation commands: skip this PR.** Install/status/uninstall
  helpers could simplify reboot recovery, but they are independent of event
  delivery and platform-specific. Reconsider after webhook delivery ships and
  real launchd/systemd usage identifies the smallest portable interface.
- **Full `Retry-After` support: skip for now.** It could improve success against
  rate-limited receivers, but conflicts with the selected deterministic 16.25s
  tick bound. Reconsider if production endpoints return sustained `429` and the
  delivery budget is deliberately expanded.

## Documentation and release

README, basic usage, and architecture will document configuration, payload
semantics, failure behavior, and the best-effort guarantee. CLI reference,
troubleshooting, and roadmap will not duplicate the same configuration text in
this slice. The feature is intended for the next `0.7.19` release; version files
remain owned by Release Please.

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | - | Not run; scope was reduced during engineering review |
| Codex Review | `/claude consult` | Independent second opinion | 1 | UNAVAILABLE | Claude weekly usage limit; no findings produced |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | CLEAR (PLAN) | 12 issues folded, 0 critical gaps |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | - | Not applicable to this backend-only change |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | - | Not required for this bounded configuration addition |

**VERDICT:** ENG CLEARED - ready to write the implementation plan.

NO UNRESOLVED DECISIONS
