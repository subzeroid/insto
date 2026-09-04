from __future__ import annotations

import asyncio
import json
import os
import plistlib
import sqlite3
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from insto.exceptions import BackendError
from insto.service import watch_service
from insto.service.history import HistoryStore


def _result(code: int, *, out: bytes = b"", err: bytes = b"") -> SimpleNamespace:
    return SimpleNamespace(returncode=code, stdout=out, stderr=err)


def test_service_paths_are_stable_and_user_scoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(watch_service.Path, "home", lambda: tmp_path / "user")
    paths = watch_service.service_paths(tmp_path / "config" / ".." / "config")
    assert paths.home == (tmp_path / "config").resolve()
    assert paths.label.startswith(f"io.insto.watch.{os.getuid()}.")
    assert paths.directory == paths.home / "services" / "watch"
    assert paths.manifest == paths.directory / "manifest.json"
    assert paths.plist == tmp_path / "user/Library/LaunchAgents" / f"{paths.label}.plist"


def test_read_private_file_rejects_permissions_symlinks_and_fifo(tmp_path: Path) -> None:
    private = tmp_path / "private"
    private.write_bytes(b"ok")
    private.chmod(0o600)
    assert watch_service.read_private_file(private) == b"ok"

    private.chmod(0o640)
    with pytest.raises(BackendError):
        watch_service.read_private_file(private)
    private.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(private)
    with pytest.raises(BackendError):
        watch_service.read_private_file(link)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo, 0o600)
    with pytest.raises(BackendError):
        watch_service.read_private_file(fifo)


@pytest.mark.asyncio
async def test_install_writes_exact_artifacts_and_repeated_install_is_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "config"
    user_home = tmp_path / "user"
    monkeypatch.setattr(watch_service.sys, "platform", "darwin")
    monkeypatch.setattr(watch_service.Path, "home", lambda: user_home)
    config = SimpleNamespace(
        backend="hikerapi",
        db_path=home / "store.db",
        output_dir=home / "output",
        aiograpi_session_path=home / "session.json",
    )
    monkeypatch.setattr(watch_service, "_resolve_config", lambda h, e: config)
    calls: list[list[str]] = []
    bootstrapped = False

    def fake_run(args: list[str], **_: object) -> SimpleNamespace:
        nonlocal bootstrapped
        calls.append(args)
        if args[1:] == ["print", f"gui/{os.getuid()}"]:
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        if args[1] == "print":
            if bootstrapped:
                return SimpleNamespace(returncode=0, stdout=b"state = running", stderr=b"")
            return SimpleNamespace(returncode=1, stdout=b"", stderr=b"Could not find service")
        if args[1] == "bootstrap":
            bootstrapped = True
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(watch_service.subprocess, "run", fake_run)
    first = await watch_service.install_service(home=home)
    paths = watch_service.service_paths(home)
    manifest = json.loads(paths.manifest.read_text())
    plist = plistlib.loads(paths.plist.read_bytes())
    assert set(manifest) == {
        "schema_version",
        "managed_by",
        "uid",
        "label",
        "config_home",
        "python",
        "backend",
        "db_path",
        "output_dir",
        "aiograpi_session_path",
        "env_file",
    }
    assert plist["ProgramArguments"] == [
        os.path.abspath(watch_service.sys.executable),
        "-I",
        "-m",
        "insto.service.watch_service_runner",
        str(paths.manifest),
    ]
    assert plist["Umask"] == 0o77
    assert first["changed"] is True
    before = paths.manifest.stat().st_mtime_ns
    calls.clear()

    def loaded(args: list[str], **_: object) -> SimpleNamespace:
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout=b"state = running\npid = 123\n", stderr=b"")

    monkeypatch.setattr(watch_service.subprocess, "run", loaded)
    second = await watch_service.install_service(home=home)
    assert second["changed"] is False
    assert paths.manifest.stat().st_mtime_ns == before
    assert all(call[1] == "print" for call in calls)


@pytest.mark.asyncio
async def test_install_refuses_foreign_and_changed_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(watch_service.sys, "platform", "darwin")
    monkeypatch.setattr(watch_service.Path, "home", lambda: tmp_path / "user")
    home = tmp_path / "config"
    paths = watch_service.service_paths(home)
    paths.plist.parent.mkdir(parents=True)
    paths.plist.write_bytes(b"foreign")
    paths.plist.chmod(0o600)
    config = SimpleNamespace(
        backend="hikerapi",
        db_path=home / "db",
        output_dir=home / "out",
        aiograpi_session_path=home / "session",
    )
    monkeypatch.setattr(watch_service, "_resolve_config", lambda h, e: config)
    with pytest.raises(BackendError):
        await watch_service.install_service(home=home)


def test_launchctl_parser_is_best_effort_and_redacted() -> None:
    assert watch_service._parse_launchctl_print(
        b"state = running\npid = 42\nlast exit code = 7\n"
    ) == {"state": "running", "pid": 42, "last_exit_code": 7}
    assert watch_service._parse_launchctl_print(b"unexpected") == {
        "state": None,
        "pid": None,
        "last_exit_code": None,
    }


@pytest.mark.asyncio
async def test_uninstall_preserves_data_and_retains_files_on_bootout_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(watch_service.sys, "platform", "darwin")
    monkeypatch.setattr(watch_service.Path, "home", lambda: tmp_path / "user")
    home = tmp_path / "config"
    config = SimpleNamespace(
        backend="hikerapi",
        db_path=home / "store.db",
        output_dir=home / "out",
        aiograpi_session_path=home / "session",
    )
    monkeypatch.setattr(watch_service, "_resolve_config", lambda h, e: config)
    installed = False

    def initial_run(args: list[str], **_: object) -> SimpleNamespace:
        nonlocal installed
        if args[1] == "bootstrap":
            installed = True
        present = args[1:] == ["print", f"gui/{os.getuid()}"] or installed
        return SimpleNamespace(
            returncode=0 if present or args[1] != "print" else 1,
            stdout=b"",
            stderr=b"" if present else b"Could not find service",
        )

    monkeypatch.setattr(watch_service.subprocess, "run", initial_run)
    await watch_service.install_service(home=home)
    paths = watch_service.service_paths(home)
    config.db_path.parent.mkdir(parents=True, exist_ok=True)
    config.db_path.write_text("data")

    def fail_bootout(args: list[str], **_: object) -> SimpleNamespace:
        if args[1] == "print":
            return SimpleNamespace(returncode=0, stdout=b"state = running", stderr=b"")
        return SimpleNamespace(returncode=1, stdout=b"secret", stderr=b"failure secret")

    monkeypatch.setattr(watch_service.subprocess, "run", fail_bootout)
    with pytest.raises(BackendError, match="launchctl") as exc:
        await watch_service.uninstall_service(home=home)
    assert "secret" not in str(exc.value)
    assert paths.manifest.exists() and paths.plist.exists() and config.db_path.exists()


@pytest.mark.asyncio
async def test_non_macos_refuses_before_creating_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(watch_service.sys, "platform", "linux")
    with pytest.raises(BackendError):
        await watch_service.install_service(home=tmp_path)
    assert not (tmp_path / "services").exists()


@pytest.mark.parametrize(
    "payload",
    [b"not-json", b"{}", b'{"schema_version":true}'],
)
def test_read_manifest_rejects_malformed_or_non_strict_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: bytes
) -> None:
    monkeypatch.setattr(watch_service.Path, "home", lambda: tmp_path / "user")
    paths = watch_service.service_paths(tmp_path / "config")
    paths.directory.mkdir(parents=True)
    paths.manifest.write_bytes(payload)
    paths.manifest.chmod(0o600)
    with pytest.raises(BackendError):
        watch_service.read_manifest(paths)


def test_read_private_file_rejects_oversize_without_disclosing_contents(tmp_path: Path) -> None:
    path = tmp_path / "secret"
    path.write_bytes(b"super-secret-value")
    path.chmod(0o600)
    with pytest.raises(BackendError) as exc:
        watch_service.read_private_file(path, max_bytes=4)
    assert "super-secret-value" not in str(exc.value)


@pytest.mark.asyncio
async def test_missing_gui_domain_publishes_no_autostart_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(watch_service.sys, "platform", "darwin")
    monkeypatch.setattr(watch_service.Path, "home", lambda: tmp_path / "user")
    home = tmp_path / "config"
    config = SimpleNamespace(
        backend="hikerapi",
        db_path=home / "store.db",
        output_dir=home / "out",
        aiograpi_session_path=home / "session",
    )
    monkeypatch.setattr(watch_service, "_resolve_config", lambda h, e: config)
    monkeypatch.setattr(
        watch_service.subprocess, "run", lambda *a, **k: _result(1, err=b"no domain")
    )
    with pytest.raises(BackendError, match="GUI domain"):
        await watch_service.install_service(home=home)
    paths = watch_service.service_paths(home)
    assert not paths.manifest.exists()
    assert not paths.plist.exists()


@pytest.mark.asyncio
async def test_bootstrap_success_without_post_registration_retains_retryable_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(watch_service.sys, "platform", "darwin")
    monkeypatch.setattr(watch_service.Path, "home", lambda: tmp_path / "user")
    home = tmp_path / "config"
    config = SimpleNamespace(
        backend="hikerapi",
        db_path=home / "db",
        output_dir=home / "out",
        aiograpi_session_path=home / "session",
    )
    monkeypatch.setattr(watch_service, "_resolve_config", lambda h, e: config)

    def launchctl(args: list[str], **_: object) -> SimpleNamespace:
        if args[1:] == ["print", f"gui/{os.getuid()}"] or args[1] != "print":
            return _result(0)
        return _result(1, err=b"Could not find service")

    monkeypatch.setattr(watch_service.subprocess, "run", launchctl)
    with pytest.raises(BackendError, match="confirm"):
        await watch_service.install_service(home=home)
    paths = watch_service.service_paths(home)
    assert paths.manifest.exists() and paths.plist.exists()


@pytest.mark.asyncio
async def test_loaded_service_without_owned_artifacts_is_refused_before_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(watch_service.sys, "platform", "darwin")
    monkeypatch.setattr(watch_service.Path, "home", lambda: tmp_path / "user")
    home = tmp_path / "config"
    config = SimpleNamespace(
        backend="hikerapi",
        db_path=home / "db",
        output_dir=home / "out",
        aiograpi_session_path=home / "session",
    )
    monkeypatch.setattr(watch_service, "_resolve_config", lambda h, e: config)
    monkeypatch.setattr(watch_service.subprocess, "run", lambda *a, **k: _result(0))
    with pytest.raises(BackendError, match="unknown loaded"):
        await watch_service.install_service(home=home)
    paths = watch_service.service_paths(home)
    assert not paths.manifest.exists() and not paths.plist.exists()


@pytest.mark.asyncio
async def test_launchctl_timeout_is_generic_and_leaves_no_plist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(watch_service.sys, "platform", "darwin")
    monkeypatch.setattr(watch_service.Path, "home", lambda: tmp_path / "user")
    home = tmp_path / "config"
    config = SimpleNamespace(
        backend="hikerapi",
        db_path=home / "db",
        output_dir=home / "out",
        aiograpi_session_path=home / "session",
    )
    monkeypatch.setattr(watch_service, "_resolve_config", lambda h, e: config)

    def timeout(*_: object, **__: object) -> None:
        raise subprocess.TimeoutExpired(["/bin/launchctl"], 10, stderr=b"secret")

    monkeypatch.setattr(watch_service.subprocess, "run", timeout)
    with pytest.raises(BackendError) as exc:
        await watch_service.install_service(home=home)
    assert "secret" not in str(exc.value)
    assert not watch_service.service_paths(home).plist.exists()


@pytest.mark.asyncio
async def test_status_without_installation_is_read_only_and_loads_no_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(watch_service.sys, "platform", "darwin")
    monkeypatch.setattr(watch_service.Path, "home", lambda: tmp_path / "user")
    home = tmp_path / "absent"
    monkeypatch.setattr(
        watch_service.subprocess,
        "run",
        lambda *a, **k: _result(1, err=b"Could not find service"),
    )
    monkeypatch.setattr(
        watch_service,
        "_resolve_config",
        lambda *a: pytest.fail("status must not resolve credentials/config"),
    )
    status = await watch_service.service_status(home=home)
    assert status["installation"] == "not_installed"
    assert status["registration"] == "unloaded"
    assert status["database_state"] == "missing"
    assert status["watches"] == []
    assert not home.exists()


def _owned_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, plist: bool
) -> tuple[Path, watch_service.ServicePaths, SimpleNamespace]:
    monkeypatch.setattr(watch_service.Path, "home", lambda: tmp_path / "user")
    home = tmp_path / "config"
    paths = watch_service.service_paths(home)
    paths.directory.mkdir(parents=True, mode=0o700)
    home.chmod(0o700)
    (home / "services").chmod(0o700)
    paths.directory.chmod(0o700)
    paths.plist.parent.mkdir(parents=True)
    config = SimpleNamespace(
        backend="hikerapi",
        db_path=home / "store.db",
        output_dir=home / "out",
        aiograpi_session_path=home / "session",
    )
    manifest, plist_bytes = watch_service._desired(paths, config, None)
    paths.manifest.write_bytes(manifest)
    paths.manifest.chmod(0o600)
    if plist:
        paths.plist.write_bytes(plist_bytes)
        paths.plist.chmod(0o600)
    return home, paths, config


@pytest.mark.asyncio
@pytest.mark.parametrize("with_plist", [False, True])
async def test_uninstall_owned_artifacts_preserves_user_data_and_lock_inode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, with_plist: bool
) -> None:
    monkeypatch.setattr(watch_service.sys, "platform", "darwin")
    home, paths, config = _owned_artifacts(tmp_path, monkeypatch, plist=with_plist)
    paths.log_dir.mkdir(mode=0o700)
    log = paths.log_dir / "watch.log"
    env_file = home / "credentials.env"
    for path in (config.db_path, log, env_file):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("keep")
    lock = paths.directory / "management.lock"
    lock.write_text("")
    lock.chmod(0o600)
    inode = lock.stat().st_ino
    monkeypatch.setattr(
        watch_service.subprocess,
        "run",
        lambda *a, **k: _result(1, err=b"Could not find service"),
    )
    result = await watch_service.uninstall_service(home=home)
    assert result["installation"] == "not_installed"
    assert not paths.manifest.exists() and not paths.plist.exists()
    assert [path.read_text() for path in (config.db_path, log, env_file)] == ["keep"] * 3
    assert lock.stat().st_ino == inode


def test_management_lock_rejects_fifo_and_competing_holder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(watch_service.Path, "home", lambda: tmp_path / "user")
    paths = watch_service.service_paths(tmp_path / "config")
    paths.directory.mkdir(parents=True)
    lock = paths.directory / "management.lock"
    os.mkfifo(lock, 0o600)
    with pytest.raises(BackendError, match="unsafe"), watch_service._management_lock(paths):
        pass
    lock.unlink()
    with (
        watch_service._management_lock(paths),
        pytest.raises(BackendError, match="already in progress"),
        watch_service._management_lock(paths),
    ):
        pass


def test_management_lock_closes_descriptor_after_wrong_owner_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(watch_service.Path, "home", lambda: tmp_path / "user")
    paths = watch_service.service_paths(tmp_path / "config")
    paths.directory.mkdir(parents=True)
    real_fstat = os.fstat
    opened_fd: int | None = None

    def wrong_owner(fd: int) -> os.stat_result:
        nonlocal opened_fd
        opened_fd = fd
        values = list(real_fstat(fd))
        values[4] = os.getuid() + 1
        return os.stat_result(values)

    monkeypatch.setattr(watch_service.os, "fstat", wrong_owner)
    with pytest.raises(BackendError, match="unsafe"), watch_service._management_lock(paths):
        pass
    assert opened_fd is not None
    with pytest.raises(OSError):
        real_fstat(opened_fd)


@pytest.mark.asyncio
async def test_uninstall_rejects_symlinked_service_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(watch_service.sys, "platform", "darwin")
    monkeypatch.setattr(watch_service.Path, "home", lambda: tmp_path / "user")
    home = tmp_path / "config"
    home.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    (home / "services").symlink_to(outside, target_is_directory=True)
    directory = outside / "watch"
    directory.mkdir(mode=0o700)
    (directory / "manifest.json").write_bytes(b"{}")
    (directory / "manifest.json").chmod(0o600)
    with pytest.raises(BackendError, match="unsafe service directory"):
        await watch_service.uninstall_service(home=home)


@pytest.mark.asyncio
@pytest.mark.parametrize("database_kind", ["corrupt", "v1"])
async def test_status_reports_unavailable_database_without_exposing_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, database_kind: str
) -> None:
    monkeypatch.setattr(watch_service.sys, "platform", "darwin")
    home, _paths, config = _owned_artifacts(tmp_path, monkeypatch, plist=True)
    if database_kind == "corrupt":
        config.db_path.write_bytes(b"private-token not sqlite")
    else:
        with sqlite3.connect(config.db_path) as connection:
            connection.execute("CREATE TABLE _meta(key TEXT PRIMARY KEY, value TEXT)")
            connection.execute("INSERT INTO _meta VALUES('schema_version', '1')")
    monkeypatch.setattr(
        watch_service.subprocess,
        "run",
        lambda *a, **k: _result(1, err=b"Could not find service"),
    )
    status = await watch_service.service_status(home=home)
    assert status["installation"] == "installed"
    assert status["database_state"] == "unavailable"
    assert status["database_error"] == "watch database could not be read safely"
    assert "private-token" not in json.dumps(status)


@pytest.mark.asyncio
async def test_install_rejects_relative_env_file_before_any_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(watch_service.sys, "platform", "darwin")
    monkeypatch.setattr(watch_service.Path, "home", lambda: tmp_path / "user")
    home = tmp_path / "config"
    with pytest.raises(BackendError, match="absolute"):
        await watch_service.install_service(home=home, env_file=Path("credentials.env"))
    assert not home.exists()
    assert not (tmp_path / "user").exists()


def test_desired_manifest_preserves_absolute_env_symlink_leaf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(watch_service.Path, "home", lambda: tmp_path / "user")
    paths = watch_service.service_paths(tmp_path / "config")
    config = SimpleNamespace(
        backend="hikerapi",
        db_path=tmp_path / "db",
        output_dir=tmp_path / "out",
        aiograpi_session_path=tmp_path / "session",
    )
    target = tmp_path / "private.env"
    target.write_text("secret")
    leaf = tmp_path / "selected.env"
    leaf.symlink_to(target)
    manifest_bytes, _plist = watch_service._desired(paths, config, leaf)
    assert json.loads(manifest_bytes)["env_file"] == str(leaf)


@pytest.mark.asyncio
async def test_status_requires_executable_permission_for_interpreter_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(watch_service.sys, "platform", "darwin")
    home, paths, _config = _owned_artifacts(tmp_path, monkeypatch, plist=True)
    interpreter = tmp_path / "python"
    interpreter.write_text("binary")
    interpreter.chmod(0o600)
    manifest = json.loads(paths.manifest.read_text())
    manifest["python"] = str(interpreter)
    paths.manifest.write_text(json.dumps(manifest))
    paths.manifest.chmod(0o600)
    monkeypatch.setattr(
        watch_service.subprocess,
        "run",
        lambda *a, **k: _result(1, err=b"Could not find service"),
    )
    status = await watch_service.service_status(home=home)
    assert status["interpreter_available"] is False


@pytest.mark.asyncio
async def test_absent_uninstall_honors_existing_management_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(watch_service.sys, "platform", "darwin")
    monkeypatch.setattr(watch_service.Path, "home", lambda: tmp_path / "user")
    home = tmp_path / "config"
    paths = watch_service.service_paths(home)
    paths.directory.mkdir(parents=True, mode=0o700)
    home.chmod(0o700)
    (home / "services").chmod(0o700)
    paths.directory.chmod(0o700)
    with (
        watch_service._management_lock(paths),
        pytest.raises(BackendError, match="already in progress"),
    ):
        await watch_service.uninstall_service(home=home)


@pytest.mark.asyncio
async def test_cancelled_install_keeps_lock_until_native_mutation_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(watch_service.sys, "platform", "darwin")
    monkeypatch.setattr(watch_service.Path, "home", lambda: tmp_path / "user")
    home = tmp_path / "config"
    config = SimpleNamespace(
        backend="hikerapi",
        db_path=home / "db",
        output_dir=home / "out",
        aiograpi_session_path=home / "session",
    )
    monkeypatch.setattr(watch_service, "_resolve_config", lambda h, e: config)
    entered = threading.Event()
    release = threading.Event()

    def launchctl(args: list[str], **_: object) -> SimpleNamespace:
        if args[1] == "enable":
            entered.set()
            release.wait(timeout=5)
            return _result(0)
        if args[1:] == ["print", f"gui/{os.getuid()}"]:
            return _result(0)
        return _result(1, err=b"Could not find service")

    monkeypatch.setattr(watch_service.subprocess, "run", launchctl)
    install = asyncio.create_task(watch_service.install_service(home=home))
    assert await asyncio.to_thread(entered.wait, 2)
    install.cancel()
    await asyncio.sleep(0)
    install.cancel()
    await asyncio.sleep(0)
    try:
        with pytest.raises(BackendError, match="already in progress"):
            await watch_service.uninstall_service(home=home)
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await install


@pytest.mark.asyncio
async def test_status_treats_out_of_range_last_ok_as_unavailable_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(watch_service.sys, "platform", "darwin")
    home, _paths, config = _owned_artifacts(tmp_path, monkeypatch, plist=True)
    store = HistoryStore(config.db_path)
    store.register_watch("alice", 300)
    store.close()
    with sqlite3.connect(config.db_path) as connection:
        connection.execute(
            "UPDATE watches SET last_ok = ? WHERE user = 'alice'", (9223372036854775807,)
        )
    monkeypatch.setattr(
        watch_service.subprocess,
        "run",
        lambda *a, **k: _result(1, err=b"Could not find service"),
    )
    status = await watch_service.service_status(home=home)
    assert status["database_state"] == "unavailable"
    assert status["database_error"] == "watch database could not be read safely"
    assert "9223372036854775807" not in json.dumps(status)
