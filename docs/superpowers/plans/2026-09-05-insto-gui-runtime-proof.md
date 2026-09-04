# insto-gui Runtime Portability Proof Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Получить воспроизводимо описанное автономное дерево CPython + insto, доказать запуск после перемещения и работу изолированной macOS-службы на этом runtime.

**Architecture:** Build-only Python tools готовят wheel-installed runtime с фиксированными upstream inputs и manifest содержимого. Проверка использует `-I -B`, чистое окружение и реальный перемещённый runtime. Это P0 developer proof; Tauri auto-publisher, token screen и подписанный app проверяются отдельно в P1/R1.

**Tech Stack:** CPython 3.12.14 python-build-standalone 20260901, существующий insto wheel, hash-locked Python dependencies, stdlib tarfile/hashlib/subprocess/unittest, macOS launchd.

---

## Предусловия и границы

Сначала выполнить [C0](2026-09-05-insto-desktop-foundation.md). Все новые файлы этого плана относятся к **отдельному** `<workspace>/insto-gui`, которого ещё нет. Перед созданием проверить точное отсутствие каталога; если он появился и содержит пользовательскую работу, не перезаписывать, сначала изучить состояние.

В этой фазе не создавать обманчивый пустой GUI и не подключать UI к shell. Developer tools могут использовать установленный Python/uv и сеть **на build machine**. Пользовательский установщик никогда не запускает эти инструменты. Только Astra.

Сначала доказать host-архитектуру. Вторая архитектура выполняет тот же план на соответствующем Mac/runner; запуск arm64 доказательства не считается x86_64 acceptance.

## Карта файлов нового репозитория

| Файл | Назначение |
| --- | --- |
| `README.md`, `.gitignore` | Честный статус proof и изоляция build outputs. |
| `packaging/python-distributions.json` | Exact URL + SHA-256 двух CPython payloads. |
| `packaging/README.md` | Только developer команды, источники, ограничения proof. |
| `scripts/__init__.py` | Импорт build helpers в unittest. |
| `scripts/runtime_manifest.py` | Снимок файлов нормализованного runtime и проверка изменений. |
| `scripts/prepare_runtime.py` | Download/hash/extract, install locked wheels, metadata. |
| `scripts/probe_runtime.py` | Read-only запуск перемещённого runtime без developer environment. |
| `tests/test_runtime_manifest.py` | Traversal/symlink/type/change tests. |
| `tests/test_prepare_runtime.py` | Archive budget/filter и upstream input checks. |
| `tests/test_probe_runtime.py` | Subprocess recipe без реального Python payload. |
| `.build/` | Игнорируемые outputs: runtime, dependency export и evidence. |

## Task 1: Зафиксировать exact inputs и создать build-only проект

**Create:** `README.md`, `.gitignore`, `packaging/python-distributions.json`, `scripts/__init__.py`.

- [ ] **Step 1: Проверить отсутствие repo, затем создать отдельный git repository.**

```sh
test ! -e <workspace>/insto-gui
mkdir <workspace>/insto-gui
git -C <workspace>/insto-gui init -b main
```

Выполнять команды по одной, останавливаясь при ненулевом exit. Не выполнять shell bootstrap из сети. Последующие команды — из нового repo. Файлы создавать через apply_patch.

`README.md`:

```markdown
# insto-gui

Self-contained macOS monitoring app, currently at runtime portability proof.
The GUI and token-only onboarding are not implemented yet.

The app will bundle a compatible Python insto core. Users will not install
Python, uv or the CLI separately. Developer packaging commands live in
packaging/README.md and are not user installation instructions.
```

`.gitignore`:

```gitignore
.build/
.venv/
__pycache__/
*.pyc
.DS_Store
```

`scripts/__init__.py` содержит только `"""Build-time tools; never called by an installed app."""`.

`packaging/python-distributions.json` — inputs, полученные через официальный GitHub releases API, не выдуманные digest:

```json
{
  "python_version": "3.12.14",
  "release": "20260901",
  "targets": {
    "arm64": {
      "url": "https://github.com/astral-sh/python-build-standalone/releases/download/20260901/cpython-3.12.14%2B20260901-aarch64-apple-darwin-install_only_stripped.tar.gz",
      "sha256": "81a359f1cfadd4da11766534c5913791cea55f26e1bb902cacd2a531bb1e4b2b"
    },
    "x86_64": {
      "url": "https://github.com/astral-sh/python-build-standalone/releases/download/20260901/cpython-3.12.14%2B20260901-x86_64-apple-darwin-install_only_stripped.tar.gz",
      "sha256": "65b195c9cedc1fef6767f044f9822069adbd1bd9204d424ece4628776fdc04bb"
    }
  }
}
```

- [ ] **Step 2: Проверить JSON.** `python3 -m json.tool packaging/python-distributions.json` → валидный JSON с обеими архитектурами. `python3` здесь — developer interpreter >=3.12; если отсутствует, использовать Python из C0 `.venv`, не предлагать его будущему пользователю.
- [ ] **Step 3: Коммит.** `git add README.md .gitignore packaging/python-distributions.json scripts/__init__.py`, `git commit -m "chore: establish standalone runtime proof inputs"`.

## Task 2: Manifest нормализованных файлов

**Create:** `scripts/runtime_manifest.py`, `tests/test_runtime_manifest.py`.

- [ ] **Step 1: Failing tests.**

```python
import tempfile
import unittest
from pathlib import Path

from scripts.runtime_manifest import describe, verify


class ManifestTests(unittest.TestCase):
    def test_detects_changes_and_extra_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "entry").write_bytes(b"payload")
            entries = describe(root)
            verify(root, entries)
            (root / "entry").write_bytes(b"changed")
            with self.assertRaises(ValueError):
                verify(root, entries)
            (root / "entry").write_bytes(b"payload")
            (root / "extra").write_bytes(b"unlisted")
            with self.assertRaises(ValueError):
                verify(root, entries)

    def test_normalized_payload_rejects_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "real").write_bytes(b"payload")
            (root / "link").symlink_to("real")
            with self.assertRaises(ValueError):
                describe(root)

    def test_mode_is_part_of_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "entry"
            path.write_bytes(b"payload")
            path.chmod(0o600)
            entries = describe(root)
            path.chmod(0o700)
            with self.assertRaises(ValueError):
                verify(root, entries)
```

- [ ] **Step 2: Red.** `python3 -m unittest discover -s tests -p 'test_runtime_manifest.py' -v` → `scripts.runtime_manifest` отсутствует.
- [ ] **Step 3: Полное содержимое `runtime_manifest.py`.** P0 нормализует допустимые internal archive symlinks в обычные файлы на build machine. Это более узкий допустимый payload, чем production publisher со spec-level symlink support; внешние links никогда не следуются.

```python
from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import Any


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def describe(root: Path) -> list[dict[str, Any]]:
    if not root.is_dir() or root.is_symlink():
        raise ValueError("runtime root must be an ordinary directory")
    entries: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        if info.st_uid != os.getuid() or info.st_mode & 0o7000:
            raise ValueError("unsafe runtime owner or special permissions")
        relative = path.relative_to(root).as_posix()
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISDIR(info.st_mode):
            entries.append({"path": relative, "type": "directory", "mode": mode})
        elif stat.S_ISREG(info.st_mode):
            entries.append({"path": relative, "type": "file", "mode": mode,
                            "size": info.st_size, "sha256": sha256(path)})
        else:
            raise ValueError("runtime payload is not normalized")
    return entries


def verify(root: Path, entries: list[dict[str, Any]]) -> None:
    if describe(root) != entries:
        raise ValueError("runtime manifest mismatch")
```

- [ ] **Step 4: Green.** Повторить unittest command → 3 tests pass.
- [ ] **Step 5: Коммит.** `git add scripts/runtime_manifest.py tests/test_runtime_manifest.py`, `git commit -m "build: describe and verify normalized runtime payloads"`.

## Task 3: Безопасно распаковать фиксированный upstream archive

**Create:** `scripts/prepare_runtime.py`, `tests/test_prepare_runtime.py`.

- [ ] **Step 1: Добавить failing tests до implementation.**

```python
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

from scripts.prepare_runtime import unpack


class ArchiveTests(unittest.TestCase):
    def archive(self, root: Path, name: str, *, link: str | None = None) -> Path:
        archive = root / "input.tar.gz"
        with tarfile.open(archive, "w:gz") as target:
            entry = tarfile.TarInfo(name)
            if link is not None:
                entry.type = tarfile.SYMTYPE
                entry.linkname = link
                target.addfile(entry)
            else:
                entry.size = 2
                target.addfile(entry, io.BytesIO(b"ok"))
        return archive

    def test_regular_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unpack(self.archive(root, "python/bin/probe"), root / "extract")
            self.assertEqual((root / "extract/python/bin/probe").read_bytes(), b"ok")

    def test_rejects_traversal_absolute_and_external_link(self) -> None:
        for name, link in [("../outside", None), ("/outside", None),
                           ("python/bin/link", "../../../outside")]:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with self.assertRaises((ValueError, tarfile.FilterError)):
                    unpack(self.archive(root, name, link=link), root / "extract")

    def test_upstream_inputs_are_complete(self) -> None:
        root = Path(__file__).resolve().parents[1]
        data = json.loads((root / "packaging/python-distributions.json").read_text())
        self.assertEqual(set(data["targets"]), {"arm64", "x86_64"})
        for target in data["targets"].values():
            self.assertEqual(len(bytes.fromhex(target["sha256"])), 32)
            self.assertTrue(target["url"].startswith(
                "https://github.com/astral-sh/python-build-standalone/releases/download/"))
```

- [ ] **Step 2: Red.** `python3 -m unittest discover -s tests -p 'test_prepare_runtime.py' -v` → отсутствует module.
- [ ] **Step 3: Начальное содержимое `prepare_runtime.py`.**

```python
from __future__ import annotations

import tarfile
from pathlib import Path, PurePosixPath


def unpack(archive: Path, destination: Path) -> None:
    destination.mkdir(mode=0o700)
    with tarfile.open(archive, "r:gz") as source:
        members = source.getmembers()
        if len(members) > 50000 or sum(item.size for item in members) > 1024**3:
            raise ValueError("archive exceeds build budget")
        for item in members:
            name = PurePosixPath(item.name)
            if name.is_absolute() or ".." in name.parts or not name.parts or name.parts[0] != "python":
                raise ValueError("archive path outside python tree")
            if not (item.isfile() or item.isdir() or item.issym()) or item.size > 256 * 1024**2:
                raise ValueError("unsupported archive entry")
            if item.issym():
                target = PurePosixPath(item.linkname)
                if target.is_absolute():
                    raise ValueError("absolute archive link")
                resolved = (destination / name.parent / item.linkname).resolve()
                if not resolved.is_relative_to((destination / "python").resolve()):
                    raise ValueError("external archive link")
        source.extractall(destination, members=members, filter="data")
    python_root = (destination / "python").resolve()
    for path in python_root.rglob("*"):
        if path.is_symlink() and not path.resolve(strict=True).is_relative_to(python_root):
            raise ValueError("resolved archive link escapes python tree")
```

`tarfile` data filter — дополнительная защита, не замена проверке фиксированного digest. Broken/cyclic links завершают build ошибкой; отказ не обходить снятием проверок. [Python tar extraction filters](https://docs.python.org/3.12/library/tarfile.html#extraction-filters) описывает поведение фильтра.

- [ ] **Step 4: Green.** Повторить unittest command → 3 tests pass.
- [ ] **Step 5: Коммит.** `git add scripts/prepare_runtime.py tests/test_prepare_runtime.py`, `git commit -m "build: constrain standalone Python archive extraction"`.

## Task 4: Собрать wheel-installed runtime на build machine

**Modify:** `scripts/prepare_runtime.py`, `tests/test_prepare_runtime.py`.

- [ ] **Step 1: Добавить red на digest mismatch.** В `tests/test_prepare_runtime.py` добавить import `patch` и заменить прежний import `unpack` следующими строками:

```python
from unittest.mock import patch

from scripts.prepare_runtime import fetch, unpack
```

Затем добавить метод в `ArchiveTests`:

```python
def test_download_hash_mismatch_is_fatal(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
        with patch("urllib.request.urlopen", return_value=io.BytesIO(b"wrong payload")):
            with self.assertRaisesRegex(ValueError, "digest"):
                fetch("https://github.com/asset", "0" * 64, Path(directory) / "download")
```

- [ ] **Step 2: Red.** `python3 -m unittest discover -s tests -p 'test_prepare_runtime.py' -v` → отсутствует `fetch`.
- [ ] **Step 3: Дополнить `prepare_runtime.py`.** Добавить эти imports к начальным imports Task 3:

```python
import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.request

from scripts.runtime_manifest import describe, sha256
```

После `unpack()` добавить весь следующий код:

```python
REPO = Path(__file__).resolve().parents[1]


def fetch(url: str, expected: str, output: Path) -> None:
    size = 0
    with urllib.request.urlopen(url, timeout=30) as response, output.open("xb") as stream:
        while block := response.read(1024 * 1024):
            size += len(block)
            if size > 256 * 1024**2:
                raise ValueError("download exceeds build budget")
            stream.write(block)
    if sha256(output) != expected:
        raise ValueError("upstream digest mismatch")


def run(arguments: list[str], *, cwd: Path) -> str:
    completed = subprocess.run(arguments, cwd=cwd, check=True, timeout=600,
                               text=True, capture_output=True)
    return completed.stdout.strip()


def build(core: Path, output: Path) -> None:
    if sys.version_info < (3, 12) or sys.platform != "darwin":
        raise ValueError("proof builder requires Python >=3.12 on macOS")
    core = core.resolve(strict=True)
    if run(["git", "status", "--porcelain"], cwd=core):
        raise ValueError("core checkout must be clean before packaging")
    commit = run(["git", "rev-parse", "HEAD"], cwd=core)
    inputs = json.loads((REPO / "packaging/python-distributions.json").read_text())
    architecture = platform.machine()
    target = inputs["targets"][architecture]
    root = REPO / ".build"
    if root.is_symlink():
        raise ValueError("build root cannot be a symlink")
    root.mkdir(mode=0o700, exist_ok=True)
    output = output.absolute()
    if output.parent.resolve() != root.resolve() or os.path.lexists(output):
        raise ValueError("output must be a new immediate child of .build")
    output.mkdir(mode=0o700)
    try:
        with tempfile.TemporaryDirectory(prefix="insto-build-", dir=root) as temporary:
            work = Path(temporary)
            archive = work / "python.tar.gz"
            fetch(target["url"], target["sha256"], archive)
            unpack(archive, work / "extracted")
            source = work / "extracted/python"
            for path in source.rglob("*"):
                if path.is_symlink() and not path.resolve(strict=True).is_file():
                    raise ValueError("P0 normalization supports only internal file symlinks")
            runtime = output / "python"
            shutil.copytree(source, runtime, symlinks=False)
            python = str(runtime / "bin/python3")
            requirements = output / "requirements.txt"
            run(["uv", "export", "--frozen", "--no-dev", "--no-default-groups",
                 "--no-emit-project", "--no-header", "--output-file", str(requirements)], cwd=core)
            wheels = work / "wheels"
            run(["uv", "build", "--wheel", "--out-dir", str(wheels)], cwd=core)
            candidates = list(wheels.glob("insto-*.whl"))
            if len(candidates) != 1:
                raise ValueError("build must produce exactly one insto wheel")
            wheel = candidates[0]
            run([python, "-I", "-B", "-m", "ensurepip"], cwd=work)
            pip = [python, "-I", "-B", "-m", "pip", "--isolated", "--disable-pip-version-check"]
            run([*pip, "install", "--no-compile", "--only-binary=:all:", "--require-hashes",
                 "-r", str(requirements)], cwd=work)
            run([*pip, "install", "--no-compile", "--no-deps", str(wheel)], cwd=work)
            run([*pip, "check"], cwd=work)
            metadata = {"core_commit": commit, "core_wheel": wheel.name,
                        "core_wheel_sha256": sha256(wheel),
                        "requirements_sha256": sha256(requirements),
                        "architecture": architecture, "python_version": inputs["python_version"],
                        "upstream_url": target["url"], "upstream_sha256": target["sha256"]}
            manifest = {"manifest_version": 1, "inputs": metadata, "files": describe(runtime)}
            canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
            manifest["build_id"] = hashlib.sha256(canonical).hexdigest()
            (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    except BaseException:
        print(f"Incomplete proof retained at {output}; do not distribute it.", file=sys.stderr)
        raise
    print(output)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--core-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    build(args.core_root, args.output)


if __name__ == "__main__":
    main()
```

Запускать только как module (`python3 -m scripts.prepare_runtime`), чтобы `scripts` imports не зависели от sys.path трюков. Не запускать builder в GUI. `manifest.json` появляется последним; отсутствие manifest означает incomplete build. Здесь нет production atomic publication, ownership adoption или automatic recovery — это P1.

`run()` предназначен только для доверенных build commands, не подходит для secret-bearing production bridge или bounded app IPC. На build machine не задавать настоящий HikerAPI-токен; build stdout не публиковать как telemetry.

- [ ] **Step 4: Green unit suite.** `python3 -m unittest discover -s tests -v` → все manifest/archive tests pass.
- [ ] **Step 5: Выполнить реальный build.** Проверить фактический C0 worktree путь перед командой:

```sh
python3 -m scripts.prepare_runtime --core-root <worktrees>/desktop-foundation --output .build/runtime-host-01
```

Ожидается exit 0, `.build/runtime-host-01/python/bin/python3`, requirements с hashes и manifest. Если chosen upstream больше не доступен или не проходит фильтры/импорты, остановить proof с evidence; не молча переходить на другой Python или снимать hash/изоляцию. Если output уже существует, выбрать новую именованную директорию вроде `.build/runtime-host-02`, не перезаписывать его.

- [ ] **Step 6: Коммит.** `git add scripts/prepare_runtime.py tests/test_prepare_runtime.py`, `git commit -m "build: assemble pinned standalone insto runtime"`. `.build` и бинарные payloads не коммитить.

## Task 5: Проверить настоящий runtime после перемещения

**Create:** `scripts/probe_runtime.py`, `tests/test_probe_runtime.py`.

- [ ] **Step 1: Failing test на fixed invocation и environment.**

```python
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.probe_runtime import handshake


class ProbeTests(unittest.TestCase):
    def test_handshake_uses_isolated_module_and_no_user_environment(self) -> None:
        envelope = {"protocol_version": 1, "request_id": "packaging-proof",
                    "result": {"capabilities": ["hello"], "schema_version_supported": 2,
                               "core_version": "test"}}
        completed = subprocess.CompletedProcess([], 0, json.dumps(envelope).encode() + b"\n", b"")
        with tempfile.TemporaryDirectory() as directory:
            python = Path(directory) / "python"
            with patch("scripts.probe_runtime.subprocess.run", return_value=completed) as call:
                self.assertEqual(handshake(python, Path(directory)), envelope["result"])
                arguments, kwargs = call.call_args
                self.assertEqual(arguments[0], [str(python), "-I", "-B", "-m", "insto.desktop"])
                self.assertEqual(kwargs["timeout"], 10)
                self.assertEqual(set(kwargs["env"]), {"PATH", "LANG"})
                self.assertNotIn("HIKERAPI_TOKEN", kwargs["env"])
```

- [ ] **Step 2: Red.** `python3 -m unittest discover -s tests -p 'test_probe_runtime.py' -v` → отсутствует module.
- [ ] **Step 3: Полное содержимое `probe_runtime.py`.**

```python
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from scripts.runtime_manifest import verify

ENV = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8"}
PROBE = """
import asyncio, importlib.metadata, json, platform, sqlite3, ssl, sys
import certifi, hikerapi, httpx, insto
assert sys.dont_write_bytecode and sys.flags.isolated
assert sqlite3.connect(':memory:').execute('select 1').fetchone() == (1,)
assert ssl.create_default_context(cafile=certifi.where()).cert_store_stats()['x509_ca'] > 0
async def sdk_probe():
    client = hikerapi.AsyncClient(token='offline-packaging-fixture', timeout=1)
    await client.aclose()
asyncio.run(sdk_probe())
print(json.dumps({'python': platform.python_version(), 'architecture': platform.machine(),
                  'core_version': insto.__version__, 'origin': insto.__file__,
                  'hikerapi_version': importlib.metadata.version('hikerapi')}))
"""


def handshake(python: Path, cwd: Path) -> dict[str, Any]:
    request = {"protocol_version": 1, "request_id": "packaging-proof", "operation": "hello", "params": {}}
    result = subprocess.run([str(python), "-I", "-B", "-m", "insto.desktop"],
                            input=json.dumps(request).encode() + b"\n", capture_output=True,
                            cwd=cwd, env=ENV, timeout=10, check=True)
    if result.stderr or result.stdout.count(b"\n") != 1 or len(result.stdout) > 2 * 1024**2:
        raise ValueError("invalid handshake transport")
    response = json.loads(result.stdout)
    if response.get("protocol_version") != 1 or response.get("request_id") != "packaging-proof":
        raise ValueError("incompatible handshake envelope")
    value = response.get("result", {})
    if "hello" not in value.get("capabilities", []) or value.get("schema_version_supported") != 2:
        raise ValueError("required desktop capability missing")
    return value


def probe(payload: Path, native_core: Path | None = None) -> Path:
    payload = payload.resolve(strict=True)
    manifest = json.loads((payload / "manifest.json").read_text())
    build_id = manifest.pop("build_id")
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    if hashlib.sha256(canonical).hexdigest() != build_id:
        raise ValueError("invalid proof manifest identifier")
    verify(payload / "python", manifest["files"])
    root = Path(tempfile.mkdtemp(prefix="relocated proof ", dir=payload.parent))
    relocated = root / "different path/python"
    shutil.copytree(payload / "python", relocated)
    python = relocated / "bin/python3"
    verify(relocated, manifest["files"])
    hello = handshake(python, root)
    completed = subprocess.run([str(python), "-I", "-B", "-c", PROBE], cwd=root,
                               env=ENV, capture_output=True, timeout=20, check=True)
    if completed.stderr:
        raise ValueError("dependency probe wrote stderr")
    details = json.loads(completed.stdout)
    if details["python"] != manifest["inputs"]["python_version"]:
        raise ValueError("unexpected Python version")
    if details["architecture"] != manifest["inputs"]["architecture"]:
        raise ValueError("unexpected runtime architecture")
    if details["core_version"] != hello["core_version"]:
        raise ValueError("wheel version differs from handshake")
    origin = Path(details["origin"]).resolve()
    if not origin.is_relative_to(relocated.resolve()) or "site-packages" not in origin.parts:
        raise ValueError("probe imported insto outside installed runtime")
    evidence: dict[str, Any] = {"build_id": build_id, "handshake": hello,
                                "dependencies": details, "native": "not_run"}
    if native_core is not None:
        core = native_core.resolve(strict=True)
        env = {**ENV, "INSTO_TEST_LAUNCHD": "1", "INSTO_TEST_NO_BYTECODE": "1",
               "INSTO_TEST_PYTHON": str(python)}
        result = subprocess.run([str(core / ".venv/bin/python"), "-m", "pytest",
                                 str(core / "tests/e2e/test_watch_service.py"),
                                 "--basetemp", str(root / "native-test"), "-q"],
                                cwd=core, env=env, capture_output=True, text=True, timeout=180)
        (root / "native-result.txt").write_text(result.stdout + result.stderr)
        if result.returncode or "1 passed" not in result.stdout or "skipped" in result.stdout:
            raise RuntimeError(f"native proof failed or skipped; preserve and inspect {root}")
        evidence["native"] = "passed"
    verify(relocated, manifest["files"])
    (root / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
    print(root)
    return root


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("payload", type=Path)
    parser.add_argument("--native-core", type=Path)
    args = parser.parse_args()
    probe(args.payload, args.native_core)


if __name__ == "__main__":
    main()
```

Relocated directory намеренно сохраняется даже при неудаче: если native cleanup не завершился, нельзя автоматически удалить interpreter работающей регистрации. `native-result.txt` содержит только isolated fake test output. Установленный app не использует этот test harness, argv `-c` или inherited build environment.

- [ ] **Step 4: Green unit tests и installed probe.**

```sh
python3 -m unittest discover -s tests -v
python3 -m scripts.probe_runtime .build/runtime-host-01
```

Ожидаются успешный hello, установленный origin внутри перемещённого дерева, TLS CA store, SDK imports и неизменный manifest после запуска. Никаких настоящих API calls. Это не live TLS endpoint smoke и не проверка Gatekeeper.

- [ ] **Step 5: Изолированный native smoke.** После проверки выше:

```sh
python3 -m scripts.probe_runtime .build/runtime-host-01 --native-core <worktrees>/desktop-foundation
```

Ожидается `1 passed`, не skip. Native тест использует только временный home с fake backend и свой label, проверяет installed origin, новый tick, restart, clean exit и удаление собственной службы с сохранением данных. Он не меняет реальные watches и не расширяет production desktop capabilities. При cleanup failure сохранить конкретные runtime/home/label и исправить только эту тестовую регистрацию после ownership-проверки; не удалять все LaunchAgents.

- [ ] **Step 6: Коммит.** `git add scripts/probe_runtime.py tests/test_probe_runtime.py`, `git commit -m "test: prove relocated runtime and isolated native service lifecycle"`.

## Task 6: Зафиксировать evidence и границу следующего app proof

**Create:** `packaging/README.md`.

- [ ] **Step 1: Добавить developer документ.**

```markdown
# Runtime proof (build machine only)

Run with Python >=3.12 on the target Mac architecture. Build requires uv,
network access, a clean C0 insto checkout, and its test environment.

1. `python3 -m unittest discover -s tests -v`
2. `python3 -m scripts.prepare_runtime --core-root <worktrees>/desktop-foundation --output .build/runtime-host-01`
3. `python3 -m scripts.probe_runtime .build/runtime-host-01`
4. `python3 -m scripts.probe_runtime .build/runtime-host-01 --native-core <worktrees>/desktop-foundation`

Use the actual clean C0 worktree if its path differs. Outputs are new private
children of .build; existing outputs are never overwritten. The probe retains
relocated trees and evidence, including on failure. Verify cleanup of the exact
temporary service before any manual removal of a failed native proof.

The manifest records CPython URL/hash, architecture, wheel hash/core commit,
requirements hash, and every normalized runtime file/mode/hash. Upstream
license files must remain in the runtime. All dependency installs occur here,
never on a user's Mac during app setup. No editable installation is bundled.

Sources: https://github.com/astral-sh/python-build-standalone/releases/tag/20260901
and https://gregoryszorc.com/docs/python-build-standalone/main/distributions.html.

P0 proves relocatability and isolated service compatibility only. The next P1
plan must build an actual Tauri app, resolve its bundled resources, publish the
runtime with trusted-manifest checks and locking, and verify service ticks after
the GUI exits and the DMG is unmounted. P0 scripts are not production IPC or
an installer. End-user completion additionally requires token onboarding,
updates/recovery, signed/notarized quarantined DMG and both claimed architectures.
```

- [ ] **Step 2: Свести evidence по реально выполненной архитектуре.** Сохранённый `evidence.json` обязан указывать фактические Python/core versions, build id, installed module origin и `native: passed`. Записать macOS version и архитектуру рядом с результатом команд `sw_vers` и `uname -m` в handoff. Вторую архитектуру и downloaded-app проверку явно отметить как не выполненные, если соответствующего runner/app нет.
- [ ] **Step 3: Финальная проверка.** `python3 -m unittest discover -s tests -v`, `git diff --check`, `git status --short`. `.build` не должна попасть в staged files. `git add packaging/README.md`, `git commit -m "docs: record runtime proof workflow and release gates"`.

## Gate P0 → P1

Успех P0 разрешает планировать минимальный **настоящий** Tauri app proof, но не означает, что установка GUI уже работает. P1 требует scoped Rust commands, bounded child I/O, canonical private root, cross-process publisher lock, trusted bundle manifest, no overwrite, interruption recovery и actual installed app test. Signing может требовать отдельной пользовательской авторизации; ad-hoc local proof не выдаётся за public release.

Если переносимость CPython/native dependencies не подтверждена, вернуть конкретный failing import/architecture/path и пересмотреть packaging в пределах согласованной архитектуры до создания полного UI. Не подменять portable runtime системным Python.
