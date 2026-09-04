# macOS Watch Service Design

Date: 2026-09-04

Status: implemented and independently reviewed; local verification complete. Evidence is recorded in the paired plan. Not merged or released.

## Goal and approved boundary

Make the existing persistent watcher usable after the terminal closes: install a macOS user LaunchAgent, start it at login, restart it after failures, inspect its state, and uninstall it without deleting monitoring data. Run without `sudo`, without a network management service, and without changing scheduling or provider behavior.

The user approved macOS first. Linux `systemd --user` and a possible separate `insto-gui` are follow-up discussions, not implementation scope here.

## What already exists

- `cli._run_watch_daemon` runs the foreground executor with SIGINT/SIGTERM handling and flushed output.
- `open_runtime` owns backend, SQLite, executor, webhook, and shutdown ordering.
- `WatchProcessLock` prevents duplicate executors per canonical database; REPL instances can remain control-only.
- SQLite schema v2 stores active/paused watches, `last_ok`, `last_error`, and error streaks.
- `config.load_config` supports the protected `config.toml`; `insto setup` already persists backend credentials there.
- `watch_webhook` reads its URL from process environment only; the existing webhook design intentionally excludes configuration-file and command-line URL inputs.
- `cli.setup_logging` supplies owner-only rotating files and central redaction.
- Release Please, the wheel/sdist pipeline, and PyPI distribution already ship the Python package. No new installer artifact or dependency is needed.

Reuse these flows. Do not create a second scheduler, process supervisor, backend factory, webhook transport, or watch registry.

## Alternatives and recommendation

1. **Recommended: native user LaunchAgent with a small Python control/runner layer.** The OS owns start/restart; Python supplies safe configuration, CLI diagnostics, and tests. This reaches the approved install/status/uninstall goal without a persistent management server.
2. **Documentation and a hand-written plist only.** Smaller implementation, but leaves installation, safe credential handling, status, and recovery to the operator. This does not fully satisfy the approved user workflow.
3. **Cross-platform supervisor or GUI now.** Adds a second lifecycle owner or another product before the existing daemon has a convenient service workflow. Deferred, not part of this slice.

## Proposed commands

```text
insto watch-service install [--env-file /absolute/path/service-env.toml]
insto watch-service status [--json]
insto watch-service uninstall
```

`watch-service` is a reserved top-level command, like `setup` and `watch-daemon`. Preserve `insto @user -c ...`, existing global options, completion, and REPL behavior. Unsupported platforms fail clearly before touching files or invoking service tools. Reject unsupported combinations, rather than interpreting service arguments as a username or silently ignoring credential flags.

Install registers the service and starts it in the current GUI login session. Repeating an identical install is a safe no-op if already loaded, or loads an unchanged installation that is currently unloaded. Changes to interpreter, pinned paths, or environment-file reference require an explicit uninstall/install cycle in this first version; no silent replacement of a running installation or foreign plist.

Uninstall removes only this installation's service registration and generated control files after successful unload. It preserves watches, snapshots, configuration, user-supplied environment files, and logs. It never kills an unrelated REPL/daemon by PID, deletes the shared executor lock, or uses a recursive cleanup.

## Approved configuration policy

**Recommended policy: no automatic capture of the invoking shell's secrets.** The service uses the existing protected configuration file. If environment-only settings are needed, the operator explicitly supplies a private, data-only TOML environment file via `--env-file`. Insto stores its absolute path, never copies its contents into its manifest or plist, never creates this secret file, and never displays its values.

Example file shape (placeholders only):

```toml
[env]
INSTO_WATCH_WEBHOOK_URL = "https://receiver.example/hook/REPLACE_ME"
```

The `[env]` allowlist is `HIKERAPI_TOKEN`, `HIKERAPI_PROXY`, `AIOGRAPI_USERNAME`, `AIOGRAPI_PASSWORD`, `AIOGRAPI_TOTP_SEED`, and `INSTO_WATCH_WEBHOOK_URL`, with string values only. The file does not accept arbitrary environment names, executable/library-path controls, Python import settings, config-home/database/output relocation, shell syntax, interpolation, or code. Parse with stdlib `tomllib`, not `source`, `eval`, or a shell. Reject unknown sections/keys, non-string values, malformed files, files larger than 64 KiB, symlinks, non-regular files, wrong owners, and any group/other permission bits. Open safely and validate the opened descriptor; check again at each service start.

Backend choice and non-secret config/database/output/session locations are resolved and pinned at installation. Do not resolve relative paths against launchd's working directory later. Credentials are loaded afresh from the protected config and optional explicit env file on each start. The runner clears inherited insto/provider settings before applying these sources, so a launchd-global environment cannot silently switch accounts, stores, or notification endpoints. Service management commands do not call providers or persist credentials.

The explicit env file is an approved **service-only way to inject the existing environment variable**. This clarifies the earlier environment-only webhook design without adding a webhook URL field to `config.toml` or SQLite.

Install validates the configuration that the service will actually use, not merely the environment that made the interactive command work. Missing credentials, insecure files, or invalid webhook settings fail before registration and do not print values. Credential flags or shell-only overrides that are not durable service configuration must not be silently treated as configured.

## Identity, paths, and ownership

One managed service per canonical `INSTO_HOME`; use a deterministic non-secret label derived from that canonical path and current user. Independent homes receive independent labels. If two homes target one database, the existing executor lock still prevents concurrent monitoring.

The plist is in `~/Library/LaunchAgents/<label>.plist`. Private installation metadata and rotating service logs live under the selected insto home. Metadata records a format version, ownership marker, label, absolute interpreter path, and pinned paths; it contains no secret values. The plist uses `ProgramArguments`, not a shell command string. Keep the virtualenv interpreter path absolute **without dereferencing the venv executable symlink out of its environment**.

Generated files are owner-only, regular files. Refuse foreign/symlink targets; do not repair permissions or overwrite unrelated files implicitly. Serialize install/uninstall for the same service with a separate management lock; it is not an executor lock and is never held by monitoring ticks. Preserve stable lock inodes.

The interpreter location must remain installed. After moving/removing an environment, status must show the missing executable and explain reinstalling the service from a durable package installation. A developer checkout is not represented as a distributable installation.

## Native lifecycle

```text
install -> validate inputs/ownership -> write owned metadata + plist
        -> enable exact gui/<uid>/<label> -> bootstrap gui/<uid> plist
        -> inspect registration; report registered separately from healthy

launchd -> pinned Python -m insto.service.watch_service_runner <metadata>
        -> safe configuration + rotating redacted logging
        -> existing foreground watch-daemon/runtime
        -> existing executor lock -> SQLite watches -> ticks/webhooks

nonzero exit -> launchd throttled restart
clean exit  -> remains stopped until next launch/login
uninstall   -> bootout exact service -> verify unloaded -> delete owned files
```

Use `KeepAlive = { SuccessfulExit = false; }` and login loading from LaunchAgents. This restarts failed processes while respecting a graceful zero-status exit. Keep launchd's restart throttling; never busy-loop in Python. Do not daemonize/fork away from launchd. Uninstall uses `bootout`, not a signal to a guessed PID. A previously disabled owned job is explicitly enabled on install. Never enable/disable a domain or another label.

Every `launchctl` subprocess uses a fixed executable, an argument vector, and a finite timeout. Distinguish missing GUI domain, absent service, permission error, invalid configuration, and timeout. Do not translate every nonzero status into "not installed". If bootstrap fails, report that installation is incomplete and preserve enough owned state to retry/uninstall safely; remove newly created files only after proving no service was loaded. If unload is uncertain, retain files and report the failure.

This is login-session availability, not a boot-before-login daemon. Logout, sleep, machine shutdown, user-disabled background items, and removal of Python can interrupt monitoring. Do not claim 24/7 availability on a sleeping laptop.

## Status contract

Report separate observations:

- installation present/absent/incomplete;
- GUI domain reachable and service registered/unregistered/unknown;
- observed process state/PID/last exit where the platform exposes recognizable diagnostics, otherwise `unknown`;
- pinned database and log paths, interpreter availability;
- persisted watch usernames, active/paused state, interval, and last successful tick in UTC;
- whether each watch has a recorded error, without returning its arbitrary error text. Credential-free status cannot safely redact historical secret values that it deliberately does not load; detailed diagnostics remain in protected service logs.

`launchctl print` is documented as unstable diagnostic text. Its parser, if used for optional fields, is best-effort and must return unknown on unrecognized output; parsed PID/state must never drive deletion, signaling, ownership, or a health-success claim. Registration alone is not proof of a healthy watcher. Missing/paused/stale watches remain visible even when the service process is running.

`--json` emits a small versioned status document with nullable/unknown observations, not raw `launchctl` output, configuration contents, or webhook URLs. Text rendering and JSON share the same status data.

Reading status never constructs a backend, acquires an executor, initializes/migrates SQLite, or changes watch rows. Add a read-only watch-list path in the history layer using the existing row mapping and a bounded SQLite timeout. An absent database means no store yet; an unsupported/corrupt database is an explicit diagnostic, not an empty healthy list. Read-only status must work even when provider credentials are missing or invalid.

## Logging

Reuse the existing redacting rotating handler in a service-specific private log directory. Route service startup/tick/warning output through it, with bounded retention; do not add launchd stdout/stderr files that grow indefinitely. Do not change foreground output defaults. Status prints log location, not automatic log contents. Configuration errors and subprocess errors also use central redaction, without raw environment dumps or tracebacks containing secret-file contents.

## Components and implementation boundary

- `insto/service/watch_service.py`: service identity, manifest/plist, platform adapter, lifecycle, status data; no backend logic.
- `insto/service/watch_service_runner.py`: explicit environment-file loading and existing daemon invocation with rotating output.
- `insto/cli.py`: early service argument routing, text/JSON output, and narrow daemon-output injection if required.
- `insto/service/history.py`: read-only status query reusing the existing watch decoder.
- Tests mirror these units; docs cover commands, credentials, login/sleep behavior, reinstall, and recovery. Existing completions are updated only as required by the new reserved command.

Prefer functions and small value records. No abstract multi-platform service framework, migration, persistent status server, extra scheduler, or new third-party dependency.

## Test and failure map

```text
CLI [unit + subprocess]
  valid commands / help / JSON / misuse / unsupported OS
  existing target, -c, setup, watch-daemon, completion [REGRESSION]
install [unit with fake launchctl + macOS integration]
  fresh / identical repeat / changed installation / concurrent attempt
  paths with spaces / venv symlink / missing interpreter
  absent GUI domain / disabled owned job / bootstrap failure / timeout
  foreign file / symlink / permissions / unsafe cleanup refusal
runner [unit + installed-package subprocess]
  protected config / explicit env file / no inherited secrets
  bad file type, permissions, ownership, keys, types, size, syntax
  config failure / duplicate executor / signals / rotating redacted output
status [unit + SQLite integration]
  absent, loaded, unloaded, incomplete, unknown platform output
  missing credentials / missing store / unsupported or corrupt schema
  active + paused + never-successful watches / timestamps / redacted errors
  no files created, migrations, backend calls, or executor acquisition
uninstall [unit + macOS integration]
  loaded / already absent / bootout timeout / remaining registration
  preserve database, watches, secrets, logs, unrelated jobs and lock inode
```

All branches above require tests in the implementation plan. Standard offline pytest runs use temporary homes and fake service commands on Linux and macOS. A separate opt-in macOS integration uses a unique temporary service identity, fake backend, loopback receiver where needed, and installed wheel; it verifies registration, failure restart, graceful exit, status, and uninstall. Never install the user's real watches as a test. Add a macOS CI check for platform-specific tests; if hosted runners lack a GUI domain, report an explicit skip and require local isolated installed-package verification rather than calling the OS integration passed.

Run the full suite, Ruff, format check, mypy, strict documentation build, and package build before handoff. No real credentials/provider calls are needed for this feature's service tests. Baseline is release 0.7.20.

## Workstreams

| Workstream | Modules | Dependency |
| --- | --- | --- |
| Platform lifecycle and ownership | service controller + its tests | approved spec |
| Runner/config/logging | runner + CLI daemon output + tests | approved command/config contract |
| Read-only status and CLI integration | history + CLI + tests | controller/runner contracts |
| Integration, documentation, release verification | e2e + docs + CI | all implementation work |

Parallel work is useful for the first two lanes with explicit file ownership. CLI integration remains with one agent to avoid overlapping edits.

## NOT in scope

- Linux/Windows service adapters, root/system LaunchDaemons, pre-login startup: separate platform slices.
- GUI, HTTP API, IPC server, remote control, telemetry: separate discussion after this work.
- Keychain integration or automatic persistence of shell secrets: neither needed nor implicitly authorized.
- New monitoring features, higher watch limits, shorter intervals, catch-up bursts: existing contract remains.
- Guaranteed webhook delivery or external endpoint provisioning: existing notification semantics remain.
- Installing a live service for the user during development, merging, or releasing this feature without the corresponding handoff/authorization.

## Engineering refinements

Six findings are accepted under the user's standing instruction to take recommended technical decisions:

1. Read both service credential sources using descriptor-checked, bounded, no-follow reads. Reuse the existing config resolution through an optional pre-read TOML mapping rather than changing default foreground file semantics.
2. Manifest fields explicitly pin `config_home`, `backend`, `db_path`, `output_dir`, and `aiograpi_session_path`, plus `python`, `env_file`, `label`, format/ownership marker and uid. Apply pins after configuration resolution; subsequent TOML edits can rotate credentials but cannot retarget the service.
3. Clear inherited insto/provider settings, standard upper/lowercase HTTP proxy variables, and HTTP client CA-bundle environment overrides before resolving and running the service. Launch Python with `-I` to avoid inherited import-path control.
4. Service logging uses the existing formatter/rotation constants but a descriptor-safe rotating handler; validate its private directory and rotated siblings rather than relying on foreground setup's permissive fallback.
5. Reject NUL-containing environment values with a value-free error. Empty values follow existing semantics: no override/fallback for provider config and disabled webhook.
6. Credential-free status exposes `has_error`, never arbitrary historical `last_error` content. Keep stdout/JSON safe without trying to load potentially broken credentials solely for redaction.

The scope challenge accepts the file count: four production integration areas plus focused tests/docs/CI, not eight independent subsystems. Two implementation lanes can proceed with disjoint ownership; main integrates CLI/status and performs spec compliance followed by independent quality review. No new TODO is proposed beyond the explicitly deferred platform/GUI work.

## Platform evidence

- Apple's [Creating Launch Daemons and Agents](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html): per-user agents, login/logout lifecycle, foreground execution, ownership, and restart behavior.
- Locally inspected macOS 26.6.2 `launchctl(1)`: `gui/<uid>`, `bootstrap`, `bootout`, persistent `enable`/`disable`, and the warning that `print` output is not an API.
- Locally inspected `launchd.plist(5)`: `ProgramArguments`, `SuccessfulExit`, implicit `RunAtLoad`, restart throttling, absolute program paths, and working-directory semantics.

## GSTACK REVIEW REPORT

| Review | Runs | Status | Findings |
| --- | --- | --- | --- |
| Eng Review | 1 | CLEAR (PLAN) | 6 refinements accepted; no critical uncovered failure paths |
| Independent outside voice | 1 | COMPLETE | Five concrete security/configuration refinements included |
| CEO / UI design | 0 | Not required | Bounded local service feature; no GUI |

Scope accepted; architecture, code quality, tests, and performance evaluated. Test/failure map above covers all planned entrypoints; coverage is required implementation work, not claimed passing coverage. Performance is bounded by launchctl timeouts, 64 KiB inputs, rotating logs, and one read-only watch query. No provider calls on management paths. Baseline: 1175 tests pass on Python 3.12.11.

VERDICT: ENG CLEARED for implementation within this spec. No actual user service installation, merge, or release is authorized by this review.

NO UNRESOLVED DECISIONS
