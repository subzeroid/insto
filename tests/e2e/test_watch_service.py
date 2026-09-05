"""Opt-in native smoke using a wheel-installed interpreter and an isolated store.

Set INSTO_TEST_LAUNCHD=1 and INSTO_TEST_PYTHON=/path/to/installed/venv/bin/python.
The test creates exactly one temporary user LaunchAgent, never live watches.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import sqlite3
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or os.environ.get("INSTO_TEST_LAUNCHD") != "1",
    reason="native LaunchAgent smoke requires explicit INSTO_TEST_LAUNCHD=1 on macOS",
)


def _wait_for(check: Callable[[], bool], seconds: float = 30) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.2)
    assert check(), "timed out waiting for isolated LaunchAgent transition"


def _python_flags() -> list[str]:
    return ["-I", *(["-B"] if os.environ.get("INSTO_TEST_NO_BYTECODE") == "1" else [])]


def _create_test_home(tmp_path: Path) -> Path:
    requested = os.environ.get("INSTO_TEST_HOME")
    parent = tmp_path if requested is None else tmp_path.parent
    info = parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("native test home parent must be a private owned directory")
    home = parent.resolve() / "service home"
    if requested is not None and (not Path(requested).is_absolute() or Path(requested) != home):
        raise ValueError("native test home must be the exact new child of basetemp")
    # mkdir without exist_ok rejects existing data and symlink leaves. A
    # supervisor can predict this path before pytest starts any LaunchAgent.
    home.mkdir(mode=0o700)
    return home


def test_installed_launchagent_lifecycle(tmp_path: Path) -> None:
    python = os.environ.get("INSTO_TEST_PYTHON")
    if not python:
        pytest.skip("set INSTO_TEST_PYTHON to a wheel-installed interpreter")
    python_flags = _python_flags()
    domain = f"gui/{os.getuid()}"
    probe = subprocess.run(
        ["/bin/launchctl", "print", domain],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=10,
    )
    if probe.returncode:
        pytest.skip("macOS GUI launchd domain is unavailable")
    home = _create_test_home(tmp_path)
    db = home / "store.db"
    config = home / "config.toml"
    config.write_text('backend = "fake"\n', encoding="utf-8")
    config.chmod(0o600)
    env_file = home / "service-env.toml"
    secret = "isolated-launchd-credential"
    env_file.write_text(f'[env]\nHIKERAPI_TOKEN = "{secret}"\n', encoding="utf-8")
    env_file.chmod(0o600)
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("INSTO_", "HIKERAPI_", "AIOGRAPI_", "PYTHON"))
    }
    env["INSTO_HOME"] = str(home)
    # The existing one-shot entrypoint selects its fake backend via this env;
    # install ignores it and the runner separately pins the config's backend.
    env["INSTO_BACKEND"] = "fake"
    label = f"io.insto.watch.{os.getuid()}.{hashlib.sha256(os.fsencode(home)).hexdigest()[:16]}"
    plist = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    manifest = home / "services" / "watch" / "manifest.json"
    assert not plist.exists(), "refusing to reuse an existing LaunchAgent"

    def cli(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [python, *python_flags, "-m", "insto", *args],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=45,
        )
        if check:
            assert result.returncode == 0, result.stderr
        return result

    origin = subprocess.run(
        [python, *python_flags, "-c", "import insto; print(insto.__file__)"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout.strip()
    assert "site-packages" in Path(origin).parts, "native smoke requires installed wheel"

    def status() -> dict[str, Any]:
        return json.loads(cli("watch-service", "status", "--json").stdout)

    def last_ok() -> int:
        with sqlite3.connect(db) as connection:
            row = connection.execute("SELECT last_ok FROM watches WHERE user='alice'").fetchone()
        return int(row[0] or 0) if row else 0

    def force_due() -> None:
        with sqlite3.connect(db) as connection:
            connection.execute("UPDATE watches SET last_ok=0 WHERE user='alice'")

    def verified_pid() -> int:
        pid = status()["process"]["pid"]
        assert isinstance(pid, int) and pid > 1, "no verified service PID"
        lock_pid = int(Path(f"{db}.watch.lock").read_text().strip())
        assert pid == lock_pid, "service is not this test's executor"
        command = subprocess.run(
            ["/bin/ps", "-ww", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        ).stdout
        expected_suffix = (
            f" {' '.join(python_flags)} -m insto.service.watch_service_runner {manifest}"
        )
        assert command.rstrip().endswith(expected_suffix), "refusing to signal unrelated process"
        return pid

    try:
        cli("@alice", "-c", "watch", "600")
        force_due()
        desktop = subprocess.run(
            [python, "-I", "-B", str(Path(__file__).with_name("desktop_lifecycle.py")), str(home)],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert desktop.returncode == 0, desktop.stderr
        assert (home / "desktop-lifecycle.json").is_file()
        force_due()
        cli("watch-service", "install", "--env-file", str(env_file))
        assert secret not in plist.read_text()
        assert secret not in manifest.read_text()
        _wait_for(lambda: last_ok() > 0)
        first_pid = verified_pid()
        cli("watch-service", "install", "--env-file", str(env_file))
        assert verified_pid() == first_pid, "identical install restarted the service"
        force_due()
        os.kill(verified_pid(), signal.SIGKILL)
        _wait_for(lambda: last_ok() > 0, seconds=45)
        assert verified_pid() != first_pid, "failed service did not restart"
        os.kill(verified_pid(), signal.SIGTERM)
        _wait_for(lambda: status()["process"]["pid"] is None)
        time.sleep(11)  # launchd's normal respawn throttle is ten seconds.
        stopped = status()
        assert stopped["registration"] == "loaded"
        assert stopped["process"]["pid"] is None, "clean exit unexpectedly restarted"
        assert stopped["process"]["last_exit_code"] == 0
        assert stopped["watches"][0]["username"] == "alice"
        cli("watch-service", "uninstall")
        assert not plist.exists()
        assert not manifest.exists()
        assert config.exists() and db.exists() and env_file.exists()
        assert last_ok() > 0
    finally:
        if manifest.exists() or plist.exists():
            result = cli("watch-service", "uninstall", check=False)
            assert result.returncode == 0, (
                f"isolated service cleanup failed; inspect {domain}/{label} and {plist}"
            )
