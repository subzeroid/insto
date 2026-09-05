# Desktop protocol foundation

`python -I -B -m insto.desktop` accepts one UTF-8 JSON line on stdin, followed
by EOF, and emits one JSON line. The caller owns the process timeout and must
close stdin after the request. There is no network listener or shell transport.

This is the foundation for a desktop client, not a complete desktop API or GUI.
Only `hello` is currently available; it does not configure or start monitoring.

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
`schema_version_supported`, and `capabilities: ["hello"]`. Clients should check
capabilities, not assume every operation exists merely because protocol major
version 1 is supported. The handshake does not load credentials, open a database
or construct a provider.

Responses carry `protocol_version` and `request_id`, plus either `result` or
`error {code, message, retryable}`. Error messages are static and never echo
request values or exception text. Invalid requests have a null request ID;
unsupported protocol versions preserve a validated ID. Once dispatch starts,
errors preserve the decoded ID. Current error responses are not retryable.

| Code | Meaning |
| --- | --- |
| `invalid_request` | Invalid JSON, envelope, field type, ID or input budget. |
| `unsupported_protocol` | A different integer protocol version was requested. |
| `unsupported_operation` | The operation is not implemented. |
| `invalid_params` | The operation received unsupported parameters. |
| `internal_error` | The operation or response serialization failed. |

A protocol error is a JSON envelope with exit 0. A nonzero exit, malformed
stdout or caller timeout is a transport failure, not a successful operation.
The caller must independently bound stdout/stderr and process lifetime; a
10-second deadline is sufficient for `hello`. Child stderr is not a UI message.

## Strict HikerAPI access primitive

`HikerBackend.validate_access()` is backend-only. It uses the existing HTTP
transport and retry policy to check `/sys/balance`, returning a `Quota` with a
nonnegative integer `remaining`. Zero confirms an exhausted balance, not a
healthy monitor. Invalid credentials, quota/rate limits, temporary failures and
unknown response schemas remain errors. Non-success HTTP responses, including
redirects with balance-shaped JSON, never confirm access. Balance 403/404 are unconfirmed access,
not Instagram profile banned/not-found results.

The existing soft `refresh_quota()` behavior is unchanged. Credential setup,
token storage, watch mutations, snapshots and service management are not yet
desktop operations. A future setup caller must register the candidate token
with the redactor before constructing the client, enforce its operation budget,
and close the backend on all outcomes.

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
