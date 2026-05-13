# Read-only aiograpi Direct Design

Issue: https://github.com/subzeroid/insto/issues/4

## Goal

Expose the safe read-only subset of aiograpi Direct through insto without widening the product boundary into account automation.

This first PR implements only:

- `/direct [N]`: list recent Direct threads for the authenticated aiograpi account.
- `/direct-thread <thread_id> [N]`: show recent messages in one Direct thread.
- JSON export for both commands.

The PR does not implement Direct search, saved collections, personal feed, private GraphQL rewrites, polling, notifications, or any write operation.

## Product Boundary

Direct support is read-only. The CLI must not expose commands for sending messages, reacting, deleting, unsending, marking seen, muting, approving requests, updating titles, uploading attachments, or sharing media/profile/story objects.

This matters because insto is an OSINT read tool. Direct write actions turn it into an account automation tool with much higher account-risk and abuse potential.

## Command UX

`/direct [N]` lists threads. Default count is 20. It renders a compact table with:

- thread id
- title or participant usernames
- participant usernames
- last activity timestamp
- message count returned by aiograpi for the thread preview
- pending, archived, muted, group flags

`/direct-thread <thread_id> [N]` lists messages from a specific thread. Default count is 20. It renders:

- timestamp
- sender user id
- item type
- text preview when text exists
- shared media/code markers when the SDK exposes them cleanly

Both commands support `--json`. Both reject `--csv` through the existing non-flat export guard unless we explicitly add flat rows in a later PR.

## Data Model

Add DTOs to `insto.models`:

- `DirectThread`: stable thread metadata plus participants and preview messages.
- `DirectMessage`: stable message metadata plus a read-only summary of content references.

DTOs store only plain Python values. They never expose raw aiograpi objects above the backend layer.

The message body is still useful OSINT data, so JSON export includes text. The backend and commands must not log raw Direct payloads or message text.

## Backend Contract

Add optional methods to `OSINTBackend`, not abstract methods:

- `iter_direct_threads(limit: int | None = None)`
- `iter_direct_messages(thread_id: str, *, limit: int | None = None)`

Default behavior raises `BackendError("needs aiograpi backend")`. This keeps HikerAPI and future non-Instagram backends from inheriting raw `NotImplementedError` behavior.

`AiograpiBackend` implements the methods via:

- `client.direct_threads(amount=limit, thread_message_limit=1)`
- `client.direct_messages(thread_id, amount=limit)`

The implementation keeps lazy login and `_translate` error mapping unchanged.

## Capability Gate

Introduce a backend capability token for Direct reads, for example `direct_read`.

The aiograpi backend advertises it. HikerAPI and fake production backend do not. Command dispatch then rejects `/direct` and `/direct-thread` on unsupported backends before making any backend call.

Unit tests may enable the capability on `tests.fakes.FakeBackend` when testing command rendering.

## Tests

All normal tests stay offline. Live Instagram checks remain opt-in only.

Coverage required in this PR:

- DTO dataclass shape and `dataclasses.asdict` behavior.
- aiograpi mapper behavior for text messages, media-share messages, and empty/non-text messages.
- backend optional-method defaults raise typed `BackendError`.
- command capability gate rejects unsupported backends clearly.
- `/direct` renders thread rows using `FakeBackend`.
- `/direct-thread` renders message rows using `FakeBackend`.
- JSON export writes the expected envelope and stdout form.
- CSV rejection remains clear.

## Docs

Update:

- `docs/cli-reference.md`
- `docs/backends.md`
- `docs/roadmap.md`

Docs must state that Direct support requires `insto[aiograpi]`, logs into a real Instagram account, carries account-risk, and is read-only.

README only changes if the public command table needs to include Direct in the top-level command surface.

## Acceptance Criteria

- `/direct` works in REPL and one-shot dispatch on aiograpi.
- `/direct-thread` works in REPL and one-shot dispatch on aiograpi.
- Unsupported backends fail with a clear user-facing missing-capability message.
- JSON export works for both commands.
- No Direct write operation appears in command registration, facade API, docs, or tests.
- `ruff check`, `ruff format --check`, `mypy insto`, `pytest --cov=insto --cov-fail-under=75`, and `mkdocs build --strict` pass.
