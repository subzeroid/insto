from __future__ import annotations

import asyncio
import json
import os
import plistlib
import sqlite3
import stat
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from insto.config import Config
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
@pytest.mark.parametrize("no_bytecode", [False, True])
async def test_install_writes_exact_artifacts_and_repeated_install_is_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_bytecode: bool
) -> None:
    home = tmp_path / "config"
    user_home = tmp_path / "user"
    monkeypatch.setattr(watch_service.sys, "platform", "darwin")
    monkeypatch.setattr(watch_service.sys, "dont_write_bytecode", no_bytecode)
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
        *(["-B"] if no_bytecode else []),
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


@pytest.mark.parametrize("stored_mode", [False, True])
@pytest.mark.parametrize("requested_mode", [False, True])
@pytest.mark.parametrize("loaded", [False, True])
async def test_install_argv_compatibility_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stored_mode: bool,
    requested_mode: bool,
    loaded: bool,
) -> None:
    monkeypatch.setattr(watch_service.sys, "platform", "darwin")
    monkeypatch.setattr(watch_service.sys, "dont_write_bytecode", False)
    home, paths, config = _owned_artifacts(tmp_path, monkeypatch, plist=True)
    # Construct the stored fixture independently from the new argv builder.
    document = plistlib.loads(paths.plist.read_bytes())
    if stored_mode:
        document["ProgramArguments"].insert(2, "-B")
    paths.plist.write_bytes(plistlib.dumps(document))
    before = (paths.manifest.read_bytes(), paths.plist.read_bytes())
    mtimes = (paths.manifest.stat().st_mtime_ns, paths.plist.stat().st_mtime_ns)
    monkeypatch.setattr(watch_service.sys, "dont_write_bytecode", requested_mode)
    monkeypatch.setattr(watch_service, "_resolve_config", lambda *args: config)
    calls: list[list[str]] = []
    registered = loaded

    def fake_run(args: list[str], **kwargs: object) -> SimpleNamespace:
        nonlocal registered
        calls.append(args)
        if args[1:] == ["print", f"gui/{os.getuid()}"]:
            return _result(0)
        if args[1] == "bootstrap":
            registered = True
        if args[1] == "print" and not registered:
            return _result(1, err=b"Could not find service")
        return _result(0, out=b"state = running\n")

    monkeypatch.setattr(watch_service.subprocess, "run", fake_run)
    if stored_mode != requested_mode:
        with pytest.raises(BackendError, match="refusing"):
            await watch_service.install_service(home=home)
        assert all(call[1] == "print" for call in calls)
    else:
        result = await watch_service.install_service(home=home)
        assert result["changed"] is (not loaded)
        assert result["registration"] == "loaded"
        assert [call[1] for call in calls if call[1] != "print"] == (
            [] if loaded else ["enable", "bootstrap"]
        )
    assert (paths.manifest.read_bytes(), paths.plist.read_bytes()) == before
    assert (paths.manifest.stat().st_mtime_ns, paths.plist.stat().st_mtime_ns) == mtimes


@pytest.mark.parametrize("stored_mode", [False, True])
@pytest.mark.parametrize("caller_mode", [False, True])
async def test_uninstall_accepts_only_exact_owned_argv_variants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stored_mode: bool, caller_mode: bool
) -> None:
    monkeypatch.setattr(watch_service.sys, "platform", "darwin")
    monkeypatch.setattr(watch_service.sys, "dont_write_bytecode", False)
    home, paths, _ = _owned_artifacts(tmp_path, monkeypatch, plist=True)
    document = plistlib.loads(paths.plist.read_bytes())
    if stored_mode:
        document["ProgramArguments"].insert(2, "-B")
    paths.plist.write_bytes(plistlib.dumps(document))
    monkeypatch.setattr(watch_service.sys, "dont_write_bytecode", caller_mode)
    monkeypatch.setattr(
        watch_service.subprocess,
        "run",
        lambda *args, **kwargs: _result(1, err=b"Could not find service"),
    )
    assert (await watch_service.uninstall_service(home=home))["installation"] == "not_installed"
    assert not paths.plist.exists() and not paths.manifest.exists()


@pytest.mark.parametrize("no_bytecode", [False, True])
@pytest.mark.parametrize("change", ["extra_flag", "duplicate_flag", "module", "cwd", "extra_key"])
async def test_uninstall_rejects_other_plist_changes_without_native_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_bytecode: bool, change: str
) -> None:
    monkeypatch.setattr(watch_service.sys, "platform", "darwin")
    monkeypatch.setattr(watch_service.sys, "dont_write_bytecode", False)
    home, paths, _ = _owned_artifacts(tmp_path, monkeypatch, plist=True)
    document = plistlib.loads(paths.plist.read_bytes())
    if no_bytecode:
        document["ProgramArguments"].insert(2, "-B")
    if change == "extra_flag":
        document["ProgramArguments"].insert(1, "-X")
    elif change == "duplicate_flag":
        document["ProgramArguments"].insert(1, "-I")
    elif change == "module":
        document["ProgramArguments"][-2] = "insto.other"
    elif change == "cwd":
        document["WorkingDirectory"] = str(tmp_path)
    else:
        document["EnvironmentVariables"] = {"PYTHONPATH": "untrusted"}
    paths.plist.write_bytes(plistlib.dumps(document))
    before = paths.plist.read_bytes()
    monkeypatch.setattr(
        watch_service.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("unexpected native call"),
    )
    with pytest.raises(BackendError, match="ownership mismatch"):
        await watch_service.uninstall_service(home=home)
    assert paths.plist.read_bytes() == before and paths.manifest.exists()


def test_no_bytecode_follows_installing_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, paths, config = _owned_artifacts(tmp_path, monkeypatch, plist=True)
    monkeypatch.setattr(watch_service.sys, "dont_write_bytecode", True)
    _, raw = watch_service._desired(paths, config, None)
    assert plistlib.loads(raw)["ProgramArguments"][1:4] == ["-I", "-B", "-m"]
    monkeypatch.setattr(watch_service.sys, "dont_write_bytecode", False)
    _, raw = watch_service._desired(paths, config, None)
    assert plistlib.loads(raw)["ProgramArguments"][1:3] == ["-I", "-m"]


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


@pytest.fixture
def registration_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> watch_service.ServicePaths:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / "Library" / "LaunchAgents").mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    paths = watch_service.service_paths(home)
    paths.directory.mkdir(parents=True, mode=0o700)
    paths.directory.parent.chmod(0o700)
    return paths


def test_registration_retention_and_replacement(
    registration_home: watch_service.ServicePaths,
) -> None:
    paths = registration_home
    assert watch_service.read_registration(paths) == (None, None)
    old = (b'{"old":1}\n', b"<plist>old</plist>\n")
    new = (b'{"new":1}\n', b"<plist>new</plist>\n")
    watch_service._atomic_write(paths.manifest, old[0])
    watch_service._atomic_write(paths.plist, old[1])
    assert watch_service.read_registration(paths) == old
    assert watch_service.read_retained_registration(paths) is None
    watch_service.retain_registration(paths, previous=old, candidate=new)
    assert watch_service.read_retained_registration(paths) == {"previous": old, "candidate": new}
    with pytest.raises(BackendError):
        watch_service.retain_registration(paths, previous=old, candidate=new)
    with pytest.raises(BackendError):
        watch_service.replace_registration(paths, new, old)  # bytes on disk are not `new`
    assert watch_service.read_registration(paths) == old
    watch_service.replace_registration(paths, old, new)
    assert watch_service.read_registration(paths) == new
    for path in (paths.manifest, paths.plist):
        info = path.lstat()
        assert stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1
    watch_service.discard_retained_registration(paths)
    assert watch_service.read_retained_registration(paths) is None
    watch_service.discard_retained_registration(paths)


def test_replace_registration_handles_absent_components(
    registration_home: watch_service.ServicePaths,
) -> None:
    paths = registration_home
    manifest_only = (b'{"m":1}\n', None)
    full = (b'{"m":2}\n', b"<plist>2</plist>\n")
    watch_service._atomic_write(paths.manifest, manifest_only[0])
    with pytest.raises(BackendError):
        watch_service.replace_registration(paths, full, manifest_only)  # plist expected, absent
    watch_service.replace_registration(paths, manifest_only, full)
    assert watch_service.read_registration(paths) == full
    watch_service.replace_registration(paths, full, manifest_only)
    assert watch_service.read_registration(paths) == manifest_only
    watch_service.retain_registration(paths, previous=manifest_only, candidate=full)
    assert watch_service.read_retained_registration(paths) == {
        "previous": manifest_only,
        "candidate": full,
    }


@pytest.mark.parametrize(
    "payload",
    [
        b"[]",
        b'{"schema_version":2}',
        b'{"schema_version":1,"previous":{},"candidate":{}}',
        b"{",
        # Parked 9: only the integer 1 is schema version 1 (JSON true and 1.0 compare equal).
        b'{"schema_version":true,"previous":{"manifest":null,"plist":null},'
        b'"candidate":{"manifest":null,"plist":null}}',
        b'{"schema_version":1.0,"previous":{"manifest":null,"plist":null},'
        b'"candidate":{"manifest":null,"plist":null}}',
        # Parked 3: nesting deep enough for json to give up, within the byte limit.
        b"[" * 100_000 + b"]" * 100_000,
        # C8: non-ASCII base64 raises ValueError, not binascii.Error.
        '{"schema_version":1,"previous":{"manifest":"é","plist":null},'
        '"candidate":{"manifest":null,"plist":null}}'.encode(),
    ],
)
def test_invalid_retained_registration_is_refused(
    registration_home: watch_service.ServicePaths, payload: bytes
) -> None:
    paths = registration_home
    watch_service._atomic_write(paths.directory / watch_service.RETAINED_REGISTRATION, payload)
    with pytest.raises(BackendError):
        watch_service.read_retained_registration(paths)


def test_atomic_write_and_replace_sync_their_directories(
    registration_home: watch_service.ServicePaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = registration_home
    synced: list[Path] = []
    monkeypatch.setattr(watch_service, "_sync_dir", lambda path: synced.append(path))
    watch_service._atomic_write(paths.manifest, b"a")
    watch_service.replace_registration(paths, (b"a", None), (b"b", b"p"))
    watch_service.discard_retained_registration(paths)
    watch_service.retain_registration(paths, previous=(b"b", b"p"), candidate=(b"c", b"q"))
    watch_service.discard_retained_registration(paths)
    assert synced.count(paths.directory) >= 4 and paths.plist.parent in synced


def _record_unlinks(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    order: list[str] = []
    original_unlink = Path.unlink

    def unlink(self: Path, *args: object, **kwargs: object) -> None:
        order.append(self.name)
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    return order


def test_replace_registration_removes_the_plist_before_the_manifest(
    registration_home: watch_service.ServicePaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = registration_home
    watch_service._atomic_write(paths.manifest, b"m")
    watch_service._atomic_write(paths.plist, b"p")
    order = _record_unlinks(monkeypatch)
    watch_service.replace_registration(paths, (b"m", b"p"), (None, None))
    assert order == [paths.plist.name, paths.manifest.name]
    assert watch_service.read_registration(paths) == (None, None)


def test_replace_registration_removes_the_plist_before_replacing_the_manifest(
    registration_home: watch_service.ServicePaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = registration_home
    watch_service._atomic_write(paths.manifest, b"m")
    watch_service._atomic_write(paths.plist, b"p")
    order = _record_unlinks(monkeypatch)
    original_replace = watch_service._replace_file

    def replace_file(path: Path, content: bytes) -> None:
        order.append(f"replace:{path.name}")
        original_replace(path, content)

    monkeypatch.setattr(watch_service, "_replace_file", replace_file)
    watch_service.replace_registration(paths, (b"m", b"p"), (b"m2", None))
    assert order == [paths.plist.name, f"replace:{paths.manifest.name}"]
    assert watch_service.read_registration(paths) == (b"m2", None)


def test_publication_failure_never_closes_a_reused_descriptor(
    registration_home: watch_service.ServicePaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = registration_home
    watch_service._atomic_write(paths.manifest, b"present")
    reused: list[int] = []
    original_link = os.link

    def link(src: str, dst: str) -> None:
        # Takes the descriptor number the temporary stream has just released.
        reused.append(os.open(paths.directory, os.O_RDONLY))
        original_link(src, dst)

    monkeypatch.setattr(os, "link", link)
    with pytest.raises(FileExistsError):
        watch_service._atomic_write(paths.manifest, b"new")
    try:
        os.fstat(reused[0])  # EBADF if the failure path closed a descriptor it no longer owned
    finally:
        os.close(reused[0])
    assert sorted(path.name for path in paths.directory.iterdir()) == ["manifest.json"]
    assert paths.manifest.read_bytes() == b"present"


def test_retain_registration_refuses_an_unreadable_document(
    registration_home: watch_service.ServicePaths,
) -> None:
    paths = registration_home
    with pytest.raises(BackendError, match="too large"):
        watch_service.retain_registration(
            paths, previous=(None, None), candidate=(b"x" * 200_000, None)
        )
    assert watch_service.read_retained_registration(paths) is None
    assert not (paths.directory / watch_service.RETAINED_REGISTRATION).exists()


@pytest.mark.asyncio
async def test_uninstall_removes_the_plist_before_the_manifest(
    registration_home: watch_service.ServicePaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = registration_home
    manifest, plist = watch_service._desired(
        paths,
        Config(
            backend="hikerapi", hiker_token="offline-order-token", db_path=paths.home / "store.db"
        ),
        None,
    )
    watch_service._atomic_write(paths.manifest, manifest)
    watch_service._atomic_write(paths.plist, plist)

    async def missing(
        arguments: list[str], *, timeout: float | None = None, deadline: float | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(arguments, 113, b"", b"Could not find service")

    monkeypatch.setattr(watch_service, "_launchctl", missing)
    monkeypatch.setattr(watch_service, "_require_macos", lambda: None)
    order = _record_unlinks(monkeypatch)
    await watch_service.uninstall_service(home=paths.home)
    assert order == [paths.plist.name, paths.manifest.name]


@pytest.mark.asyncio
async def test_uninstall_refuses_truncated_plist_without_native_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(watch_service.sys, "platform", "darwin")
    home, paths, _ = _owned_artifacts(tmp_path, monkeypatch, plist=False)
    paths.plist.write_bytes(
        b'<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0">\n<dict>\n'
    )
    paths.plist.chmod(0o600)
    calls: list[list[str]] = []
    monkeypatch.setattr(watch_service.subprocess, "run", lambda args, **_: calls.append(args))
    with pytest.raises(BackendError, match="invalid LaunchAgent plist"):
        await watch_service.uninstall_service(home=home)
    assert calls == [] and paths.manifest.exists() and paths.plist.exists()
