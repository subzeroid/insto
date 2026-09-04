# macOS Watch Service Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox syntax for tracking.

**Goal:** Add `insto watch-service install/status/uninstall` for a private macOS user LaunchAgent without changing the watcher engine.

**Architecture:** A controller manages only one owned launchd registration per canonical config home. A runner safely loads explicit credential sources and invokes the existing foreground daemon; SQLite status is independently read-only. No new dependencies, scheduler, network service, GUI, or release-version edits.

**Tech Stack:** Python 3.11+, stdlib plistlib/tomllib/subprocess/fcntl/sqlite3/logging, launchctl, pytest.

## Shared interfaces and ownership

Worktree: `<worktrees>/macos-watch-service`; base `6db7c11`. Use `.venv/bin/pytest`, `.venv/bin/ruff`, `.venv/bin/mypy`; `uv run` needlessly rewrites the stale root version in uv.lock.

Controller owns `insto/service/watch_service.py` and `tests/test_watch_service.py`. Runner owns `insto/service/watch_service_runner.py`, `tests/test_watch_service_runner.py`, and the optional `toml_data` keyword in `config.load_config`. Main owns CLI, history read-only API, their tests, docs, and CI. Workers never commit overlapping files.

Controller contract:

```python
@dataclass(frozen=True)
class ServicePaths:
    home: Path
    label: str
    directory: Path
    manifest: Path
    plist: Path
    log_dir: Path

def service_paths(home: Path | None = None) -> ServicePaths:
    canonical = (home or config_dir()).expanduser().resolve()
    digest = hashlib.sha256(os.fsencode(canonical)).hexdigest()[:16]
    label = f"io.insto.watch.{os.getuid()}.{digest}"
    directory = canonical / "services" / "watch"
    return ServicePaths(canonical, label, directory, directory / "manifest.json",
                        Path.home() / "Library" / "LaunchAgents" / f"{label}.plist",
                        directory / "logs")
```

Public async functions `install_service(*, home=None, env_file=None)`, `service_status(*, home=None)`, `uninstall_service(*, home=None)` return JSON-safe dictionaries. They reject non-macOS before file effects. CLI invokes them with `asyncio.run`. Native subprocess work is bounded and offloaded; only the controller calls launchctl.

Shared `read_private_file(path: Path, *, max_bytes: int = 65536) -> bytes` rejects symlink/non-regular/wrong-owner/group-other-accessible files and bounds reads using the opened fd, with `O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC`. It reports value-free `BackendError` errors. Runner reuses it for TOML. `read_manifest(paths: ServicePaths) -> dict[str, Any]` validates exact schema, marker, uid, computed label/home, absolute paths and expected installation location. Missing manifest is handled before this function; malformed is not absent.

Manifest version 1 keys: `schema_version`, `managed_by="insto-watch-service"`, `uid`, `label`, `config_home`, `python`, `backend`, `db_path`, `output_dir`, `aiograpi_session_path`, `env_file` (null or absolute path). No secret values. Interpreter is `os.path.abspath(sys.executable)`, not Path.resolve(). Plist invokes `[python, "-I", "-m", "insto.service.watch_service_runner", manifest_path]`, `RunAtLoad=true`, `KeepAlive={"SuccessfulExit":False}`, fixed working directory home, `Umask=63`, stdout/stderr `/dev/null` because the runner logs through a bounded private handler.

Runner contract: `read_service_env(path: Path | None) -> dict[str,str]`; `resolve_service_config(home: Path, env_file: Path | None, *, pinned: dict[str,Any] | None=None) -> Config`; `main(argv: list[str] | None=None) -> int`. Resolver is a startup-only sync helper: temporarily remove inherited app/proxy/CA values, apply approved env file and home, securely parse config (missing file means empty data), invoke `load_config(toml_data=...)`, then apply all pins and resolve paths. Restore the caller environment on return; runner main additionally retains the sanitized environment for backend lifetime. Validate configured backend credentials and webhook locally, without constructing a backend. Empty values follow existing `_pick` semantics, NUL is rejected. Main supplies `_run_watch_daemon(config, log, *, output=None)` where supplied output receives all startup/tick messages; default remains flushed print.

History contract: `read_watches_readonly(path: Path) -> list[WatchSpec] | None`, and async wrapper via `asyncio.to_thread`. None means missing file; validate `_meta.schema_version == 2`, reuse `_WATCH_SELECT`/`_row_to_watchspec`; `mode=ro`, bounded busy timeout, no migrations/chmod/mkdir/journal changes. Status returns watch `has_error` only, never `last_error` text.

## Task 1: Native controller and ownership

- [x] Write `tests/test_watch_service.py` with real temporary files and fake launchctl boundary. First test calls public install and checks owned manifest/plist arguments rather than merely asserting mock existence.
- [x] Run `.venv/bin/pytest tests/test_watch_service.py -q`; confirm missing controller behavior fails.
- [x] Implement the shared contracts and lifecycle. Serialize mutations with a nonblocking secure management flock. Validate parent ownership/modes and avoid symlinks before generated writes; never chmod foreign files. Atomic exclusive initial writes and deterministic manifest/plist equality make identical repeat safe. Missing one owned generated file can be repaired only with no loaded job; different/foreign content errors. Uninstall validates both documents before native mutation; uncertain service state or bootout failure preserves artifacts. Keep lock file and all data/logs/env files.
- [x] Add tests for fresh/repeat/unloaded/changed/corrupt/foreign/symlink/FIFO/wrong owner, missing GUI, disabled state, bootstrap and bootout failures, subprocess timeout, unknown status text, missing Python, concurrency, preserving SQLite/env/logs and executor-lock inode. Use fixed `/bin/launchctl` argument lists and ten-second command timeout. Diagnostic parsing is optional and never used to authorize destructive operations.
- [x] Run focused tests plus Ruff/mypy for the module. Main performs spec compliance; independent reviewer checks quality afterward. Commit only the controller and its tests once green.

## Task 2: Safe runner, secret sources, and logs

- [x] Write tests for explicit env-file values overriding protected config, ambient env ignored, backend/path pins surviving config edits, credentials reloaded, malformed/NUL/unknown-key env values, and generated logs not containing secret values. Start by exercising `resolve_service_config` with a protected fake-backend config.
- [x] Run `.venv/bin/pytest tests/test_watch_service_runner.py -q`; verify expected missing behavior fails.
- [x] Add `toml_data` keyword in `load_config` with unchanged default behavior:

```python
def load_config(cli_overrides: dict[str, Any] | None = None, *,
                toml_data: dict[str, Any] | None = None) -> Config:
    cli = cli_overrides or {}
    if toml_data is None:
        toml_data = _read_toml(config_file_path())
```

- [x] Implement startup-only resolver and runner contract. Clear `INSTO_*`, `HIKERAPI_*`, `AIOGRAPI_*`, upper/lowercase standard HTTP proxy variables, `SSL_CERT_FILE`, `SSL_CERT_DIR`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`; apply only approved sources. Config/manifest/env file errors never include content. Pin backend/db/output/session after resolving credentials; ensure the backend factory cannot override pins via ambient `INSTO_BACKEND`.
- [x] Implement a service-specific rotating handler using existing `RedactingFormatter`/rotation constants. Descriptor-safe `_open` and validated rotation siblings must refuse symlink/FIFO/wrong owner/unsafe modes. Use INFO for service startup and tick events, stderr/stdout not additional unlimited files. Runner calls existing async daemon and preserves clean exit status, cancels/cleans resources through existing runtime, returns nonzero on startup failure.
- [x] Verify config symlink/owner/mode/read-bound tests, env size and empty semantics, logging symlink/rotation/redaction, and daemon invocation pins/output. Run `.venv/bin/pytest tests/test_watch_service_runner.py tests/test_config.py -q`, Ruff and mypy. Commit only owned files when green, after main spec review then independent quality review.

## Task 3: Read-only status and CLI

- [x] Add a missing-store test before implementation:

```python
def test_readonly_missing_store_creates_nothing(tmp_path):
    from insto.service import history
    path = tmp_path / "absent" / "store.db"
    assert history.read_watches_readonly(path) is None
    assert not path.parent.exists()
```

- [x] Observe expected failure, then implement the history contract. Test actual schema-v2 active/paused rows, absent/corrupt/v1/future schemas, read-only permissions, URI punctuation and no database-byte/schema changes. Use one explicit read transaction for version + rows.
- [x] Add service parser/routing tests before changing CLI: service help/commands, rejected legacy credential flags, unknown options, Linux error without config/log directories, `@watch-service -c info` unchanged, no credentials/backend needed for status. Parse service verbs without swallowing existing `-c` remainder; a narrow early routing function is preferable to converting the whole CLI into subparsers.
- [x] Add keyword-only optional output injection to `_run_watch_daemon`, keeping old callers and flushed stdout unchanged. Test callback receives startup messages and default foreground path remains compatible.
- [x] Integrate JSON-safe status with version 1, separate installation/registration/process observations, nullable unknown diagnostics, UTC last_ok, per-watch `has_error`, and unavailable-database error. Do not equate registered with healthy. Text uses the same object; print paths/helpful recovery but no stored errors or raw launchctl output.
- [x] Run `.venv/bin/pytest tests/test_cli.py tests/test_history.py tests/test_watch_service_cli.py tests/e2e/test_watch_daemon.py -q` plus focused new modules. Review actual cross-module paths before commit.

## Task 4: Distribution, docs, and installed integration

- [x] Add documented opt-in macOS test `tests/e2e/test_watch_service.py`: temp config home, unique generated label, installed package/fake backend; install, observe SQLite tick, crash process and observe restart, clean stop, uninstall with data preservation. Use real launchctl only behind explicit opt-in and GUI-domain availability. Never use the user's live config. Always targeted cleanup in finally, bounded waits, report skip distinctly.
- [x] Add a macOS CI job with Python3.12 and focused offline tests; run the opt-in OS integration only if a GUI domain exists. Linux matrix still passes all offline tests. Do not add provider credentials to this job.
- [x] Update README, basic usage, CLI reference and architecture/roadmap where necessary: exact commands, private env file manual creation/protection, no copying shell secrets, pinned path changes require reinstall, installed vs healthy, login/sleep/logout, failed bootstrap/unload recovery, logs/rotation, existing webhook clarification. GUI and Linux remain deferred.
- [x] Run full `.venv/bin/pytest -q`, `.venv/bin/ruff check`, `.venv/bin/ruff format --check`, `.venv/bin/mypy insto`, strict MkDocs to a temporary destination, wheel/sdist build to a temporary destination. Install wheel in isolated venv; assert imports from site-packages, run CLI/runner smoke with fake backend and local receiver. OS integration uses only an explicit ephemeral test label or is reported unavailable.
- [x] Perform final independent spec review then quality review, fix verified findings test-first, run one clean relevant verification pass. Leave release-version files unchanged. Hand off branch/evidence; no automatic merge or live installation.

## Engineering review synthesis

Architecture: 3 refinements (descriptor-safe inputs, enforceable pins, ambient network settings). Code quality: 2 (safe logging integration, NUL/empty input contract). Tests/security: 1 (historical unknown-secret disclosure in status); test diagram is in the paired spec and each task above maps its branches. Performance: no separate issue; ten-second subprocess caps, 64 KiB reads, bounded rotation, and one read-only query. No LLM evals or GUI QA apply.

NOT in scope and existing components are recorded in the approved spec. Two disjoint implementation lanes (controller and runner), main CLI/history integration, then dependent installed/OS checks. No extra follow-up TODOs beyond already deferred GUI/Linux. Six recommended refinements accepted under standing user direction; no shortcuts selected.

## GSTACK REVIEW REPORT

| Review | Runs | Status | Findings |
| --- | --- | --- | --- |
| Eng Review | 1 | CLEAR (PLAN) | Six refinements mapped to tasks; no unresolved decisions |
| Independent outside voice | 1 | COMPLETE | Five security/configuration findings included |

VERDICT: Implementation and final review complete; verification evidence is recorded below.

NO UNRESOLVED DECISIONS

## Completion evidence — 2026-09-04

- Independent spec and quality reviews completed; no remaining P1/P2 findings. Verified fixes include management-lock linearization for absent uninstall, repeated-cancellation draining of native mutations, and safe handling of corrupt stored timestamps.
- Full offline suite: **1,243 passed, 1 opt-in native test skipped** (15.22s). The native test was then explicitly enabled against the installed wheel: **1 passed** (40.06s), including failure restart, clean stop, and data-preserving uninstall.
- Installed-wheel foreground subprocess checks: **3 passed**, including the loopback webhook receiver and secret-disclosure assertions. Imports were verified from site-packages with the source checkout excluded.
- Ruff check and format check passed (119 files); mypy passed (52 source files); strict MkDocs and wheel/sdist builds passed.
- Local unreleased wheel SHA-256: `1f8707afadd15336aa3af10a0e7b0f70e07d6ca335b0da43336ad33667a7140b`. Package version remains 0.7.20; this is not a published release.
- Implementation workers left commits to the main agent for an integrated feature commit after shared verification. No merge, remote publication, or live user service installation is part of this handoff. The isolated native test removed its own generated registration and control files and retained its temporary data.
