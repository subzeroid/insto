"""Installed-wheel C1 fixture, called only by the supervised opt-in native test."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import insto
from insto.config import Config
from insto.service.watch_service import service_paths, uninstall_service
from insto.service.watch_service_lifecycle import managed_service


def validate_fixture(home: Path) -> None:
    assert home.is_absolute() and home.resolve() == home
    for directory in (home.parent, home):
        info = directory.lstat()
        assert stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
        assert stat.S_IMODE(info.st_mode) == 0o700
    for path in (home / "config.toml", home / "store.db"):
        info = path.lstat()
        assert stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
        assert stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1
    assert (home / "config.toml").read_bytes() == b'backend = "fake"\n'


async def run(home: Path) -> None:
    validate_fixture(home)
    assert insto.__file__ is not None and "site-packages" in Path(insto.__file__).parts
    assert sys.platform == "darwin" and sys.flags.isolated and sys.dont_write_bytecode
    paths = service_paths(home)
    assert not paths.manifest.exists() and not paths.plist.exists()
    config = Config(
        backend="fake",
        db_path=home / "store.db",
        output_dir=home / "output",
        aiograpi_session_path=home / "aiograpi.session.json",
        cli_history_path=home / "cli_history",
    )
    evidence = {}
    with managed_service(home=home, config=config, deadline=time.monotonic() + 90) as lease:
        first = await lease.ensure_running()
        evidence["started"] = first
        second = await lease.ensure_running()
        assert second["process"]["pid"] == first["process"]["pid"]
        evidence["idempotent"] = second
        artifacts = paths.manifest.read_bytes(), paths.plist.read_bytes()
        stopped = await lease.ensure_stopped()
        assert stopped["registration"] == "unloaded" and stopped["executor"]["state"] == "idle"
        assert artifacts == (paths.manifest.read_bytes(), paths.plist.read_bytes())
        evidence["stopped"] = stopped
        restarted = await lease.ensure_running()
        evidence["restarted"] = restarted
        pid = restarted["process"]["pid"]
        assert isinstance(pid, int) and pid > 1 and pid == restarted["executor"]["pid"]
        command = subprocess.run(
            ["/bin/ps", "-ww", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        ).stdout.rstrip()
        suffix = f" -I -B -m insto.service.watch_service_runner {paths.manifest}"
        assert command.endswith(suffix), "refusing to signal unrelated process"
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 15
        while True:
            exited = await lease.inspect_owned()
            if exited["process"]["pid"] is None and exited["executor"]["state"] == "idle":
                break
            assert time.monotonic() < deadline, "owned service did not exit cleanly"
            await asyncio.sleep(0.1)
        assert exited["registration"] == "loaded" and exited["process"]["last_exit_code"] == 0
        evidence["clean_exit"] = exited
        evidence["kickstarted"] = await lease.ensure_running()
        assert evidence["kickstarted"]["executor"]["state"] == "busy"
        evidence["final_stop"] = await lease.ensure_stopped()
    await uninstall_service(home=home)
    assert not paths.manifest.exists() and not paths.plist.exists()
    validate_fixture(home)
    descriptor = os.open(
        home / "desktop-lifecycle.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
    )
    with os.fdopen(descriptor, "w") as stream:
        json.dump(evidence, stream, indent=2)
        stream.write("\n")


if __name__ == "__main__":
    asyncio.run(run(Path(sys.argv[1])))
