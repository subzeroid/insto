# Read-only aiograpi Direct Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add read-only Direct thread and message commands for the aiograpi backend.

**Architecture:** Add Direct DTOs in `insto.models`, optional read methods on `OSINTBackend`, mapper helpers for aiograpi Direct payloads, thin facade methods, and a new `insto.commands.direct` module registered from `insto.commands.__init__`. The command layer exports JSON and renders tables, while capability gating prevents unsupported backends from executing the commands.

**Tech Stack:** Python dataclasses, async iterators, aiograpi 0.9.6, Rich rendering, pytest-asyncio, strict mypy, ruff.

---

## File Structure

- `insto/models.py`: add `DirectThread` and `DirectMessage` DTOs.
- `insto/backends/_base.py`: add optional Direct methods and import DTOs.
- `insto/backends/_aiograpi_map.py`: add mapper helpers for aiograpi Direct objects.
- `insto/backends/aiograpi.py`: advertise `direct_read`, call aiograpi Direct read methods, map results.
- `insto/service/facade.py`: add `direct_threads()` and `direct_messages()`.
- `insto/commands/direct.py`: add `/direct` and `/direct-thread`.
- `insto/commands/__init__.py`: import the new command module.
- `tests/fakes.py`: add Direct fixtures, errors, and iterators.
- `tests/test_models.py`: cover DTO shape.
- `tests/test_backend_contract.py`: cover optional default errors.
- `tests/test_aiograpi_map.py`: cover Direct mappers.
- `tests/test_commands_direct.py`: cover command render/export/capability behavior.
- `docs/cli-reference.md`, `docs/backends.md`, `docs/roadmap.md`, optionally `README.md`: document the new read-only commands.

## Task 1: Direct DTOs

**Files:**
- Modify: `insto/models.py`
- Test: `tests/test_models.py`

- [ ] **Step 1: Write failing DTO tests**

Add tests that import `DirectThread`, `DirectMessage`, and `User`, construct representative values, and assert `dataclasses.asdict()` returns stable plain dictionaries.

Expected fields:

```python
DirectMessage(
    pk="m1",
    thread_id="t1",
    sender_pk="100",
    timestamp=1_700_000_000,
    item_type="text",
    text="hello",
    media_pk=None,
    media_code=None,
    link_url=None,
)

DirectThread(
    pk="t1",
    title="Alice",
    users=[User(pk="100", username="alice")],
    last_activity_at=1_700_000_000,
    message_count=1,
    is_group=False,
    is_pending=False,
    is_archived=False,
    is_muted=False,
    messages=[message],
)
```

- [ ] **Step 2: Run the DTO tests and verify RED**

Run:

```bash
uv run pytest tests/test_models.py -q
```

Expected: import failure for `DirectThread` / `DirectMessage`.

- [ ] **Step 3: Implement DTOs**

Add dataclasses near the other Instagram DTOs in `insto/models.py`:

```python
@dataclass(slots=True)
class DirectMessage:
    """Read-only Direct message summary."""

    pk: str
    thread_id: str
    sender_pk: str
    timestamp: int
    item_type: str = ""
    text: str | None = None
    media_pk: str | None = None
    media_code: str | None = None
    link_url: str | None = None


@dataclass(slots=True)
class DirectThread:
    """Read-only Direct thread summary."""

    pk: str
    title: str
    users: list[User] = field(default_factory=list)
    last_activity_at: int = 0
    message_count: int = 0
    is_group: bool = False
    is_pending: bool = False
    is_archived: bool = False
    is_muted: bool = False
    messages: list[DirectMessage] = field(default_factory=list)
```

- [ ] **Step 4: Run DTO tests and commit**

Run:

```bash
uv run pytest tests/test_models.py -q
uv run mypy insto
```

Commit:

```bash
git add insto/models.py tests/test_models.py
git commit -m "feat: add direct read DTOs"
```

## Task 2: Backend Contract and FakeBackend

**Files:**
- Modify: `insto/backends/_base.py`
- Modify: `tests/fakes.py`
- Test: `tests/test_backend_contract.py`

- [ ] **Step 1: Write failing contract tests**

Add tests that call `OSINTBackend.iter_direct_threads()` and `iter_direct_messages()` through a minimal concrete test backend and assert `BackendError` with `needs aiograpi backend`.

Add fake-backend tests that configure `direct_threads` and `direct_messages` data and assert paging respects `limit`.

- [ ] **Step 2: Run contract tests and verify RED**

Run:

```bash
uv run pytest tests/test_backend_contract.py -q
```

Expected: missing methods or raw `NotImplementedError`.

- [ ] **Step 3: Implement optional backend methods**

In `OSINTBackend`, import `DirectMessage` and `DirectThread`, then add:

```python
def iter_direct_threads(self, *, limit: int | None = None) -> AsyncIterator[DirectThread]:
    """Iterate read-only Direct threads. Default requires aiograpi."""
    raise BackendError("needs aiograpi backend")


def iter_direct_messages(
    self, thread_id: str, *, limit: int | None = None
) -> AsyncIterator[DirectMessage]:
    """Iterate read-only Direct messages in one thread. Default requires aiograpi."""
    raise BackendError("needs aiograpi backend")
```

- [ ] **Step 4: Extend `tests.fakes.FakeBackend`**

Add `iter_direct_threads` and `iter_direct_messages` error slots, fixture fields, request logging, and paged iterators.

Use dictionaries keyed by thread id:

```python
direct_threads: list[DirectThread] = field(default_factory=list)
direct_messages: dict[str, list[DirectMessage]] = field(default_factory=dict)
```

- [ ] **Step 5: Run contract tests and commit**

Run:

```bash
uv run pytest tests/test_backend_contract.py -q
uv run pytest tests/test_commands_base.py::test_dispatch_rejects_command_requiring_missing_capability -q
uv run mypy insto
```

Commit:

```bash
git add insto/backends/_base.py tests/fakes.py tests/test_backend_contract.py
git commit -m "feat: add direct backend contract"
```

## Task 3: aiograpi Direct Mappers

**Files:**
- Modify: `insto/backends/_aiograpi_map.py`
- Test: `tests/test_aiograpi_map.py`

- [ ] **Step 1: Write failing mapper tests**

Use lightweight objects with attributes matching aiograpi Direct models. Cover:

- text message maps `text`.
- media share maps `media_pk` and `media_code` when present.
- non-text message maps `item_type` and leaves text fields `None`.
- thread maps users, title, flags, last activity timestamp, and preview messages.

- [ ] **Step 2: Run mapper tests and verify RED**

Run:

```bash
uv run pytest tests/test_aiograpi_map.py -q
```

Expected: mapper function import failure.

- [ ] **Step 3: Implement mapper helpers**

Add:

```python
def _direct_ts(raw: Any, attr: str) -> int:
    value = getattr(raw, attr, None)
    if hasattr(value, "timestamp"):
        return int(value.timestamp())
    if isinstance(value, int | float):
        return int(value)
    return 0


def _maybe_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def map_direct_message(raw: Any, *, thread_id: str | None = None) -> DirectMessage:
    media = getattr(raw, "media_share", None) or getattr(raw, "clip", None)
    link = getattr(raw, "link", None)
    return DirectMessage(
        pk=str(getattr(raw, "id", "")),
        thread_id=str(getattr(raw, "thread_id", None) or thread_id or ""),
        sender_pk=str(getattr(raw, "user_id", "") or ""),
        timestamp=_direct_ts(raw, "timestamp"),
        item_type=str(getattr(raw, "item_type", "") or ""),
        text=getattr(raw, "text", None),
        media_pk=_maybe_str(getattr(media, "pk", None) or getattr(media, "id", None))
        if media is not None
        else None,
        media_code=_maybe_str(getattr(media, "code", None)) if media is not None else None,
        link_url=_maybe_str(getattr(link, "url", None)) if link is not None else None,
    )


def map_direct_thread(raw: Any) -> DirectThread:
    messages = [map_direct_message(msg, thread_id=str(getattr(raw, "id", ""))) for msg in getattr(raw, "messages", [])]
    users = [
        User(
            pk=str(getattr(user, "pk", "")),
            username=str(getattr(user, "username", "") or ""),
            full_name=str(getattr(user, "full_name", "") or ""),
            is_private=bool(getattr(user, "is_private", False) or False),
        )
        for user in getattr(raw, "users", [])
    ]
    return DirectThread(
        pk=str(getattr(raw, "id", None) or getattr(raw, "pk", "")),
        title=str(getattr(raw, "thread_title", "") or ""),
        users=users,
        last_activity_at=_direct_ts(raw, "last_activity_at"),
        message_count=len(messages),
        is_group=bool(getattr(raw, "is_group", False)),
        is_pending=bool(getattr(raw, "pending", False)),
        is_archived=bool(getattr(raw, "archived", False)),
        is_muted=bool(getattr(raw, "muted", False)),
        messages=messages,
    )
```

Rules:

- Convert `datetime` values to Unix seconds with `int(dt.timestamp())`.
- Prefer `raw.thread_id`, fallback to explicit `thread_id`, fallback to empty string.
- Convert `raw.user_id` to `sender_pk`, fallback to empty string.
- Extract `media_pk` and `media_code` from `media_share` or `clip` only when attributes exist.
- Extract `link_url` from `link.url` when present.
- Do not retain raw aiograpi objects.

- [ ] **Step 4: Run mapper tests and commit**

Run:

```bash
uv run pytest tests/test_aiograpi_map.py -q
uv run mypy insto
```

Commit:

```bash
git add insto/backends/_aiograpi_map.py tests/test_aiograpi_map.py
git commit -m "feat: map aiograpi direct payloads"
```

## Task 4: Aiograpi Backend Methods

**Files:**
- Modify: `insto/backends/aiograpi.py`
- Test: `tests/test_aiograpi_map.py` and existing backend import tests

- [ ] **Step 1: Write backend behavior tests if a client stub exists**

If the current test suite has aiograpi backend client stubs, add tests proving:

- `iter_direct_threads(limit=3)` calls `client.direct_threads(amount=3, thread_message_limit=1)`.
- `iter_direct_messages("123", limit=5)` calls `client.direct_messages(123, amount=5)`.
- errors pass through `_translate`.

If there is no existing client-stub pattern, keep this coverage in mapper and command tests to avoid inventing a large test harness in this PR.

- [ ] **Step 2: Implement capability and methods**

In `AiograpiBackend`:

```python
capabilities = frozenset({"followed", "direct_read"})
```

Add async generator methods:

```python
async def iter_direct_threads(
    self, *, limit: int | None = None
) -> AsyncIterator[DirectThread]:
    amount = limit if limit is not None and limit > 0 else 20
    raws = await self._call(
        lambda: self._client.direct_threads(amount=amount, thread_message_limit=1)
    )
    for raw in raws:
        yield map_direct_thread(raw)


async def iter_direct_messages(
    self, thread_id: str, *, limit: int | None = None
) -> AsyncIterator[DirectMessage]:
    amount = limit if limit is not None and limit > 0 else 20
    raws = await self._call(lambda: self._client.direct_messages(int(thread_id), amount=amount))
    for raw in raws:
        yield map_direct_message(raw, thread_id=thread_id)
```

Reject non-numeric thread ids with `BackendError(f"invalid direct thread id: {thread_id!r}")`.

- [ ] **Step 3: Run backend-adjacent tests and commit**

Run:

```bash
uv run pytest tests/test_aiograpi_map.py tests/test_backend_contract.py -q
uv run mypy insto
```

Commit:

```bash
git add insto/backends/aiograpi.py tests/test_aiograpi_map.py
git commit -m "feat: wire aiograpi direct read methods"
```

## Task 5: Facade and Commands

**Files:**
- Modify: `insto/service/facade.py`
- Create: `insto/commands/direct.py`
- Modify: `insto/commands/__init__.py`
- Test: `tests/test_commands_direct.py`

- [ ] **Step 1: Write failing command tests**

Create `tests/test_commands_direct.py` with tests for:

- `/direct 2` renders two threads.
- `/direct-thread t1 2` renders two messages.
- `/direct --json` writes `output/direct/direct.json` or equivalent default path chosen by `default_export_path`.
- `/direct --json -` writes a valid JSON envelope to stdout.
- `/direct --csv -` is rejected by the existing CSV guard.
- unsupported backend without `direct_read` fails before backend calls.

- [ ] **Step 2: Run command tests and verify RED**

Run:

```bash
uv run pytest tests/test_commands_direct.py -q
```

Expected: unknown command `/direct`.

- [ ] **Step 3: Add facade methods**

Add to `OsintFacade`:

```python
async def direct_threads(self, *, limit: int = 20) -> list[DirectThread]:
    return [t async for t in self.backend.iter_direct_threads(limit=limit)]


async def direct_messages(self, thread_id: str, *, limit: int = 20) -> list[DirectMessage]:
    return [m async for m in self.backend.iter_direct_messages(thread_id, limit=limit)]
```

- [ ] **Step 4: Add direct command module**

Implement `insto/commands/direct.py` with:

- `_add_direct_args(parser)`: optional count default 20.
- `_add_direct_thread_args(parser)`: `thread_id` plus optional count default 20.
- `_resolve_count(ctx, default=20)`: global `--limit` wins.
- `direct_cmd`: `@command("direct", "List read-only Direct threads (aiograpi only)", add_args=_add_direct_args, requires=("direct_read",))`.
- `direct_thread_cmd`: `@command("direct-thread", "Show read-only Direct messages for one thread (aiograpi only)", add_args=_add_direct_thread_args, requires=("direct_read",))`.

Use `dataclasses.asdict()` for JSON export.

- [ ] **Step 5: Register command module**

Add to `insto/commands/__init__.py`:

```python
from insto.commands import direct as _direct  # noqa: F401  (registers commands)
```

- [ ] **Step 6: Run command tests and commit**

Run:

```bash
uv run pytest tests/test_commands_direct.py tests/test_commands_base.py -q
uv run mypy insto
```

Commit:

```bash
git add insto/service/facade.py insto/commands/direct.py insto/commands/__init__.py tests/test_commands_direct.py
git commit -m "feat: add read-only direct commands"
```

## Task 6: Docs and Final Verification

**Files:**
- Modify: `docs/cli-reference.md`
- Modify: `docs/backends.md`
- Modify: `docs/roadmap.md`
- Modify: `README.md` if the command table should include Direct

- [ ] **Step 1: Update docs**

Document:

- `/direct [N]`
- `/direct-thread <thread_id> [N]`
- aiograpi-only requirement
- read-only boundary
- account-ban risk
- no Direct write operations

- [ ] **Step 2: Run docs build**

Run:

```bash
uv run --extra docs mkdocs build --strict --site-dir /tmp/insto-mkdocs-site
```

Expected: exit code 0. Existing Material for MkDocs warning is acceptable if the build exits 0.

- [ ] **Step 3: Run full verification**

Run:

```bash
uv run ruff check
uv run ruff format --check
uv run mypy insto
uv run pytest --cov=insto --cov-fail-under=75
uv run --extra docs mkdocs build --strict --site-dir /tmp/insto-mkdocs-site
```

- [ ] **Step 4: Guardrail scan for Direct write operations**

Run:

```bash
rg -n "direct_(send|answer|delete|unsend|like|unlike|seen|mute|approve|hide|create|update|upload|share)|message_seen|mark_unread|send_seen" insto docs tests
```

Expected: no production command exposure. Mapper tests may mention forbidden method names only if asserting they are absent.

- [ ] **Step 5: Commit docs**

Commit:

```bash
git add docs/cli-reference.md docs/backends.md docs/roadmap.md README.md
git commit -m "docs: document read-only direct commands"
```

## Task 7: PR Prep

**Files:**
- No source changes unless verification finds issues.

- [ ] **Step 1: Push branch**

Run:

```bash
git push -u origin feat/aiograpi-direct-readonly
```

- [ ] **Step 2: Create PR**

Use title:

```text
feat: add read-only aiograpi direct commands
```

PR body must include:

- Closes #4 partially.
- Scope: `/direct`, `/direct-thread`, JSON export, read-only aiograpi Direct.
- Non-goals: Direct writes, Direct search, saved/feed, private GraphQL rewrites.
- Verification commands and outputs.

- [ ] **Step 3: Wait for CI**

Run:

```bash
gh pr checks --watch --fail-fast
```

Expected: CI success.
