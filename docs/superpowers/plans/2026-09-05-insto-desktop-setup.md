# C1 desktop setup and service recovery implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Use Astra only.

**Goal:** Add safe token-only setup, credential replacement and managed service lifecycle operations to the Python desktop bridge, ready for P1 application integration.

**Architecture:** Python owns the private GUI profile, durable recovery and the existing launchd controller. One desktop profile lock nests one service management lock; native calls drain before either releases. Token validation precedes disk or service changes. Read operations never initialize state or import a provider.

**Tech Stack:** Python 3.11+, asyncio, stdlib filesystem/flock/SQLite, existing HikerBackend/HistoryStore/macOS controller, pytest/Ruff/mypy. No new shipped dependencies.

## Inputs, boundaries and baseline

Approved requirements: [GUI specification](../specs/2026-09-05-insto-gui-design.md), [delivery map](2026-09-05-insto-gui-delivery-map.md), [engineering review](2026-09-05-insto-gui-engineering-review.md). C0 is `d4ea4f89aa57940e6fbba2d9c195b11ae682e777`; P0 passed on arm64 in sibling `insto-gui`, build ID `50fb4f6e77cbbb13e7267cf4dbd5c0b262832b6f0220b24b545c451586469778`.

Execute in `feat/desktop-setup`, worktree `<worktrees>/desktop-setup`. C0 and the P0 payload remain unchanged. Baseline: **1379 passed, 1 opt-in native skipped**, 10.85s. The developer venv uses frozen exports plus developer-only `editables==0.5`; isolated subprocess tests require its editable checkout installation. Published/runtime wheels remain non-editable. Never run `uv run` or unfrozen `uv sync`; `uv.lock` remains unchanged.

This phase does not implement watch CRUD/history, a GUI, an updater, external-home adoption, runtime migration, signing, publication or real provider smoke. Existing CLI semantics remain compatible. Test services use only new isolated fake profiles and verified exact-registration cleanup.

## Contracts fixed before implementation

- Operations: `hello`, `setup.inspect`, `setup.configure`, `settings.inspect`, `credentials.replace`, `service.start`, `service.stop`, `service.repair`.
- `setup.configure` and `credentials.replace` accept exactly `{"token": "..."}`; other operations accept an empty object. Tokens are nonempty bounded printable ASCII, with no surrounding whitespace or controls. No profile paths, backend choice, runtime path or arbitrary native arguments come from JSON.
- Trusted launcher environment `INSTO_DESKTOP_ROOT` selects a canonical absolute root; default is `~/Library/Application Support/insto-gui`. C1 uses only its fixed `profile/` child. It ignores `INSTO_HOME` and inherited provider/proxy settings for desktop operations. P1 supplies cleaned child environment. Existing CLI configuration loading is unchanged.
- Root `desktop-state.json` binds schema 1, `managed_by=insto-gui`, UID, exact profile path, desired running/stopped, a random revision and last validated quota. No credential or credential digest is stored there. Unmarked nonempty profiles and foreign/ambiguous service ownership are refused, not adopted.
- Profile `config.toml` pins HikerAPI, `store.db`, `output/` and the session path inside this profile. Only the private config contains the token. Replacement preserves existing configuration bytes on rollback and preserves all watches/history.
- New root/profile directories are 0700; state/config/backup/recovery/lock files are 0600, owned, ordinary, bounded, with no symlink leaf or unsafe path component. Reads do not mkdir/chmod/migrate. Atomic writes use private same-directory temporary files, fsync(file), replace/link and fsync(parent).
- `setup.inspect`/`settings.inspect` return safe allowlisted fields, not raw config, paths, arbitrary exceptions or watch errors. Missing profile is a valid unconfigured state. Pending recovery is reported explicitly and resolved by `service.repair` or before any further mutation; reading itself does not recover by writing.
- Zero remaining quota is validated access and can be saved, but reports `quota_exhausted`, never healthy monitoring. A provider 402 exception is a safe quota error, not a fabricated successful validation. Unknown access responses do not replace prior credentials.

### Lock and recovery ordering

```text
candidate validation (at most 30s, including backend close)
  → desktop profile flock (nonblocking; stale caller revalidated)
    → existing service management flock
      → durable protected backup + journal
      → stop exact owned service; confirm absence + idle executor
      → atomic config/state publication
      → start/verify only when appropriate
      → committed journal → remove exact backup/journal
```

The composite deadline is monotonic, 120 seconds from operation entry, with 35 seconds reserved for rollback. Native subprocess timeouts are `min(10, remaining_budget)`. No phase starts after its budget expires. Cancellation drains started native work before locks release; exhausted recovery leaves a durable record and a safe `recovery_required`/timeout response, never blind retry.

Replacement journal phases are `prepared`, `stopped`, `written`, `committed`, `rollback`, `rolled_back`. It contains original nonsecret state, separately observed `previous_running`, and a fixed backup filename, not raw credentials. The backup is durable before journal publication, and both are durable before any stop/config change. An orphan pre-journal backup is removable only when it safely matches the still-current config; otherwise recovery is required. Recovery of an uncommitted replacement rolls back exact old config and state; terminal `committed`/`rolled_back` records only finish cleanup. Persist the terminal phase before unlinking backup then journal. A missing required nonterminal backup never becomes successful recovery.

Both live rollback and crash recovery re-inspect and stop any candidate service, then hold `idle_executor` before restoring old config. A successful bootstrap followed by readiness failure can leave a running candidate: phase names never substitute for observing that process. If idle ownership cannot be established, preserve recovery state instead of restoring beneath an external executor. Restart the old service only when `previous_running` was true. A credential change on a desired-running but already cleanly-exited/unloaded service preserves that observed nonrunning disposition; explicit start/repair is a separate action.

First setup is different: after validated config is saved it is retained if service startup fails. Recovery can finish database/service setup without another token prompt. A journal marks setup before publishing config, and state is bound before service mutation. Repeated identical configure is idempotent; a different token requires `credentials.replace`.

Stop persists stopped intent before native disable/bootout, retains service artifacts and profile data, and verifies the executor is idle. Restarting the GUI does not change that intent. Start/repair explicitly handle loaded-but-cleanly-exited jobs through a non-forcing kickstart of the exact verified label. Runtime changes and external-home adoption remain G2, not permissive repair.

## File boundaries

| File | Responsibility |
| --- | --- |
| `insto/desktop/errors.py` | Static safe domain error codes/messages |
| `insto/desktop/access.py` | Strict candidate validation, redaction and bounded close |
| `insto/desktop/profile.py` | Canonical paths, private I/O, ownership state and profile flock |
| `insto/service/watch_service_lifecycle.py` | Scoped controller lease, start/stop/readiness/idle executor; no desktop protocol |
| `insto/service/watch_service.py` | Backward-compatible optional per-call native timeout |
| `insto/desktop/recovery.py` | Credential journal and exact rollback/reconciliation |
| `insto/desktop/operations.py` | Setup/replace/service orchestration and safe DTOs |
| `insto/desktop/dispatch.py` | Strict allowlist/params, lazy imports, static errors |
| Corresponding `tests/test_desktop_*.py`, `tests/test_watch_service_lifecycle.py` | Unit, filesystem and state-machine tests |
| `tests/e2e/test_desktop_lifecycle.py` | Isolated process/native controller proof, no real provider |

## Task 1: Static error and strict access boundary

- [ ] Add `tests/test_desktop_access.py` before implementation. Parameterize positive remaining/zero, AuthInvalid, QuotaExhausted, RateLimited, Transient, SchemaDrift, plain BackendError, constructor failure, timeout and cancellation. Verify redactor registration before construction and exact once-close on every constructed-client outcome.

```python
@pytest.mark.asyncio
async def test_zero_is_confirmed_but_not_monitoring_health(monkeypatch):
    from insto.desktop import access
    class Backend:
        closed = 0
        async def validate_access(self):
            return Quota.with_remaining(0)
        async def aclose(self):
            self.closed += 1
    backend = Backend()
    monkeypatch.setattr(access, "make_backend", lambda token: backend)
    result = await access.validate_candidate("offline-c1-candidate")
    assert result == 0
    assert backend.closed == 1
```

- [ ] Red: `.venv/bin/python -m pytest tests/test_desktop_access.py -q` must fail for the missing access boundary.
- [ ] Implement `DesktopError(code)` and a static catalog. `validate_candidate(token: str) -> int` registers the secret before lazy SDK import, calls a desktop-local `make_backend(token)` directly constructing HikerBackend (never the environment-sensitive backend registry), uses the strict C0 `validate_access`, and closes once in `finally`. The SDK adapter initializes BaseClient with its explicit packaged default host and creates its HTTP transport with `trust_env=False`, so poisoned provider/proxy/CA environment cannot affect construction or requests. Preserve CLI defaults. Invalid params fail before construction; no console logging or exception reflection. Access errors map to `invalid_token`, `quota_exhausted`, `rate_limited`, `network_error`, `access_unconfirmed`, `operation_timeout`; filesystem/lifecycle errors have their own codes.
- [ ] Green, Ruff and mypy; commit only access/errors and tests. Independent spec review, then code-quality review.

## Task 2: Private owned profile storage

- [ ] Add filesystem tests for no-effect missing reads, unsafe roots/components/files, ownership binding, 64KiB read bounds, state schema/types, atomic replacement, directory fsync, stable nonblocking lock contention and no secret in state.

```python
def test_missing_inspection_does_not_create_profile(tmp_path, monkeypatch):
    from insto.desktop.profile import Profile
    root = tmp_path / "new-root"
    monkeypatch.setenv("INSTO_DESKTOP_ROOT", str(root))
    profile = Profile.from_environment()
    assert profile.read_state() is None
    assert not root.exists()
```

- [ ] Red before writing `profile.py`.
- [ ] Implement `Profile(root)` with fixed `home`, `config`, `state`, `recovery`, `backup`, `lock_path` properties; `from_environment`, `read_state`, `read_config`, `read_journal`, `write_state`, `write_config`, `write_journal`, `write_backup`, `remove_journal`, `remove_backup`, `locked(initialize=False)`. State/journal validators reject unknown keys, malformed types, foreign UID/profile and arbitrary backup paths. Read config returns bounded bytes, not a public DTO. All writes occur under the profile lease; no mkdir in reads. Reject an unmarked populated profile before mutation. Make ownership establishment explicit for new setup, not implicit in a read.
- [ ] Green tests, real cross-process flock test, Ruff/mypy, scoped commit and two-stage review.

## Task 3: Extend the existing service controller safely

- [ ] Add controller tests using real private filesystem fixtures and the native-command boundary mock: running no-op, clean-exited start, persistent stopped disable/bootout, startup verification failure, partial owned artifact repair, unknown state, altered manifest/plist, mismatched interpreter, executor contention and repeated cancellation.
- [ ] Red: `.venv/bin/python -m pytest tests/test_watch_service_lifecycle.py -q`.
- [ ] Add optional time budget to `_run_launchctl`/`_launchctl` while retaining old defaults and legacy tests. New context-manager `managed_service(*, home: Path, config: Config, deadline: float)` acquires the same stable `_management_lock` and yields a `ManagedService`. Lease methods: `inspect_owned()`, `ensure_running()`, `ensure_stopped()`, `idle_executor()` (context manager). No `already_locked=True` escape hatch.
- [ ] Reuse controller `_desired`, `_existing_matches`, `_matches_owned_plist`, `_atomic_write`, `_launchctl`, `_parse_launchctl_print` and validated directories. An existing artifact must match the requested config/runtime before any mutation; absent/partial artifacts can be completed only when existing pieces match and no unknown loaded registration exists. Also verify the loaded native job's actual program/complete argument vector against the owned plist before kickstart/bootout; missing or ambiguous native provenance is an error, not disk-only authorization. `ensure_running` verifies native running state and matching busy executor lock/PID, not just a loaded label. Never signal that PID. `ensure_stopped` disables the owned label across login, bootouts when loaded, verifies absence and idle executor, preserving files. `idle_executor` holds a validated stable flock without overwriting its PID while config changes are published.
- [ ] Native absence plus a busy executor is not authorization to kill/adopt it. Refuse safely. Start has a release-to-bootstrap race with a foreground CLI; detect failed ownership/readiness and leave recovery rather than changing a running external executor's config.
- [ ] Preserve cancellation draining; deadlines stop new subprocess starts and bound each native command. Tests must show competing old CLI controller calls remain blocked until in-flight native work has drained.
- [ ] Green new and legacy service/runner tests, Ruff/mypy, commit and independent two-stage review.

## Task 4: Durable setup and credential transactions

- [ ] Add `tests/test_desktop_operations.py` and `tests/test_desktop_recovery.py` with real profile/state/config/database files and a controlled service lease. Assert unchanged config before confirmed stop; old/new credential sentinels absent from DTOs/errors/journal/state. Inject a failure at every durable phase and reconstruct a fresh operation object for recovery.

```python
@pytest.mark.asyncio
async def test_failed_stop_does_not_publish_candidate(configured_profile, monkeypatch):
    from insto.desktop import operations
    profile, service = configured_profile
    old = profile.read_config()
    service.fail_stop = True
    monkeypatch.setattr(operations, "validate_candidate", AsyncMock(return_value=8))
    with pytest.raises(DesktopError):
        await operations.replace_credentials(profile, "new-offline-sentinel")
    assert profile.read_config() == old
```

- [ ] Red, then implement `configure(profile, token)`, `replace_credentials(profile, token)`, `change_service(profile, action)`, and `inspect_profile(profile)`. Use explicit Config construction from the private TOML and fixed profile paths: do not call environment-mutating `resolve_service_config` from desktop orchestration. No `open_runtime` or provider for inspect. Initialize HistoryStore only for setup after safe schema/type checks; never migrate an existing incompatible DB.
- [ ] Implement the journal protocol above in `recovery.py`. Revalidate state under both locks after candidate validation; never overwrite pending recovery. Duplicate same-token setup/replacement avoids unnecessary service restart after revalidation. Replacement records previous state, confirms stop, holds idle executor across atomic config write, restarts only when `previous_running` was true and commits new revision/quota. Stopped remains stopped. On stop failure old config remains; on start failure rollback restores exact old config/state and restarts only the previously running owned service. A failed rollback leaves a protected pending record and no success response.
- [ ] Recovery always precedes new mutations; inspect only reports it. Recovery itself never validates tokens over the network. Committed replacement finishes cleanup, other replacement phases restore previous config. Setup with saved validated config retains it and resumes local setup/service recovery. Preserve existing watch generations, paused status, snapshots and limits.
- [ ] Deadline tests cover validation+native work+rollback within one 120-second envelope, 35-second rollback reserve, timeout before disk mutation and timeout after mutation yielding durable recovery. Repeat cancellation during native stop/start must not release either lock early.
- [ ] Run cross-process double-submit and crash-reconstruction tests. Commit implementation/tests after green and two independent reviews.

## Task 5: Publish only implemented desktop capabilities

- [ ] Add strict contract tests before changing dispatch: exact accepted params, absent/extra/invalid token types, arbitrary paths/backend rejected, safe static errors, no candidate/previous sentinel anywhere, one response line and request-ID fidelity.

```python
@pytest.mark.asyncio
async def test_setup_does_not_accept_a_profile_path():
    raw = b'{"protocol_version":1,"request_id":"c1","operation":"setup.configure","params":{"token":"offline","home":"/foreign"}}\n'
    result = json.loads(await handle(raw))
    assert result["error"]["code"] == "invalid_params"
```

- [ ] Wire lazy operation imports, keep `hello` SDK/config/database-free and advertise exactly the C1 list. Update C0 capability assertions without weakening transport tests. Wrap only known safe `DesktopError` codes; unexpected details become static `internal_error`. No production fake operation or credential echo.
- [ ] Add isolated installed-process tests for `hello`, missing setup inspection, invalid params and pending recovery reporting. A repeated read must leave the managed root unchanged and import no provider SDK.
- [ ] Green desktop/full regression tests, commit and two-stage review.

## Task 6: Installed/native verification and handoff

- [ ] Run `.venv/bin/python -m pytest --cov=insto --cov-report=term-missing -q`, `.venv/bin/ruff check .`, `.venv/bin/ruff format --check .`, `.venv/bin/mypy insto`, and `git diff --check`. Repeat service/desktop tests with `python -B`.
- [ ] Build wheel/sdist using frozen hash-bearing build constraints into a new temporary evidence directory; install wheel non-editably into a separate venv. Verify package origin, new hello capabilities and missing-profile inspect with `-I -B`, cleaned environment and no real token.
- [ ] Native fixture exercises the new controller from the installed wheel with fake config only: start, idempotence, explicit stop, restart after loaded clean exit, matching executor readiness, cleanup. A test-only Python fixture can inject boundaries; no fake knob is added to the production protocol. Reuse the previously reviewed owned-process-group supervisor/cleanup rules; run unit failure-injection before native state changes. Do not claim live HikerAPI validation from offline tests.
- [ ] Update `docs/desktop-protocol.md`, README if appropriate, and add `2026-09-05-insto-desktop-setup-results.md` with exact tests, artifacts, remaining P1/C2/G1/G2/R1 gates and any unproved native credential scenario. Keep the old C0/P0 historical results intact.
- [ ] Final independent Astra whole-change review, address verified findings with red/green regressions, rerun checks, commit and preserve local branch. No push, merge, signing or release. Verify no `Co-authored-by:` trailer in any commit.
