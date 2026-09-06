import json
import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path

import pytest

from insto.config import Config
from insto.desktop.errors import DesktopError
from insto.exceptions import BackendError
from insto.service import watch_service

DEADLINE = 1e12


def home_config(home: Path) -> Config:
    return Config(
        backend="hikerapi",
        hiker_token="offline-facts-token",
        db_path=home / "store.db",
        output_dir=home / "output",
        cli_history_path=home / "cli_history",
        aiograpi_session_path=home / "aiograpi.session.json",
    )


def registration(home: Path, *, python: str | None = None, plist: bool = True) -> dict:
    paths = watch_service.service_paths(home)
    for directory in (home / "services", paths.directory):
        directory.mkdir(mode=0o700, exist_ok=True)
    manifest, document = watch_service._desired(paths, home_config(home), None)
    value = json.loads(manifest)
    if python is not None:
        value["python"] = python
        manifest = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
        document = plistlib.dumps(
            watch_service._plist_document(
                paths, value, dont_write_bytecode=sys.dont_write_bytecode
            ),
            fmt=plistlib.FMT_XML,
            sort_keys=True,
        )
    paths.manifest.write_bytes(manifest)
    paths.manifest.chmod(0o600)
    if plist:
        paths.plist.write_bytes(document)
        paths.plist.chmod(0o600)
    return value


def job_output(value: dict, *, state: str = "running", program: str | None = None) -> bytes:
    arguments = [
        program or value["python"],
        "-I",
        *(["-B"] if sys.dont_write_bytecode else []),
        "-m",
        "insto.service.watch_service_runner",
        str(watch_service.service_paths(Path(value["config_home"])).manifest),
    ]
    lines = [f"\tprogram = {arguments[0]}", "\targuments = {"]
    lines += [f"\t\t{argument}" for argument in arguments]
    lines += ["\t}", f"\tstate = {state}", "\tpid = 41" if state == "running" else ""]
    return "\n".join(lines).encode()


@pytest.fixture
def home(tmp_path, monkeypatch):
    (tmp_path / "Library" / "LaunchAgents").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    home = tmp_path / "cli-home"
    home.mkdir(mode=0o700)
    return home


def launchctl(
    monkeypatch, *, returncode: int, stdout: bytes = b"", stderr: bytes = b"", error=None
):
    async def fake(arguments, *, timeout=None, deadline=None):
        if error is not None:
            raise error
        return subprocess.CompletedProcess(
            [watch_service._LAUNCHCTL, *arguments], returncode, stdout, stderr
        )

    monkeypatch.setattr(watch_service, "_launchctl", fake)


ABSENT = {
    "registration": "none",
    "installation": None,
    "interpreter": None,
    "interpreter_exists": None,
    "loaded": False,
    "process": "stopped",
    "settings": None,
}


async def test_no_files_and_no_job_is_none(home, monkeypatch):
    from insto.desktop.service_facts import registration_facts

    launchctl(monkeypatch, returncode=113, stderr=b"Could not find service")
    assert await registration_facts(watch_service.service_paths(home), deadline=DEADLINE) == ABSENT


async def test_owned_current_interpreter_loaded_running(home, monkeypatch):
    from insto.desktop.service_facts import registration_facts

    value = registration(home)
    launchctl(monkeypatch, returncode=0, stdout=job_output(value))
    facts = await registration_facts(
        watch_service.service_paths(home), deadline=DEADLINE, expected=value
    )
    assert facts == {
        "registration": "owned",
        "installation": "installed",
        "interpreter": "current",
        "interpreter_exists": True,
        "loaded": True,
        "process": "running",
        "settings": "matching",
    }


async def test_other_interpreter_and_different_settings(home, monkeypatch, tmp_path):
    from insto.desktop.service_facts import registration_facts

    other = tmp_path / "old-runtime" / "python3"
    value = registration(home, python=str(other))
    launchctl(monkeypatch, returncode=0, stdout=job_output(value, state="waiting"))
    expected = dict(value, db_path=str(home / "elsewhere.db"))
    facts = await registration_facts(
        watch_service.service_paths(home), deadline=DEADLINE, expected=expected
    )
    assert facts["registration"] == "owned" and facts["interpreter"] == "other"
    assert facts["interpreter_exists"] is False and facts["loaded"] is True
    assert facts["process"] == "stopped" and facts["settings"] == "different"


async def test_manifest_without_plist_is_owned_but_incomplete(home, monkeypatch):
    from insto.desktop.service_facts import registration_facts

    registration(home, plist=False)
    launchctl(monkeypatch, returncode=113, stderr=b"Could not find service")
    facts = await registration_facts(watch_service.service_paths(home), deadline=DEADLINE)
    assert facts["registration"] == "owned" and facts["installation"] == "incomplete"
    assert facts["loaded"] is False


@pytest.mark.parametrize(
    "case",
    ["plist_only", "manifest_only_loaded", "foreign_manifest", "job_without_files", "foreign_job"],
)
async def test_unknown_ownership_cases(home, monkeypatch, case, tmp_path):
    from insto.desktop.service_facts import registration_facts

    paths = watch_service.service_paths(home)
    value = None
    if case == "plist_only":
        registration(home)
        paths.manifest.unlink()
    elif case == "manifest_only_loaded":
        value = registration(home, plist=False)
    elif case == "foreign_manifest":
        registration(home)
        raw = json.loads(paths.manifest.read_bytes())
        raw["uid"] = os.getuid() + 1
        paths.manifest.write_bytes(json.dumps(raw).encode())
    elif case == "foreign_job":
        value = registration(home)
    if case in {"manifest_only_loaded", "job_without_files"}:
        launchctl(monkeypatch, returncode=0, stdout=b"\tstate = running\n\tpid = 5\n")
    elif case == "foreign_job":
        launchctl(
            monkeypatch,
            returncode=0,
            stdout=job_output(value, program=str(tmp_path / "other" / "python3")),
        )
    else:
        launchctl(monkeypatch, returncode=113, stderr=b"Could not find service")
    facts = await registration_facts(paths, deadline=DEADLINE)
    assert facts["registration"] == "unknown" and facts["interpreter"] is None
    assert facts["settings"] is None


async def test_unknown_launchctl_output_is_unknown_not_stopped(home, monkeypatch):
    from insto.desktop.service_facts import registration_facts

    registration(home)
    launchctl(monkeypatch, returncode=5, stderr=b"Input/output error")
    facts = await registration_facts(watch_service.service_paths(home), deadline=DEADLINE)
    assert facts["loaded"] is None and facts["process"] == "unknown"
    assert facts["registration"] == "owned"


async def test_missing_launchctl_degrades_to_file_facts(home, monkeypatch):
    from insto.desktop.service_facts import registration_facts

    registration(home)
    launchctl(
        monkeypatch, returncode=0, error=BackendError("launchctl operation failed or timed out")
    )
    facts = await registration_facts(watch_service.service_paths(home), deadline=DEADLINE)
    assert facts["registration"] == "owned" and facts["loaded"] is None
    assert facts["process"] == "unknown"


async def test_expired_deadline_is_a_timeout_not_degraded_facts(home, monkeypatch):
    from insto.desktop.service_facts import registration_facts

    registration(home)
    launchctl(monkeypatch, returncode=0, error=BackendError("launchctl operation deadline expired"))
    with pytest.raises(DesktopError, match="operation_timeout"):
        await registration_facts(watch_service.service_paths(home), deadline=time.monotonic() - 1)


async def test_inspect_service_on_unconfigured_profile_is_none(tmp_path):
    from insto.desktop import operations
    from insto.desktop.profile import Profile

    result = await operations.inspect_service(Profile(tmp_path / "desktop"))
    assert result == {
        "registration": "none",
        "interpreter": None,
        "interpreter_exists": None,
        "loaded": None,
        "settings": None,
    }


async def test_inspect_service_compares_the_home_configuration(home, monkeypatch, tmp_path):
    from insto.desktop import operations
    from insto.desktop.configuration import initialize_database
    from insto.desktop.profile import Profile

    (home / "config.toml").write_bytes(
        b'backend = "hikerapi"\n[hikerapi]\ntoken = "offline-facts-token"\n'
    )
    (home / "config.toml").chmod(0o600)
    initialize_database(home / "store.db")
    registration(home, python=str(tmp_path / "old" / "python3"))
    root = tmp_path / "desktop"
    own = Profile(root)
    with own.locked(initialize=True):
        own.write_binding(home)
    adopted = Profile(root, home=home)
    with adopted.locked(initialize=True):
        adopted.write_state(adopted.new_state(remaining=None, desired="stopped"))
    launchctl(monkeypatch, returncode=113, stderr=b"Could not find service")
    result = await operations.inspect_service(adopted)
    assert result["registration"] == "owned" and result["interpreter"] == "other"
    assert result["settings"] == "matching" and result["loaded"] is False
    assert "installation" not in result and "process" not in result


async def test_corrupt_plist_is_unknown_without_raising(home, monkeypatch):
    from insto.desktop.service_facts import registration_facts

    paths = watch_service.service_paths(home)
    registration(home)
    paths.plist.write_bytes(b"<?xml version='1.0'?><plist><dict><key>a</key>")
    paths.plist.chmod(0o600)
    launchctl(monkeypatch, returncode=113, stderr=b"Could not find service")
    facts = await registration_facts(paths, deadline=DEADLINE)
    assert facts["registration"] == "unknown" and facts["settings"] is None


@pytest.mark.skipif(os.geteuid() == 0, reason="permission bits do not bind root")
async def test_interpreter_below_untraversable_directory_is_absent(home, monkeypatch, tmp_path):
    from insto.desktop.service_facts import registration_facts

    locked = tmp_path / "locked"
    locked.mkdir(mode=0o700)
    (locked / "python3").write_bytes(b"")
    registration(home, python=str(locked / "python3"))
    launchctl(monkeypatch, returncode=113, stderr=b"Could not find service")
    locked.chmod(0)
    try:
        facts = await registration_facts(watch_service.service_paths(home), deadline=DEADLINE)
    finally:
        locked.chmod(0o700)
    assert facts["registration"] == "owned" and facts["interpreter"] == "other"
    assert facts["interpreter_exists"] is False


async def test_partial_expected_leaves_settings_unknown(home, monkeypatch):
    from insto.desktop.service_facts import registration_facts

    registration(home)
    launchctl(monkeypatch, returncode=113, stderr=b"Could not find service")
    facts = await registration_facts(
        watch_service.service_paths(home), deadline=DEADLINE, expected={"backend": "hikerapi"}
    )
    assert facts["registration"] == "owned" and facts["settings"] is None


async def test_loaded_job_without_state_line_is_unknown_process(home, monkeypatch):
    from insto.desktop.service_facts import registration_facts

    value = registration(home)
    stateless = b"\n".join(
        line for line in job_output(value).splitlines() if not line.startswith(b"\tstate")
    )
    launchctl(monkeypatch, returncode=0, stdout=stateless)
    facts = await registration_facts(watch_service.service_paths(home), deadline=DEADLINE)
    assert facts["registration"] == "owned" and facts["loaded"] is True
    assert facts["process"] == "unknown"


async def test_malformed_job_output_is_unknown_ownership(home, monkeypatch):
    from insto.desktop.service_facts import registration_facts

    value = registration(home)
    unterminated = job_output(value).replace(b"\t}\n", b"")
    launchctl(monkeypatch, returncode=0, stdout=unterminated)
    facts = await registration_facts(watch_service.service_paths(home), deadline=DEADLINE)
    assert facts["registration"] == "unknown" and facts["loaded"] is True


async def test_framed_job_output_parses_as_owned_running(home, monkeypatch):
    from insto.desktop.service_facts import registration_facts

    value = registration(home)
    paths = watch_service.service_paths(home)
    framed = b"".join(
        [
            f"gui/{os.getuid()}/{paths.label} = {{\n".encode(),
            job_output(value),
            b"\n\tproperties = {\n\t\tpartial import = 0\n\t}\n}\n",
        ]
    )
    launchctl(monkeypatch, returncode=0, stdout=framed)
    facts = await registration_facts(paths, deadline=DEADLINE, expected=value)
    assert facts["registration"] == "owned" and facts["loaded"] is True
    assert facts["process"] == "running" and facts["settings"] == "matching"
