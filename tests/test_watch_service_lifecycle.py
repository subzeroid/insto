from __future__ import annotations

import asyncio
import fcntl
import importlib
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from insto.config import Config
from insto.exceptions import BackendError
from insto.service import watch_service as legacy


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Config, Path]:
    monkeypatch.setattr(legacy.sys, "platform", "darwin")
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "user")
    home = tmp_path / "config"
    config = Config(
        db_path=home / "store.db",
        output_dir=home / "output",
        aiograpi_session_path=home / "session.json",
    )
    home.mkdir(mode=0o700)
    config.db_path.touch(mode=0o600)
    module = importlib.import_module("insto.service.watch_service_lifecycle")
    return module, config, home


def native(
    monkeypatch: pytest.MonkeyPatch, state: str = "missing"
) -> tuple[list[list[str]], dict[str, Any]]:
    calls: list[list[str]] = []
    job: dict[str, Any] = {"state": state, "fd": None}

    def run(args: list[str], *, timeout: float = 10) -> subprocess.CompletedProcess[bytes]:
        assert 0 < timeout <= 10
        calls.append(args)
        if args[0] == "print":
            if job["state"] == "missing":
                return subprocess.CompletedProcess(args, 1, b"", b"Could not find service")
            import plistlib

            plists = list(Path.home().glob("Library/LaunchAgents/*.plist"))
            provenance = ""
            if plists:
                try:
                    document = plistlib.loads(plists[0].read_bytes())
                    argv = document["ProgramArguments"]
                    provenance = (
                        f"program = {argv[0]}\narguments = {{\n" + "\n".join(argv) + "\n}\n"
                    )
                except Exception:
                    pass
            return subprocess.CompletedProcess(
                args, 0, f"state = {job['state']}\npid = 123\n{provenance}".encode(), b""
            )
        if args[0] in {"bootstrap", "kickstart"}:
            job["state"] = "running"
            if job.get("lock"):
                fd = os.open(job["lock"], os.O_RDWR | os.O_CREAT, 0o600)
                os.write(fd, b"123\n")
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                job["fd"] = fd
        if args[0] == "bootout":
            job["state"] = "missing"
            if job["fd"] is not None:
                os.close(job["fd"])
                job["fd"] = None
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(legacy, "_run_launchctl", run)
    return calls, job


def artifacts(home: Path, config: Config) -> None:
    paths = legacy.service_paths(home)
    manifest, plist = legacy._desired(paths, config, None)
    legacy._atomic_write(paths.manifest, manifest)
    legacy._atomic_write(paths.plist, plist)


@pytest.mark.asyncio
@pytest.mark.parametrize("unpublished", [b"", b"987\n"])
async def test_start_waits_for_executor_pid_publication(setup, monkeypatch, unpublished):
    module, config, home = setup
    _, job = native(monkeypatch)
    job["lock"] = Path(f"{config.db_path}.watch.lock")
    original = legacy._run_launchctl
    prints = 0

    def delayed(args, **kwargs):
        nonlocal prints
        if args[0] == "print" and job["fd"] is not None:
            prints += 1
            if prints == 2:
                os.ftruncate(job["fd"], 0)
                os.pwrite(job["fd"], b"123\n", 0)
        result = original(args, **kwargs)
        if args[0] == "bootstrap":
            os.ftruncate(job["fd"], 0)
            os.pwrite(job["fd"], unpublished, 0)
        return result

    monkeypatch.setattr(legacy, "_run_launchctl", delayed)
    try:
        with module.managed_service(
            home=home, config=config, deadline=time.monotonic() + 2
        ) as lease:
            result = await lease.ensure_running()
            assert result["executor"]["pid"] == result["process"]["pid"] == 123
            assert prints >= 2
    finally:
        if job["fd"] is not None:
            os.close(job["fd"])


@pytest.mark.asyncio
async def test_start_noop_stop_preserves_artifacts(
    setup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    module, config, home = setup
    calls, job = native(monkeypatch)
    job["lock"] = Path(f"{config.db_path}.watch.lock")
    with module.managed_service(home=home, config=config, deadline=time.monotonic() + 3) as service:
        assert (await service.inspect_owned())["installation"] == "not_installed"
        assert (await service.ensure_running())["executor"]["state"] == "busy"
        paths = legacy.service_paths(home)
        before = (paths.manifest.read_bytes(), paths.plist.read_bytes())
        count = len(calls)
        await service.ensure_running()
        assert all(call[0] == "print" for call in calls[count:])
        assert (await service.ensure_stopped())["executor"]["state"] == "idle"
        assert before == (paths.manifest.read_bytes(), paths.plist.read_bytes())
        assert [c[0] for c in calls if c[0] != "print"] == [
            "enable",
            "bootstrap",
            "disable",
            "bootout",
        ]
    with pytest.raises(BackendError):
        await service.inspect_owned()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["exited", "waiting", "not running"])
async def test_clean_exit_kickstarts(
    setup: Any, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    module, config, home = setup
    calls, job = native(monkeypatch, state)
    job["lock"] = Path(f"{config.db_path}.watch.lock")
    try:
        with module.managed_service(
            home=home, config=config, deadline=time.monotonic() + 3
        ) as service:
            artifacts(home, config)
            await service.ensure_running()
            assert [c[0] for c in calls if c[0] != "print"] == ["enable", "kickstart"]
            assert all("-k" not in c for c in calls)
    finally:
        if job["fd"] is not None:
            os.close(job["fd"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["manifest", "plist", "unknown", "partial_loaded", "foreign_executor", "deadline"]
)
async def test_refuses_before_mutations(
    setup: Any, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    module, config, home = setup
    calls, _ = native(monkeypatch, "mystery" if fault == "unknown" else "running")
    with module.managed_service(home=home, config=config, deadline=time.monotonic() + 2) as service:
        artifacts(home, config)
        paths = legacy.service_paths(home)
        fd = None
        if fault in {"manifest", "plist"}:
            getattr(paths, fault).write_bytes(b"foreign")
        if fault == "partial_loaded":
            paths.plist.unlink()
        if fault == "foreign_executor":
            fd = os.open(f"{config.db_path}.watch.lock", os.O_CREAT | os.O_RDWR, 0o600)
            os.write(fd, b"456\n")
            fcntl.flock(fd, fcntl.LOCK_EX)
        if fault == "deadline":
            service.deadline = time.monotonic() - 1
        try:
            with pytest.raises(BackendError):
                await service.ensure_running()
            assert all(c[0] == "print" for c in calls)
        finally:
            if fd is not None:
                os.close(fd)


def test_idle_preserves_pid_inode_and_rejects_busy(setup: Any) -> None:
    module, config, home = setup
    with module.managed_service(home=home, config=config, deadline=time.monotonic() + 3) as service:
        lock = Path(f"{config.db_path}.watch.lock")
        lock.write_bytes(b"987\n")
        lock.chmod(0o600)
        inode = lock.stat().st_ino
        with service.idle_executor():
            assert lock.read_bytes() == b"987\n"
            fd = os.open(lock, os.O_RDWR)
            try:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd)
        assert lock.stat().st_ino == inode


@pytest.mark.asyncio
async def test_partial_repair_and_unknown_registration(
    setup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    module, config, home = setup
    _, job = native(monkeypatch)
    job["lock"] = Path(f"{config.db_path}.watch.lock")
    try:
        with module.managed_service(
            home=home, config=config, deadline=time.monotonic() + 3
        ) as service:
            paths = legacy.service_paths(home)
            manifest, _ = legacy._desired(paths, config, None)
            legacy._atomic_write(paths.manifest, manifest)
            assert (await service.inspect_owned())["installation"] == "incomplete"
            await service.ensure_running()
            assert paths.plist.exists()
            monkeypatch.setattr(
                legacy,
                "_run_launchctl",
                lambda *a, **k: subprocess.CompletedProcess([], 1, b"", b"unknown"),
            )
            with pytest.raises(BackendError):
                await service.ensure_stopped()
    finally:
        if job["fd"] is not None:
            os.close(job["fd"])


@pytest.mark.asyncio
async def test_running_without_executor_never_ready(
    setup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    module, config, home = setup
    calls, _ = native(monkeypatch, "running")
    monkeypatch.setattr(module, "_READINESS_SECONDS", 0.01)
    with module.managed_service(home=home, config=config, deadline=time.monotonic() + 1) as service:
        artifacts(home, config)
        with pytest.raises(BackendError, match=r"ready|deadline"):
            await service.ensure_running()
        assert all(c[0] == "print" for c in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["argv", "interpreter", "bytecode"])
async def test_runtime_replacement_refused(
    setup: Any, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    module, config, home = setup
    calls, _ = native(monkeypatch, "waiting")
    with module.managed_service(home=home, config=config, deadline=time.monotonic() + 1) as service:
        if fault == "interpreter":
            monkeypatch.setattr(legacy.sys, "executable", "/different/python")
        if fault == "bytecode":
            monkeypatch.setattr(
                legacy.sys, "dont_write_bytecode", not legacy.sys.dont_write_bytecode
            )
        artifacts(home, config)
        if fault == "argv":
            monkeypatch.setattr(
                legacy,
                "_run_launchctl",
                lambda *a, **k: subprocess.CompletedProcess(
                    [], 0, b"state = waiting\nprogram = /foreign\narguments = {\n/foreign\n}\n", b""
                ),
            )
        with pytest.raises(BackendError):
            await service.ensure_stopped()
        assert all(c[0] == "print" for c in calls)


@pytest.mark.parametrize("fault", ["symlink", "hardlink", "permissions", "busy", "swap"])
def test_unsafe_executor_refused(setup: Any, fault: str) -> None:
    module, config, home = setup
    with module.managed_service(home=home, config=config, deadline=time.monotonic() + 1) as service:
        lock = Path(f"{config.db_path}.watch.lock")
        original = home / "original"
        original.write_bytes(b"123\n")
        original.chmod(0o600)
        if fault == "symlink":
            lock.symlink_to(original)
        elif fault == "hardlink":
            os.link(original, lock)
        else:
            lock.write_bytes(b"123\n")
            lock.chmod(0o644 if fault == "permissions" else 0o600)
        fd = os.open(lock, os.O_RDWR)
        try:
            if fault == "busy":
                fcntl.flock(fd, fcntl.LOCK_EX)
            with pytest.raises(BackendError), service.idle_executor():
                if fault == "swap":
                    lock.unlink()
                    lock.write_bytes(b"123\n")
                    lock.chmod(0o600)
        finally:
            os.close(fd)


@pytest.mark.asyncio
async def test_repeated_cancel_drains_worker_before_lease_release(
    setup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    module, config, home = setup
    entered, release = threading.Event(), threading.Event()

    def run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        entered.set()
        release.wait(3)
        return subprocess.CompletedProcess([], 1, b"", b"Could not find service")

    monkeypatch.setattr(legacy, "_run_launchctl", run)

    async def operation() -> None:
        with module.managed_service(
            home=home, config=config, deadline=time.monotonic() + 3
        ) as service:
            await service.inspect_owned()

    task = asyncio.create_task(operation())
    try:
        while not entered.is_set():
            await asyncio.sleep(0.001)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        with pytest.raises(BackendError), legacy._management_lock(legacy.service_paths(home)):
            pass
        with pytest.raises(BackendError, match="already in progress"):
            await legacy.install_service(home=home)
        with pytest.raises(BackendError, match="already in progress"):
            await legacy.uninstall_service(home=home)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    with legacy._management_lock(legacy.service_paths(home)):
        pass


@pytest.mark.asyncio
async def test_worker_rechecks_deadline_before_native_call(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        nonlocal called
        called = True
        return subprocess.CompletedProcess([], 0, b"", b"")

    monkeypatch.setattr(legacy, "_run_launchctl", run)
    with pytest.raises(BackendError):
        await legacy._launchctl(["print", "test"], timeout=1, deadline=time.monotonic() - 1)
    assert not called


@pytest.mark.asyncio
async def test_native_timeout_is_capped_by_remaining_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = []

    def run(args: list[str], *, timeout: float) -> subprocess.CompletedProcess[bytes]:
        observed.append(timeout)
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(legacy, "_run_launchctl", run)
    await legacy._launchctl(["print", "test"], timeout=10, deadline=time.monotonic() + 0.2)
    assert len(observed) == 1
    assert 0 < observed[0] <= 0.2


@pytest.mark.asyncio
async def test_readiness_native_call_uses_readiness_budget(
    setup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    module, config, home = setup
    native(monkeypatch, "running")
    original = legacy._run_launchctl
    observed = []

    def run(args: list[str], *, timeout: float) -> subprocess.CompletedProcess[bytes]:
        observed.append(timeout)
        return original(args, timeout=timeout)

    monkeypatch.setattr(legacy, "_run_launchctl", run)
    monkeypatch.setattr(module, "_READINESS_SECONDS", 0.02)
    with module.managed_service(home=home, config=config, deadline=time.monotonic() + 2) as service:
        artifacts(home, config)
        with pytest.raises(BackendError):
            await service.ensure_running()
    assert len(observed) >= 2
    assert observed[1] <= 0.02


@pytest.mark.parametrize("fault", ["missing", "symlink", "permissions", "hardlink"])
def test_missing_lock_requires_private_initialized_database(setup: Any, fault: str) -> None:
    module, config, home = setup
    if fault == "missing":
        config.db_path.unlink()
    elif fault == "permissions":
        config.db_path.chmod(0o644)
    else:
        other = home / "other.db"
        config.db_path.rename(other)
        if fault == "symlink":
            config.db_path.symlink_to(other)
        else:
            os.link(other, config.db_path)
    with module.managed_service(home=home, config=config, deadline=time.monotonic() + 1) as service:
        with pytest.raises(BackendError), service.idle_executor():
            pass
        assert not Path(f"{config.db_path}.watch.lock").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra", ["nested", "duplicate_pid", "duplicate_exit", "bad_pid", "bad_exit"]
)
async def test_native_outer_fields_are_unambiguous(
    setup: Any, monkeypatch: pytest.MonkeyPatch, extra: str
) -> None:
    module, config, home = setup
    calls, _ = native(monkeypatch, "running")
    original = legacy._run_launchctl
    additions = {
        "nested": "resource coalition = {\nstate = active\npid = 456\nprogram = /nested\n}\n",
        "duplicate_pid": "pid = 456\n",
        "duplicate_exit": "last exit code = 0\nlast exit code = 1\n",
        "bad_pid": "pid = garbage\n",
        "bad_exit": "last exit code = garbage\n",
    }

    def run(args: list[str], *, timeout: float) -> subprocess.CompletedProcess[bytes]:
        result = original(args, timeout=timeout)
        result.stdout = b"gui/501/test = {\n" + result.stdout + additions[extra].encode() + b"}\n"
        return result

    monkeypatch.setattr(legacy, "_run_launchctl", run)
    with module.managed_service(home=home, config=config, deadline=time.monotonic() + 1) as service:
        artifacts(home, config)
        fd = os.open(f"{config.db_path}.watch.lock", os.O_CREAT | os.O_RDWR, 0o600)
        os.write(fd, b"123\n")
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            if extra == "nested":
                assert (await service.ensure_running())["process"]["pid"] == 123
            else:
                with pytest.raises(BackendError):
                    await service.ensure_running()
            assert all(call[0] == "print" for call in calls)
        finally:
            os.close(fd)
