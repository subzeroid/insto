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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    [
        "immediate",
        "delayed",
        "stopped",
        "no_executor",
        "foreign_pid",
        "foreign_program",
        "expired",
        "oserror",
        "nonzero",
    ],
)
async def test_kickstart_timeout_observes_without_retry(
    setup: Any, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    module, config, home = setup
    calls, job = native(monkeypatch, "exited")
    original = legacy._run_launchctl
    monkeypatch.setattr(module, "_READINESS_SECONDS", 0.08)
    after_kickstart = False
    observations = 0
    lease: Any = None

    def run(args: list[str], *, timeout: float) -> subprocess.CompletedProcess[bytes]:
        nonlocal after_kickstart, observations
        if args[0] == "kickstart":
            after_kickstart = True
            result = original(args, timeout=timeout)
            if outcome == "nonzero":
                result.returncode = 1
                return result
            if outcome == "oserror":
                raise BackendError("native failed") from OSError("test")
            if outcome == "stopped":
                job["state"] = "exited"
            if outcome == "expired":
                lease.deadline = time.monotonic() - 1
            raise BackendError("native timed out") from subprocess.TimeoutExpired(args, timeout)
        if args[0] == "print" and after_kickstart:
            observations += 1
            if (
                outcome in {"immediate", "delayed", "foreign_pid", "foreign_program"}
                and job["fd"] is None
                and (outcome != "delayed" or observations >= 2)
            ):
                fd = os.open(f"{config.db_path}.watch.lock", os.O_RDWR)
                os.write(fd, b"456\n" if outcome == "foreign_pid" else b"123\n")
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                job["fd"] = fd
        result = original(args, timeout=timeout)
        if outcome == "foreign_program" and after_kickstart:
            result.stdout = result.stdout.replace(b"program = ", b"program = /foreign")
        return result

    monkeypatch.setattr(legacy, "_run_launchctl", run)
    try:
        with module.managed_service(
            home=home, config=config, deadline=time.monotonic() + 2
        ) as lease:
            artifacts(home, config)
            if outcome in {"immediate", "delayed"}:
                assert (await lease.ensure_running())["executor"]["pid"] == 123
            else:
                with pytest.raises(BackendError):
                    await lease.ensure_running()
        assert [call[0] for call in calls if call[0] != "print"] == ["enable", "kickstart"]
        if outcome in {"nonzero", "oserror", "expired"}:
            assert observations == 0
        if outcome == "delayed":
            assert observations >= 2
    finally:
        if job["fd"] is not None:
            os.close(job["fd"])


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["kickstart", "observation"])
async def test_kickstart_timeout_cancellation_drains_under_lease(
    setup: Any, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    module, config, home = setup
    calls, _ = native(monkeypatch, "exited")
    original = legacy._run_launchctl
    entered, release = threading.Event(), threading.Event()
    after_kickstart = False

    def run(args: list[str], *, timeout: float) -> subprocess.CompletedProcess[bytes]:
        nonlocal after_kickstart
        if args[0] == "kickstart":
            after_kickstart = True
            original(args, timeout=timeout)
            if stage == "kickstart":
                entered.set()
                release.wait(3)
            raise BackendError("native timed out") from subprocess.TimeoutExpired(args, timeout)
        if args[0] == "print" and after_kickstart and stage == "observation":
            entered.set()
            release.wait(3)
        return original(args, timeout=timeout)

    monkeypatch.setattr(legacy, "_run_launchctl", run)

    async def operation() -> None:
        with module.managed_service(
            home=home, config=config, deadline=time.monotonic() + 3
        ) as lease:
            artifacts(home, config)
            await lease.ensure_running()

    task = asyncio.create_task(operation())
    try:
        for _ in range(1000):
            if entered.is_set() or task.done():
                break
            await asyncio.sleep(0.001)
        assert entered.is_set()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        with pytest.raises(BackendError, match="already in progress"):
            await legacy.install_service(home=home)
        with pytest.raises(BackendError, match="already in progress"):
            await legacy.uninstall_service(home=home)
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    with legacy._management_lock(legacy.service_paths(home)):
        pass
    assert [call[0] for call in calls if call[0] != "print"] == ["enable", "kickstart"]


@pytest.mark.asyncio
async def test_bootstrap_timeout_still_propagates(
    setup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    module, config, home = setup
    calls, _ = native(monkeypatch)
    original = legacy._run_launchctl

    def run(args: list[str], *, timeout: float) -> subprocess.CompletedProcess[bytes]:
        result = original(args, timeout=timeout)
        if args[0] == "bootstrap":
            raise BackendError("native timed out") from subprocess.TimeoutExpired(args, timeout)
        return result

    monkeypatch.setattr(legacy, "_run_launchctl", run)
    with module.managed_service(home=home, config=config, deadline=time.monotonic() + 1) as lease:
        with pytest.raises(BackendError) as error:
            await lease.ensure_running()
        assert isinstance(error.value.__cause__, subprocess.TimeoutExpired)
    assert calls[-1][0] == "bootstrap"


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
@pytest.mark.parametrize("already_stopping", [False, True])
async def test_stop_waits_through_native_sigtermed_state(setup, monkeypatch, already_stopping):
    module, config, home = setup
    _, job = native(monkeypatch, "SIGTERMed" if already_stopping else "running")
    job["lock"] = Path(f"{config.db_path}.watch.lock")
    original = legacy._run_launchctl
    stopping_prints = 0
    booted_out = False

    def transient(args, **kwargs):
        nonlocal stopping_prints, booted_out
        if args[0] == "print" and booted_out:
            stopping_prints += 1
            if stopping_prints == 2:
                job["state"] = "missing"
        result = original(args, **kwargs)
        if args[0] == "bootout":
            booted_out = True
            job["state"] = "SIGTERMed"
        return result

    monkeypatch.setattr(legacy, "_run_launchctl", transient)
    try:
        with module.managed_service(
            home=home, config=config, deadline=time.monotonic() + 2
        ) as lease:
            artifacts(home, config)
            job["fd"] = os.open(job["lock"], os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(job["fd"], fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.write(job["fd"], b"123\n")
            report = await lease.ensure_stopped()
            assert report["registration"] == "unloaded" and report["executor"]["state"] == "idle"
            assert stopping_prints == 2
    finally:
        if job["fd"] is not None:
            os.close(job["fd"])


@pytest.mark.asyncio
@pytest.mark.parametrize("already_spawning", [False, True])
async def test_native_xpcproxy_and_never_exited_are_pending_not_ready(
    setup, monkeypatch, already_spawning
):
    module, config, home = setup
    calls, job = native(monkeypatch, "xpcproxy" if already_spawning else "missing")
    job["lock"] = Path(f"{config.db_path}.watch.lock")
    original = legacy._run_launchctl
    prints = 0

    def transient(args, **kwargs):
        nonlocal prints
        result = original(args, **kwargs)
        if args[0] == "print" and result.returncode == 0:
            prints += 1
            state = "xpcproxy" if prints == 1 else "running"
            result.stdout = (
                result.stdout.replace(
                    f"state = {job['state']}".encode(), f"state = {state}".encode()
                )
                + b"last exit code = (never exited)\n"
            )
        return result

    monkeypatch.setattr(legacy, "_run_launchctl", transient)
    try:
        with module.managed_service(
            home=home, config=config, deadline=time.monotonic() + 2
        ) as lease:
            if already_spawning:
                artifacts(home, config)
                job["fd"] = os.open(job["lock"], os.O_RDWR | os.O_CREAT, 0o600)
                fcntl.flock(job["fd"], fcntl.LOCK_EX | fcntl.LOCK_NB)
                os.write(job["fd"], b"123\n")
            report = await lease.ensure_running()
            assert report["process"]["state"] == "running"
            assert report["process"]["last_exit_code"] is None
            assert report["executor"]["pid"] == report["process"]["pid"] == 123
            assert prints >= 2
            if already_spawning:
                assert all(call[0] == "print" for call in calls)
    finally:
        if job["fd"] is not None:
            os.close(job["fd"])


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
