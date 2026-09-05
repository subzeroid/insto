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
