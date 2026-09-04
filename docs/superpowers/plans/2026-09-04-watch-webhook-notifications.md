# Watch Webhook Notifications Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: use `superpowers:subagent-driven-development` to execute this plan task by task, and `superpowers:test-driven-development` for every behavior change.

**Goal:** Deliver one bounded, best-effort JSON webhook after a persistent watch commits and reports a real profile change, without changing watcher health or one-shot behavior.

**Architecture:** Extend environment configuration with an opaque webhook secret, implement pure event conversion plus an isolated HTTPX notifier in `insto/service/watch_webhook.py`, and let `open_runtime` own notifier construction, delivery, and teardown only for REPL/daemon roles. The existing `diff_and_snapshot` persistence boundary and `WatchManager` state machine remain authoritative; webhook failures are observational output only.

**Tech stack:** Python 3.11+, asyncio, HTTPX 0.28, pytest/pytest-asyncio, Ruff, strict mypy, MkDocs.

**Approved design:** `docs/superpowers/specs/2026-09-04-watch-webhook-notifications-design.md`

---

## Parallel execution

- Lane A: Task 1 (configuration and isolation).
- Lane B: Tasks 2 then 3 (event contract, then delivery in the same new module).
- Lane C: Task 6 documentation may start from the approved contract.
- Merge/check Lanes A and B before Task 4. Run Task 5 only after Task 4.

Agents share one worktree, so each lane owns only the files listed for it and commits its own changes. Task 4 is the integration checkpoint and must begin from a clean tree.

### Task 1: Add environment-only configuration and safe reporting

**Files:**

- Modify: `insto/config.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/e2e/conftest.py`
- Test: `tests/test_commands_operational.py`

**Step 1: Write failing configuration and isolation tests**

Add `ENV_WATCH_WEBHOOK_URL = "INSTO_WATCH_WEBHOOK_URL"` to fixture cleanup lists first, then cover:

```python
def test_watch_webhook_is_environment_only(monkeypatch: pytest.MonkeyPatch) -> None:
    write_config({"watch": {"webhook_url": "https://toml.invalid/hook"}})
    assert load_config().watch_webhook_url is None

    monkeypatch.setenv(cfgmod.ENV_WATCH_WEBHOOK_URL, "https://receiver.example/hook")
    config = load_config()
    assert config.watch_webhook_url == "https://receiver.example/hook"
    assert config.sources["watch.webhook_url"] == "env"
```

Also test unset and empty values, secret registration through `redact_secrets`, and exact `effective_config_report` rows whose value is only `configured` or `disabled`. Exercise `/config` in terminal and JSON export paths. In `insto_env`, remove an inherited webhook variable before applying test-specific values.

**Step 2: Run the focused tests and confirm RED**

Run: `uv run --frozen pytest tests/test_config.py tests/test_commands_operational.py tests/test_cli.py tests/e2e/test_oneshot.py -q`

Expected: failures for the missing constant, field, and report row.

**Step 3: Implement the minimal config change**

In `Config`, add `watch_webhook_url: str | None = None`. In `load_config`, call `_pick` with no CLI key or TOML value:

```python
webhook_url, sources["watch.webhook_url"] = _pick(
    {}, "watch_webhook_url", ENV_WATCH_WEBHOOK_URL, None, None
)
```

Register a non-empty string with `register_secret`, return it on `Config`, and add a custom report value:

```python
"watch.webhook_url": "configured" if config.watch_webhook_url else "disabled"
```

Do not add it to `write_config`, accepted CLI overrides, or proxy rendering.

**Step 4: Run GREEN and quality checks**

Run: `uv run --frozen pytest tests/test_config.py tests/test_commands_operational.py tests/test_cli.py tests/e2e/test_oneshot.py -q`

Run: `uv run --frozen ruff check insto/config.py tests/test_config.py tests/test_commands_operational.py tests/test_cli.py tests/e2e/conftest.py`

**Step 5: Commit**

```text
feat: add watch webhook configuration boundary
```

### Task 2: Define endpoint validation and the versioned event contract

**Files:**

- Create: `insto/service/watch_webhook.py`
- Create: `tests/test_watch_webhook.py`

**Step 1: Write failing endpoint validation tests**

Table-test accepted remote HTTPS URLs plus `http://localhost`, IPv4 loopback, and IPv6 loopback. Reject missing hosts, fragments, unsupported schemes, and remote HTTP. Each rejection must assert the exception text contains neither the endpoint nor its secret path/query.

Use one public validator:

```python
validate_webhook_url(value: str) -> str
```

It returns the unchanged accepted URL and raises `BackendError("invalid watch webhook URL: ...")` with a reason that never interpolates the value.

**Step 2: Write failing pure event tests**

Call `build_watch_event(username, diff, event_id=..., observed_at=...)` and assert the exact key set and values. Cover `first_seen`, empty `changes`, ordinary changes, aliases as context, and the critical historical-alias/no-current-change regression. Require a UTC `Z` timestamp and verify the supplied id is unchanged.

**Step 3: Run the focused tests and confirm RED**

Run: `uv run --frozen pytest tests/test_watch_webhook.py -q`

Expected: import failure because the module does not exist.

**Step 4: Implement pure validation and conversion**

Use `httpx.URL` for parsing and `ipaddress.ip_address` for loopback classification. Keep filtering explicit:

```python
if diff.get("first_seen") or not diff.get("changes"):
    return None
```

Return only `schema_version`, `event`, `event_id`, `username`, `observed_at`, `changes`, and `previous_usernames`. Copy mutable dict/list values so later caller mutation cannot change an in-flight payload.

**Step 5: Run GREEN and static checks**

Run: `uv run --frozen pytest tests/test_watch_webhook.py -q`

Run: `uv run --frozen ruff check insto/service/watch_webhook.py tests/test_watch_webhook.py`

Run: `uv run --frozen mypy insto/service/watch_webhook.py`

**Step 6: Commit**

```text
feat: define watch webhook event contract
```

### Task 3: Implement bounded streaming delivery

**Files:**

- Modify: `insto/service/watch_webhook.py`
- Modify: `tests/test_watch_webhook.py`

**Step 1: Write failing success, retry, and permanent-failure tests**

Construct `WebhookNotifier` with an injected `httpx.AsyncClient` using `MockTransport` and an injected async sleep recorder. Assert:

- every `2xx` succeeds once;
- transport errors, `408`, `429`, and representative `5xx` statuses make at most three attempts with delays `[0.25, 1.0]`;
- retry then success stops early;
- `3xx` and other `4xx` fail after one attempt;
- every attempt carries the exact same payload and event id;
- response bodies and endpoint values never appear in `WebhookDeliveryError`.

**Step 2: Write failing boundary/lifecycle tests**

Add cases proving redirects are not followed, the owned client has `trust_env=False`, a never-completing attempt hits the hard deadline, a large streaming body is closed without being read, cancellation propagates during both request and retry sleep, and `aclose()` closes the client exactly once.

Patch a module-level `ATTEMPT_TIMEOUT_SECONDS` to a tiny value for the deadline test; do not make the production timeout configurable.

**Step 3: Run the focused tests and confirm RED**

Run: `uv run --frozen pytest tests/test_watch_webhook.py -q`

Expected: failures for the missing notifier and error type.

**Step 4: Implement the notifier**

Expose `WebhookDeliveryError`, `WebhookNotifier.send(payload)`, and `WebhookNotifier.aclose()`. The default client is one pooled `httpx.AsyncClient(follow_redirects=False, trust_env=False)`. For each attempt:

```python
request = client.build_request("POST", endpoint, json=payload)
async with asyncio.timeout(ATTEMPT_TIMEOUT_SECONDS):
    response = await client.send(request, stream=True)
    try:
        status = response.status_code
    finally:
        await response.aclose()
```

Retry `httpx.TransportError`, built-in `TimeoutError`, `408`, `429`, and `5xx`; never catch `asyncio.CancelledError`. Raise one safe terminal error after exhaustion or a permanent status. Never read response content or include raw exception text unless it has first been reduced to a controlled failure class.

**Step 5: Run GREEN and static checks**

Run: `uv run --frozen pytest tests/test_watch_webhook.py -q`

Run: `uv run --frozen ruff check insto/service/watch_webhook.py tests/test_watch_webhook.py`

Run: `uv run --frozen mypy insto/service/watch_webhook.py`

**Step 6: Commit**

```text
feat: deliver watch webhooks with bounded retries
```

### Task 4: Integrate delivery with runtime ownership

**Files:**

- Modify: `insto/service/runtime.py`
- Modify: `tests/test_runtime.py`

**Step 1: Write failing construction and teardown tests**

Add a `webhook_notifier_factory` seam to `open_runtime`. Assert configured one-shot mode neither validates nor calls the factory. Assert REPL/daemon validates and constructs before executor acquisition. Extend normal, partial-construction, coordinator-stop-failure, and cancellation cases so notifier cleanup happens once and does not prevent the remaining resources from closing.

**Step 2: Write failing tick-flow tests**

Use a notifier spy and seeded `HistoryStore` snapshots to cover first-seen, unchanged, changed, and backend failure. For the changed branch, record this exact order:

```text
snapshot visible in SQLite -> terminal output callback -> notifier.send
```

Assert event id generation and UTC observation happen once per changed tick. Prove an alias-only historical context does not deliver.

**Step 3: Write failing observational-failure tests**

Make delivery exhaust and assert one redacted warning, active watch status, `last_ok` set, `consecutive_errors == 0`, and `last_error is None`. Repeat with a warning callback that raises; watcher state must remain successful. Use distinct endpoint and response-body sentinels and assert neither appears in captured output.

**Step 4: Run the focused tests and confirm RED**

Run: `uv run --frozen pytest tests/test_runtime.py tests/test_watch.py -q`

Expected: missing runtime factory/lifecycle and delivery calls.

**Step 5: Implement ordered runtime integration**

For non-one-shot roles with a configured URL: validate, create a notifier, and retain it for teardown. After `diff_and_snapshot` and the existing suppressed terminal output, build an event with one `uuid4()` and `datetime.now(timezone.utc)`; if non-`None`, await delivery. Catch ordinary delivery exceptions only at this integration boundary and emit one `redact_secrets` warning through the already observational output callback.

Cancel coordinator/manager work before closing notifier, then close notifier before CDN/backend/history resources. Preserve `CancelledError` propagation and all existing `WatchManager` success semantics.

**Step 6: Run GREEN and static checks**

Run: `uv run --frozen pytest tests/test_runtime.py tests/test_watch.py -q`

Run: `uv run --frozen ruff check insto/service/runtime.py tests/test_runtime.py`

Run: `uv run --frozen mypy insto/service/runtime.py`

**Step 7: Commit**

```text
feat: notify after persisted watch changes
```

### Task 5: Prove the daemon flow in a subprocess

**Files:**

- Modify: `tests/e2e/test_watch_daemon.py`

**Step 1: Write the failing POSIX E2E**

Start a loopback `ThreadingHTTPServer` fixture that records request headers/body and returns a response with a unique body sentinel. Register `@alice`, pre-seed a snapshot that differs from the fake backend's current profile, set only this test's `INSTO_WATCH_WEBHOOK_URL`, make the watch due, and launch the daemon.

Wait until exactly one request arrives. Assert the complete version-1 payload, `Content-Type`, persisted snapshot, and healthy watch state. Send SIGTERM and require exit code 0. Scan stdout, stderr, and any rotating log beneath `INSTO_HOME`; neither endpoint nor response-body sentinel may appear.

**Step 2: Run the E2E and confirm RED**

Run: `uv run --frozen pytest tests/e2e/test_watch_daemon.py -q`

Expected: failure until runtime integration is complete or any integration defect is exposed.

**Step 3: Make only integration-level corrections**

Fix product code only when the E2E proves a real boundary mismatch. Keep the server loopback-only, ensure cleanup stops both process and server in `finally`, and do not weaken exact-one-request assertions.

**Step 4: Run GREEN**

Run: `uv run --frozen pytest tests/e2e/test_watch_daemon.py -q`

**Step 5: Commit**

```text
test: cover daemon webhook delivery end to end
```

### Task 6: Document the operator contract

**Files:**

- Modify: `README.md`
- Modify: `docs/basic-usage.md`
- Modify: `docs/architecture.md`

**Step 1: Update the selected documentation only**

Document `INSTO_WATCH_WEBHOOK_URL`, `configured`/`disabled` reporting, role behavior, version-1 payload, retry/deadline policy, direct-network/no-redirect boundary, trusted-endpoint warning, receiver deduplication via `event_id`, and the best-effort crash window. Add this architecture diagram:

```text
WatchManager -> diff_and_snapshot -> SQLite snapshot
                                |
                                +-> terminal output
                                `-> build event -> bounded webhook retry -> warning only
```

Do not add duplicate pages or service-installation guidance.

**Step 2: Build documentation strictly**

Run: `uv run --frozen mkdocs build --strict`

**Step 3: Commit**

```text
docs: explain watch webhook notifications
```

### Task 7: Final verification and pre-landing review

**Files:**

- Verify all changed files

**Step 1: Run the complete test and quality matrix**

Run:

```text
uv run --frozen pytest -q
uv run --frozen ruff check
uv run --frozen ruff format --check
uv run --frozen mypy insto
uv run --frozen mkdocs build --strict
uv build
```

Expected: every command exits 0; pytest retains or improves the repository coverage gate.

**Step 2: Verify repository scope and secrets**

Run `git diff --check`, inspect `git status --short`, and search the diff for endpoint fixtures, response-body sentinels, accidental URL logging, unrelated lock/version churn, and any mention of deferred transports. Confirm `uv.lock` changed only if `uv build` or dependency metadata legitimately requires it; otherwise restore the incidental root-package version line with `apply_patch`.

**Step 3: Request independent code review**

Use `superpowers:requesting-code-review` and the repository `review` skill. Resolve verified P1/P2 findings with regression tests, rerun the full matrix, and keep unrelated user changes untouched.

**Step 4: Finish the branch**

Use `superpowers:verification-before-completion`, then `superpowers:finishing-a-development-branch`. The expected path is PR creation followed by `land-and-deploy` only after required CI is green.
