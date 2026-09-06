"""Opt-in native smoke: CLI-registered service → adopt → migrate → uninstall via the bridge.

Set INSTO_TEST_LAUNCHD=1 and INSTO_TEST_PYTHON=/path/to/installed/venv/bin/python (the
"old" interpreter). The bridge runs under this pytest interpreter (the "new" one);
the two must be distinct interpreters or the smoke skips, since a migration between
identical interpreters proves nothing.
Exactly one temporary user LaunchAgent, zero watches, an offline token and an
unreachable proxy, so no provider request can ever leave the host.

The old CLI installs from a foreign working directory (its default ``./output`` is
pinned against that cwd), which is the realistic case the migration must accept.
Each run leaves exactly one persistent launchd enable/disable override entry for its
label in ``launchctl print gui/<uid>`` (a domain flag, not a job, a plist or a file);
launchd keeps it forever and the cleanup never touches it.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from insto.service.watch_service import RETAINED_REGISTRATION
from tests.e2e.test_watch_service import _create_test_home, _python_flags, _wait_for

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or os.environ.get("INSTO_TEST_LAUNCHD") != "1",
    reason="native migration smoke requires explicit INSTO_TEST_LAUNCHD=1 on macOS",
)

TOKEN = "isolated-migration-credential"
# The runner's own readiness poll is ten seconds; a loaded host (or a CI runner)
# needs headroom above that for two interpreter cold starts per transition.
TRANSITION_SECONDS = 60


def _interpreter_identity(python: str, flags: list[str]) -> str:
    """The absolute path a process started this way records for its own interpreter.

    This is what ``watch_service`` writes into the manifest and what launchd then
    execs, and it is not always the path used to invoke it: inside a venv built on
    a macOS framework Python, ``sys.executable`` is the framework binary, not the
    venv's ``bin/python``. Ask each interpreter instead of guessing.
    """
    return subprocess.run(
        [python, *flags, "-c", "import os, sys; print(os.path.abspath(sys.executable))"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout.strip()


def test_migration_through_the_bridge(tmp_path: Path) -> None:
    old_python = os.environ.get("INSTO_TEST_PYTHON")
    if not old_python:
        pytest.skip("set INSTO_TEST_PYTHON to a wheel-installed interpreter")
    old_python = os.path.abspath(old_python)  # the manifest and `ps` report absolute paths
    domain = f"gui/{os.getuid()}"
    probe = subprocess.run(["/bin/launchctl", "print", domain], capture_output=True, timeout=10)
    if probe.returncode:
        pytest.skip("macOS GUI launchd domain is unavailable")
    flags = _python_flags()
    old_identity = _interpreter_identity(old_python, flags)
    new_identity = _interpreter_identity(sys.executable, ["-I", "-B"])  # the bridge's own flags
    if old_identity == new_identity:
        pytest.skip(
            "INSTO_TEST_PYTHON resolves to the same interpreter as the bridge "
            f"({new_identity}), so the smoke cannot prove a switch"
        )
    home = _create_test_home(tmp_path)
    root = tmp_path / "desktop-root"
    root.mkdir(mode=0o700)
    config = home / "config.toml"
    config.write_text(
        f'backend = "hikerapi"\n[hikerapi]\ntoken = "{TOKEN}"\nproxy = "http://127.0.0.1:9"\n',
        encoding="utf-8",
    )
    config.chmod(0o600)
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("INSTO_", "HIKERAPI_", "AIOGRAPI_", "PYTHON"))
    }
    env["INSTO_HOME"] = str(home)
    label = f"io.insto.watch.{os.getuid()}.{hashlib.sha256(os.fsencode(home)).hexdigest()[:16]}"
    plist = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    manifest = home / "services" / "watch" / "manifest.json"
    retained = manifest.with_name(RETAINED_REGISTRATION)
    assert not plist.exists(), "refusing to reuse an existing LaunchAgent"

    def cli(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        # A foreign cwd: the CLI pins its default `./output` against it, and the
        # bridge normalizes the output directory to the home on migration (R21).
        result = subprocess.run(
            [old_python, *flags, "-m", "insto", *args],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=45,
        )
        if check:
            assert result.returncode == 0, result.stderr
        return result

    def bridge(operation: str, params: dict[str, Any]) -> dict[str, Any]:
        request = {
            "protocol_version": 1,
            "request_id": "e2e",
            "operation": operation,
            "params": params,
        }
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-m", "insto.desktop"],
            input=(json.dumps(request) + "\n").encode(),
            capture_output=True,
            timeout=150,
            cwd=tmp_path,
            env={**env, "INSTO_DESKTOP_ROOT": str(root)},
        )
        assert result.returncode == 0, result.stderr
        assert result.stderr == b"" and TOKEN.encode() not in result.stdout
        response = json.loads(result.stdout)
        assert "result" in response, response
        return response["result"]

    def status() -> dict[str, Any]:
        return json.loads(cli("watch-service", "status", "--json").stdout)

    def executor_pid() -> int | None:
        try:
            return int(Path(f"{home / 'store.db'}.watch.lock").read_text().strip())
        except (FileNotFoundError, ValueError):
            return None

    def ready() -> bool:
        # launchd reports a PID the moment it spawns the job; the runner publishes
        # the same PID in the executor lock only once it owns the database.
        pid = status()["process"]["pid"]
        return pid is not None and pid == executor_pid()

    def verified_pid(interpreter: str) -> int:
        pid = status()["process"]["pid"]
        assert isinstance(pid, int) and pid > 1, "no verified service PID"
        assert pid == executor_pid(), "service is not this test's executor"
        command = subprocess.run(
            ["/bin/ps", "-ww", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        ).stdout.rstrip()
        assert command.startswith(f"{interpreter} "), command
        assert command.endswith(f"-m insto.service.watch_service_runner {manifest}"), command
        return pid

    try:
        cli("watch-service", "install")
        _wait_for(ready, seconds=TRANSITION_SECONDS)
        old_pid = verified_pid(old_identity)
        assert json.loads(manifest.read_text())["python"] == old_identity
        assert len(bridge("hello", {})["capabilities"]) == 24
        report = bridge("home.inspect", {"path": str(home)})
        assert report["adoptable"] and report["registration"] == "owned"
        assert report["interpreter"] == "other" and report["process"] == "running"
        selected = bridge("home.select", {"path": str(home)})
        assert selected["configured"] and selected["desired_service"] == "running"
        assert (root / "desktop-home.json").exists() and (home / "desktop-state.json").exists()
        facts = bridge("service.inspect", {})
        assert facts == {
            "registration": "owned",
            "interpreter": "other",
            "interpreter_exists": True,
            "loaded": True,
            "settings": "matching",
        }
        migrated = bridge("service.migrate", {})
        assert migrated["status"] == "running" and migrated["service_running"] is True
        new_pid = verified_pid(new_identity)
        assert new_pid != old_pid
        assert json.loads(manifest.read_text())["python"] == new_identity
        assert not retained.exists()
        assert not (home / "desktop-recovery.json").exists()
        assert TOKEN not in plist.read_text() and TOKEN not in manifest.read_text()
        assert bridge("service.inspect", {})["interpreter"] == "current"
        assert bridge("service.migrate", {})["status"] == "running"
        assert verified_pid(new_identity) == new_pid, "no-op migration restarted the service"
        assert bridge("service.stop", {})["status"] == "stopped"
        _wait_for(lambda: status()["process"]["pid"] is None, seconds=TRANSITION_SECONDS)
        assert bridge("service.start", {})["status"] == "running"
        verified_pid(new_identity)
        removed = bridge("service.uninstall", {})
        assert removed["status"] == "stopped" and removed["desired_service"] == "stopped"
        assert not plist.exists() and not manifest.exists()
        assert config.exists() and (home / "store.db").exists()
        gone = subprocess.run(
            ["/bin/launchctl", "print", f"{domain}/{label}"], capture_output=True, timeout=10
        )
        diagnostic = (gone.stderr + gone.stdout).decode("utf-8", "replace").lower()
        assert gone.returncode != 0 and "could not find service" in diagnostic, diagnostic
        returned = bridge("home.select", {"path": None})
        assert returned["configured"] is False
        assert not (root / "desktop-home.json").exists()
    finally:
        native_cleanup(domain, label, plist, manifest, home / "store.db")


def native_cleanup(domain: str, label: str, plist: Path, manifest: Path, db: Path) -> None:
    """Fixture-owned cleanup that needs no bridge operation and no healthy journal.

    Boots the label out if loaded, removes the test's own files (plist first), then
    proves the job is gone and no executor still holds the lock, whatever state a
    failed migration left behind (old, new or mixed registration, pending journal).
    """
    target = f"{domain}/{label}"
    loaded = subprocess.run(["/bin/launchctl", "print", target], capture_output=True, timeout=10)
    bootout = b""
    if loaded.returncode == 0:
        result = subprocess.run(
            ["/bin/launchctl", "bootout", target], capture_output=True, timeout=30
        )
        bootout = result.stderr + result.stdout
    for path in (plist, manifest):
        if path.exists():
            path.unlink()
    deadline = time.monotonic() + 30
    while True:
        gone = subprocess.run(["/bin/launchctl", "print", target], capture_output=True, timeout=10)
        if gone.returncode != 0 or time.monotonic() >= deadline:
            break
        time.sleep(0.5)  # bootout of a live job returns before launchd drops the label
    diagnostic = (gone.stderr + gone.stdout).decode("utf-8", "replace").lower()
    assert gone.returncode != 0 and "could not find service" in diagnostic, (
        diagnostic,
        bootout.decode("utf-8", "replace"),
    )
    lock = Path(f"{db}.watch.lock")
    if lock.exists():
        with lock.open("rb") as stream:  # a still-running executor would hold this flock
            while True:
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise AssertionError("an executor still holds the watch lock") from None
                    time.sleep(0.5)  # a booted-out runner may still be shutting down
                    continue
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                break
    assert not plist.exists() and not manifest.exists(), "LaunchAgent cleanup not confirmed"
