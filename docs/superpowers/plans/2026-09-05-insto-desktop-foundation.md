# insto Desktop Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Добавить проверяемый desktop handshake и строгую проверку HikerAPI credential, сохранив совместимость текущего CLI и macOS-сервиса.

**Architecture:** `insto.desktop` — отдельный короткоживущий JSON entrypoint без CLI formatting и открытия runtime. HikerAPI validation остаётся в backend; GUI transport пока предоставляет только `hello`. Service controller поддерживает bytecode-free launch без автоматической замены legacy plist.

**Tech Stack:** Python 3.12 для proof, совместимость исходников Python >=3.11, stdlib JSON/asyncio, существующие pytest/httpx/HikerAPI.

---

## Граница и рабочая среда

Читать [спеку](../specs/2026-09-05-insto-gui-design.md) и [delivery map](2026-09-05-insto-gui-delivery-map.md). Это **C0**, не все desktop operations. Здесь нет сохранения токена, setup, CRUD наблюдений, GUI или live API-запросов.

Начальная основа — `docs/insto-gui-design` поверх `6d2dfd0`; документационная спецификация — `2386eeb`. Перед исполнением через using-git-worktrees создать отдельную `feat/desktop-foundation`, не переключать основной checkout и не менять sibling repositories. Только Astra. Команды ниже выполняются в implementation worktree.

Baseline: `uv sync --frozen --extra docs`, затем `.venv/bin/pytest -q`. Ожидается текущий полный offline suite; native smoke без opt-in пропускается. Не применять `uv run`, автоматически обновляющий текущий lock metadata. Никаких настоящих токенов.

## Карта файлов

| Файл | Единственная ответственность |
| --- | --- |
| `insto/desktop/__init__.py` | Обозначение отдельного private desktop package, без eager imports. |
| `insto/desktop/protocol.py` | Ограниченный input, envelope, request ID, статические safe errors. |
| `insto/desktop/dispatch.py` | Только объявленные capabilities и `hello`. |
| `insto/desktop/__main__.py` | stdin/stdout lifecycle одного запроса. |
| `insto/backends/hiker.py` | Дополнительный строгий `validate_access()`; soft refresh не меняется. |
| `insto/service/watch_service.py` | Два точных допустимых service argv для ownership-safe управления. |
| `tests/test_desktop_protocol.py` | Parser/limits/errors/response contract. |
| `tests/test_desktop_entrypoint.py` | Реальный subprocess, отсутствие side effects и eager SDK. |
| `tests/test_hiker_access.py` | Strict auth на MockTransport, без сети. |
| `tests/test_watch_service.py` | Legacy и bytecode-free plist compatibility. |
| `docs/desktop-protocol.md` | Реально доступные capabilities C0, не будущая API-реклама. |
| `mkdocs.yml` | Ссылка на новую страницу протокола в существующем nav. |

## Task 1: Строгий request и безопасный response

**Create:** `insto/desktop/__init__.py`, `insto/desktop/protocol.py`.
**Test:** `tests/test_desktop_protocol.py`.

- [ ] **Step 1: Добавить failing tests.** Полное начальное содержимое тестового файла:

```python
import json

import pytest

from insto.desktop.protocol import MAX_INPUT_BYTES, MAX_OUTPUT_BYTES, ProtocolError, decode, encode


def packet(**changes: object) -> bytes:
    value = {"protocol_version": 1, "request_id": "r-1", "operation": "hello", "params": {}}
    value.update(changes)
    return (json.dumps(value) + "\n").encode()


def test_decode_valid_request() -> None:
    request = decode(packet())
    assert (request.request_id, request.operation, request.params) == ("r-1", "hello", {})


@pytest.mark.parametrize("raw", [
    b"", b"{}", b"{}\n{}\n", b"\xff\n", b"[]\n",
    b'{"x":1,"x":2}\n', b'{"x":NaN}\n', b"[" * 2000 + b"\n",
    b"x" * (MAX_INPUT_BYTES + 1), packet(protocol_version=True),
    packet(request_id="secret with spaces"), packet(request_id="x" * 65),
    packet(params=[]), packet(extra="secret"),
])
def test_rejects_invalid_input_without_echo(raw: bytes) -> None:
    with pytest.raises(ProtocolError) as caught:
        decode(raw)
    assert str(caught.value) == "invalid_request"


def test_unsupported_version_has_safe_request_id() -> None:
    with pytest.raises(ProtocolError) as caught:
        decode(packet(protocol_version=2))
    assert caught.value.code == "unsupported_protocol"
    assert caught.value.request_id == "r-1"


def test_encode_is_one_line_and_refuses_nonfinite_output() -> None:
    assert encode({"value": "line\nline"}).count(b"\n") == 1
    with pytest.raises(ValueError):
        encode({"value": float("nan")})


def test_encode_enforces_output_limit() -> None:
    with pytest.raises(ValueError):
        encode({"value": "x" * MAX_OUTPUT_BYTES})
```

- [ ] **Step 2: Запустить red.** `.venv/bin/pytest tests/test_desktop_protocol.py -q` → collection error: `insto.desktop` отсутствует. Ошибка зависимостей или syntax error не считается ожидаемым red.
- [ ] **Step 3: Создать package и реализацию.** `__init__.py` содержит только `"""Private desktop bridge; capabilities are negotiated through hello."""`. Полный `protocol.py`:

```python
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

PROTOCOL_VERSION = 1
MAX_INPUT_BYTES = 64 * 1024
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


class ProtocolError(Exception):
    def __init__(self, code: str, request_id: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.request_id = request_id


@dataclass(frozen=True, slots=True)
class Request:
    request_id: str
    operation: str
    params: dict[str, Any]


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _constant(value: str) -> Any:
    raise ValueError("nonfinite JSON")


def decode(raw: bytes) -> Request:
    if not raw.endswith(b"\n") or raw.count(b"\n") != 1 or len(raw) > MAX_INPUT_BYTES:
        raise ProtocolError("invalid_request")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ProtocolError("invalid_request") from None
    keys = {"protocol_version", "request_id", "operation", "params"}
    if not isinstance(value, dict) or set(value) != keys:
        raise ProtocolError("invalid_request")
    request_id = value["request_id"]
    if not isinstance(request_id, str) or not _ID.fullmatch(request_id):
        raise ProtocolError("invalid_request")
    if type(value["protocol_version"]) is not int:
        raise ProtocolError("invalid_request")
    if value["protocol_version"] != PROTOCOL_VERSION:
        raise ProtocolError("unsupported_protocol", request_id)
    if not isinstance(value["operation"], str) or not isinstance(value["params"], dict):
        raise ProtocolError("invalid_request")
    return Request(request_id, value["operation"], value["params"])


def encode(value: dict[str, Any]) -> bytes:
    raw = (json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")) + "\n").encode()
    if len(raw) > MAX_OUTPUT_BYTES:
        raise ValueError("response too large")
    return raw
```

- [ ] **Step 4: Запустить green и форматирование.** `.venv/bin/ruff format insto/desktop tests/test_desktop_protocol.py`, затем `.venv/bin/pytest tests/test_desktop_protocol.py -q` → все cases проходят.
- [ ] **Step 5: Коммит.** `git add insto/desktop tests/test_desktop_protocol.py`, `git commit -m "feat: define bounded desktop protocol envelope"`.

## Task 2: Только hello, без config/database/provider

**Create:** `insto/desktop/dispatch.py`, `insto/desktop/__main__.py`, `tests/test_desktop_entrypoint.py`.

- [ ] **Step 1: Добавить failing subprocess и dispatch tests.**

```python
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from insto.desktop.dispatch import handle


def request(operation: str = "hello", params: object = None) -> bytes:
    return (json.dumps({"protocol_version": 1, "request_id": "probe-1",
                        "operation": operation, "params": {} if params is None else params}) + "\n").encode()


def test_capabilities_are_truthful() -> None:
    result = json.loads(asyncio.run(handle(request())))
    assert result["result"]["capabilities"] == ["hello"]
    assert result["result"]["schema_version_supported"] == 2
    assert result["request_id"] == "probe-1"


def test_unknown_operation_and_unexpected_params() -> None:
    for raw, expected in [(request("setup.configure"), "unsupported_operation"),
                          (request(params={"token": "sentinel-token"}), "invalid_params")]:
        result = asyncio.run(handle(raw))
        assert json.loads(result)["error"]["code"] == expected
        assert b"sentinel-token" not in result


def test_module_is_one_shot_without_sdk_or_home(tmp_path: Path) -> None:
    home = tmp_path / "not-created"
    env = {**os.environ, "INSTO_HOME": str(home)}
    script = (
        "import runpy,sys; runpy.run_module('insto.desktop',run_name='__main__'); "
        "assert not any(k.split('.')[0] in {'hikerapi','aiograpi'} for k in sys.modules)"
    )
    result = subprocess.run([sys.executable, "-I", "-B", "-c", script],
                            input=request(), capture_output=True, timeout=10, env=env)
    assert result.returncode == 0, result.stderr
    assert result.stderr == b"" and result.stdout.count(b"\n") == 1
    assert json.loads(result.stdout)["result"]["capabilities"] == ["hello"]
    assert not home.exists()
```

- [ ] **Step 2: Red.** `.venv/bin/pytest tests/test_desktop_entrypoint.py -q` → отсутствует `insto.desktop.dispatch`.
- [ ] **Step 3: Реализовать `dispatch.py`.** Ошибки не содержат exception repr или входные значения; malformed request ID возвращается как `null`.

```python
from __future__ import annotations

from typing import Any

from insto import __version__
from insto.desktop.protocol import PROTOCOL_VERSION, ProtocolError, Request, decode, encode
from insto.service.history import _SCHEMA_VERSION

_MESSAGES = {
    "invalid_request": "Invalid desktop request.",
    "unsupported_protocol": "Unsupported desktop protocol.",
    "unsupported_operation": "Unsupported desktop operation.",
    "invalid_params": "Invalid operation parameters.",
    "internal_error": "Desktop operation failed.",
}


async def dispatch(request: Request) -> dict[str, Any]:
    if request.operation != "hello":
        raise ProtocolError("unsupported_operation", request.request_id)
    if request.params:
        raise ProtocolError("invalid_params", request.request_id)
    return {"core_version": __version__, "schema_version_supported": _SCHEMA_VERSION,
            "capabilities": ["hello"]}


async def handle(raw: bytes) -> bytes:
    request_id: str | None = None
    try:
        request = decode(raw)
        request_id = request.request_id
        result = await dispatch(request)
        return encode({"protocol_version": PROTOCOL_VERSION, "request_id": request_id,
                       "result": result})
    except ProtocolError as error:
        code = error.code
        request_id = error.request_id
    except Exception:
        code = "internal_error"
    return encode({"protocol_version": PROTOCOL_VERSION, "request_id": request_id,
                   "error": {"code": code, "message": _MESSAGES[code], "retryable": False}})
```

`__main__.py` — полное содержимое:

```python
import asyncio
import sys

from insto.desktop.dispatch import handle
from insto.desktop.protocol import MAX_INPUT_BYTES


def main() -> None:
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    sys.stdout.buffer.write(asyncio.run(handle(raw)))
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
```

Parent обязан закрыть stdin после единственной строки. До EOF этот entrypoint может ждать; внешний 10-second process deadline будет ответственностью P1. Семантический error — корректный JSON response с exit 0; transport/nonzero exit не считается доменной ошибкой.

- [ ] **Step 4: Green.** `.venv/bin/ruff format insto/desktop tests/test_desktop_entrypoint.py`; `.venv/bin/pytest tests/test_desktop_protocol.py tests/test_desktop_entrypoint.py -q`; `.venv/bin/mypy insto/desktop` → pass.
- [ ] **Step 5: Коммит.** `git add insto/desktop tests/test_desktop_entrypoint.py`, `git commit -m "feat: expose side-effect-free desktop handshake"`.

## Task 3: Строгая проверка доступа HikerAPI без изменения REPL

**Modify:** `insto/backends/hiker.py`, рядом с `refresh_quota()`.
**Create:** `tests/test_hiker_access.py`.

- [ ] **Step 1: Добавить failing tests.** `validate_access()` возвращает `Quota`, ноль — действительный ответ о нулевом остатке, не зелёное состояние мониторинга. Маппинг в UI выполняется в C1.

```python
from collections.abc import Callable

import hikerapi
import httpx
import pytest

from insto.backends.hiker import HikerBackend
from insto.exceptions import AuthInvalid, QuotaExhausted, RateLimited, SchemaDrift, Transient
from tests.test_hiker_backend import _no_retry


async def backend(handler: Callable[[httpx.Request], httpx.Response]) -> HikerBackend:
    sdk = hikerapi.AsyncClient(token="test-credential", timeout=1)
    await sdk._client.aclose()
    sdk._client = httpx.AsyncClient(base_url=sdk._url, transport=httpx.MockTransport(handler))
    return HikerBackend(client=sdk, retry_decorator=_no_retry())


@pytest.mark.parametrize("remaining", [0, 1, 100])
async def test_valid_access_and_zero_quota(remaining: int) -> None:
    paths: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json={"requests": remaining})

    client = await backend(respond)
    try:
        quota = await client.validate_access()
        assert quota.remaining == remaining
        assert paths == ["/sys/balance"]
    finally:
        await client.aclose()


@pytest.mark.parametrize("payload", [[], {}, {"requests": True}, {"requests": -1},
                                     {"requests": "100"}, {"requests": None}])
async def test_unknown_schema_is_not_success(payload: object) -> None:
    client = await backend(lambda request: httpx.Response(200, json=payload))
    try:
        with pytest.raises(SchemaDrift):
            await client.validate_access()
    finally:
        await client.aclose()


@pytest.mark.parametrize("status,error", [(401, AuthInvalid), (402, QuotaExhausted),
                                         (429, RateLimited), (503, Transient)])
async def test_strict_errors_and_soft_refresh(status: int, error: type[Exception]) -> None:
    client = await backend(lambda request: httpx.Response(status, json={"error": "sentinel"}))
    try:
        with pytest.raises(error):
            await client.validate_access()
        assert (await client.refresh_quota()).remaining is None
    finally:
        await client.aclose()


async def test_network_error_is_not_invalid_token() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    client = await backend(fail)
    try:
        with pytest.raises(Transient):
            await client.validate_access()
    finally:
        await client.aclose()


async def test_non_json_success_is_schema_drift() -> None:
    client = await backend(lambda request: httpx.Response(200, content=b"not json"))
    try:
        with pytest.raises(SchemaDrift):
            await client.validate_access()
    finally:
        await client.aclose()
```

- [ ] **Step 2: Red.** `.venv/bin/pytest tests/test_hiker_access.py -q` → `HikerBackend` has no `validate_access`.
- [ ] **Step 3: Добавить метод в `HikerBackend`.** `_call` уже отвечает за retry/HTTP taxonomy; второй retry loop не создаётся. Не изменять существующий `refresh_quota()`.

```python
async def validate_access(self) -> Quota:
    """Validate access strictly; zero is a valid exhausted-balance result."""
    response = await self._call(partial(self._client._client.get, "/sys/balance"))
    try:
        data = response.json()
    except ValueError:
        raise self._record_drift(SchemaDrift("/sys/balance", "valid JSON object")) from None
    remaining = data.get("requests") if isinstance(data, dict) else None
    if type(remaining) is not int or remaining < 0:
        raise self._record_drift(SchemaDrift("/sys/balance", "nonnegative integer requests"))
    self._quota = Quota.with_remaining(remaining)
    return self._quota
```

Это метод внутри существующего класса; используемые `partial`, `Quota`, `SchemaDrift` уже импортированы. Метод намеренно не передаёт неизвестные optional SDK поля в DTO. C1 регистрирует token в redactor **до** создания клиента, ограничивает всю validation operation 30 секундами и закрывает backend в `finally`.

- [ ] **Step 4: Green.** `.venv/bin/ruff format insto/backends/hiker.py tests/test_hiker_access.py`; `.venv/bin/pytest tests/test_hiker_access.py tests/test_hiker_backend.py -q`; `.venv/bin/mypy insto/backends/hiker.py` → pass.
- [ ] **Step 5: Коммит.** `git add insto/backends/hiker.py tests/test_hiker_access.py`, `git commit -m "feat: validate HikerAPI access without swallowing failures"`.

## Task 4: Bytecode-free service без поломки legacy ownership

**Modify:** `insto/service/watch_service.py`, `tests/test_watch_service.py`, `tests/e2e/test_watch_service.py`.

- [ ] **Step 1: Добавить failing regression tests в `tests/test_watch_service.py`.** Здесь `_owned_artifacts` и `_result` — существующие helpers этого файла, не новые зависимости.

```python
@pytest.mark.parametrize("no_bytecode", [False, True])
async def test_uninstall_accepts_only_exact_owned_argv_variants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_bytecode: bool
) -> None:
    monkeypatch.setattr(watch_service.sys, "platform", "darwin")
    home, paths, _ = _owned_artifacts(tmp_path, monkeypatch, plist=True)
    manifest = watch_service.read_manifest(paths)
    document = watch_service._plist_document(paths, manifest, dont_write_bytecode=no_bytecode)
    paths.plist.write_bytes(plistlib.dumps(document))
    paths.plist.chmod(0o600)
    monkeypatch.setattr(watch_service.subprocess, "run",
                        lambda *a, **k: _result(1, err=b"Could not find service"))
    assert (await watch_service.uninstall_service(home=home))["installation"] == "not_installed"


def test_no_bytecode_follows_installing_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, paths, config = _owned_artifacts(tmp_path, monkeypatch, plist=True)
    monkeypatch.setattr(watch_service.sys, "dont_write_bytecode", True)
    _, raw = watch_service._desired(paths, config, None)
    assert plistlib.loads(raw)["ProgramArguments"][1:4] == ["-I", "-B", "-m"]
    monkeypatch.setattr(watch_service.sys, "dont_write_bytecode", False)
    _, legacy = watch_service._desired(paths, config, None)
    assert plistlib.loads(legacy)["ProgramArguments"][1:3] == ["-I", "-m"]


def test_owned_argv_match_rejects_additional_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, paths, _ = _owned_artifacts(tmp_path, monkeypatch, plist=True)
    manifest = watch_service.read_manifest(paths)
    document = watch_service._plist_document(paths, manifest, dont_write_bytecode=True)
    document["ProgramArguments"].insert(1, "-X")
    assert not watch_service._matches_owned_plist(paths, manifest, document)
```

- [ ] **Step 2: Red.** `.venv/bin/pytest tests/test_watch_service.py -k 'argv_variants or no_bytecode or additional_flags' -q` → отсутствующий keyword/helper.
- [ ] **Step 3: Изменить только создание argv и точное сравнение.** В `_desired()` заменить строку создания `plist` на:

```python
plist = _plist_document(paths, manifest, dont_write_bytecode=sys.dont_write_bytecode)
```

Полное новое содержимое `_plist_document`; остальные поля сохраняются:

```python
def _plist_document(
    paths: ServicePaths, manifest: dict[str, Any], *, dont_write_bytecode: bool = False
) -> dict[str, Any]:
    return {
        "Label": paths.label,
        "ProgramArguments": [
            manifest["python"],
            "-I",
            *(["-B"] if dont_write_bytecode else []),
            "-m",
            "insto.service.watch_service_runner",
            str(paths.manifest),
        ],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "WorkingDirectory": str(paths.home),
        "Umask": 0o77,
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": "/dev/null",
    }
```

Добавить отдельный helper перед `uninstall_service`:

```python
def _matches_owned_plist(
    paths: ServicePaths, manifest: dict[str, Any], document: object
) -> bool:
    return any(
        document == _plist_document(paths, manifest, dont_write_bytecode=flag)
        for flag in (False, True)
    )
```

Внутри `uninstall_service` заменить только ownership-condition:

```python
if not _matches_owned_plist(paths, manifest, actual_plist):
    raise BackendError("LaunchAgent plist ownership mismatch")
```

Manifest schema, management lock и остальные plist поля не меняются. Повторная установка legacy обычным CLI остаётся byte-exact no-op. Если явная установка запрошена из другого режима `-B`, существующее несовпадение всё ещё даёт отказ, а не неявную миграцию. GUI production bridge всегда запускается с `-I -B`.

- [ ] **Step 4: Расширить существующий opt-in native test на режим proof.** В `test_installed_launchagent_lifecycle` после выбора `python` определить:

```python
python_flags = ["-I"]
if os.environ.get("INSTO_TEST_NO_BYTECODE") == "1":
    python_flags.append("-B")
```

В helper `cli` его subprocess argv заменить на `[python, *python_flags, "-m", "insto", *args]`. У отдельной проверки `origin` заменить argv на `[python, *python_flags, "-c", "import insto; print(insto.__file__)"]`: иначе сама проверка создаст bytecode в runtime. В `verified_pid` заменить только `expected_suffix`:

```python
expected_suffix = f" {' '.join(python_flags)} -m insto.service.watch_service_runner {manifest}"
```

PID/lock/ps ownership assertions и `finally` cleanup не ослаблять. Это test-only env, production backend его не читает.

- [ ] **Step 5: Green и regression.** `.venv/bin/ruff format insto/service/watch_service.py tests/test_watch_service.py tests/e2e/test_watch_service.py`; `.venv/bin/pytest tests/test_watch_service.py tests/test_watch_service_runner.py tests/test_watch_service_cli.py -q` → pass. Native test выполняется в P0 с wheel-installed runtime, не из checkout.
- [ ] **Step 6: Коммит.** `git add insto/service/watch_service.py tests/test_watch_service.py tests/e2e/test_watch_service.py`, `git commit -m "feat: support bytecode-free managed service launches safely"`.

## Task 5: Документировать только работающий foundation и проверить wheel

**Create:** `docs/desktop-protocol.md`. **Modify:** `mkdocs.yml`.

- [ ] **Step 1: Добавить документ следующего содержания.**

```markdown
# Desktop protocol foundation

`python -I -B -m insto.desktop` accepts one UTF-8 JSON line on stdin, followed
by EOF, and emits one JSON line. The caller owns the process timeout.
There is no network listener and no shell command transport.

The request has exactly `protocol_version`, `request_id`, `operation`, and
`params`. Version is integer 1. IDs contain 1–64 ASCII letters, digits,
underscores or hyphens. Input is limited to 64 KiB, output to 2 MiB.
Duplicate JSON keys and nonfinite numbers are rejected.

Only `hello` with empty params is implemented. Its result includes
`core_version`, `schema_version_supported`, and `capabilities: ["hello"]`.
It does not load credentials, open a database or construct a provider.

Responses carry version and request ID, plus either result or
`error {code, message, retryable}`. Malformed IDs become null. Error messages
are static and never echo request values. A protocol error is an envelope
with exit 0; nonzero exit or malformed stdout is a transport failure.

Credential setup, watch mutations, snapshots and service management are not
yet desktop operations. The strict HikerBackend.validate_access() primitive
is backend-only until protected setup and IPC are implemented.

Installations initiated by an interpreter with bytecode writes disabled
preserve that setting in their LaunchAgent. Legacy registrations retain
their existing form. Removal accepts only either exact owned form.
```

- [ ] **Step 2: Включить страницу в nav.** В `mkdocs.yml` непосредственно после существующего `Architecture: architecture.md` добавить одну запись, сохранив остальной nav:

```yaml
  - Desktop protocol: desktop-protocol.md
```

- [ ] **Step 3: Запустить полный offline gate.**

```sh
.venv/bin/ruff check
.venv/bin/ruff format --check
.venv/bin/mypy insto
.venv/bin/pytest --cov=insto --cov-fail-under=75
.venv/bin/mkdocs build --strict
.venv/bin/python -m build
git diff --check
```

Ожидается exit 0 у каждой команды; opt-in native skip допустим только до P0. Не выключать strict docs при ошибке.

- [ ] **Step 4: Проверить содержимое wheel без установки.**

```sh
.venv/bin/python -c 'from pathlib import Path; from zipfile import ZipFile; wheels=list(Path("dist").glob("insto-*.whl")); assert len(wheels)==1; names=ZipFile(wheels[0]).namelist(); assert "insto/desktop/__main__.py" in names; assert "insto/desktop/protocol.py" in names; print(wheels[0])'
```

Если `dist` уже содержит несколько версий, использовать новый build output directory и повторить проверку, не удалять чужие артефакты.

- [ ] **Step 5: Коммит документа и handoff в P0.** `git add docs/desktop-protocol.md mkdocs.yml`, `git commit -m "docs: describe implemented desktop foundation"`. Зафиксировать SHA core commit и wheel SHA-256 в P0 build evidence. Не bump version, push, merge или release в рамках C0.

## Самопроверка покрытия C0

Task 1/2 покрывают handshake и границы protocol; Task 3 — strict access primitive; Task 4 — совместимость service argv; Task 5 — wheel и регрессии. C1, C2, P1 и GUI остаются следующими этапами delivery map. Наличие `hello` не считается выполнением token-only onboarding.
