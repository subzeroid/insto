"""Every C3 operation through the real `python -I -B -m insto.desktop` child, offline."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from insto.desktop.configuration import initialize_database
from tests.test_desktop_entrypoint import EXACT_CAPABILITIES

TOKEN = "offline-bridge-token"
DARWIN = sys.platform == "darwin"


def bridge(tmp_path: Path, root: Path, operation: str, params: dict[str, Any]) -> dict[str, Any]:
    """One request through an isolated child whose HOME is a fake user home.

    `~/Library/LaunchAgents` lookups never see the real directory; the child's
    `INSTO_HOME` points at an unused path and its desktop root at `root`. On macOS
    the facts perform one `launchctl print` read for a label derived from the
    temporary home; nothing registers a LaunchAgent.
    """
    fake_home = tmp_path / "fake-user-home"
    (fake_home / "Library" / "LaunchAgents").mkdir(parents=True, exist_ok=True)
    request = {
        "protocol_version": 1,
        "request_id": "c3",
        "operation": operation,
        "params": params,
    }
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-m", "insto.desktop"],
        input=(json.dumps(request) + "\n").encode(),
        capture_output=True,
        timeout=60,
        cwd=tmp_path,
        env={
            **{k: v for k, v in os.environ.items() if not k.startswith(("INSTO_", "HIKERAPI_"))},
            "HOME": str(fake_home),
            "INSTO_HOME": str(tmp_path / "unused-cli-home"),
            "INSTO_DESKTOP_ROOT": str(root),
        },
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == b""
    assert result.stdout.count(b"\n") == 1
    assert TOKEN.encode() not in result.stdout
    response = json.loads(result.stdout)
    assert response["request_id"] == "c3"
    return response


def seed_home(tmp_path: Path, name: str = "cli-home", *, database: bool = True) -> Path:
    home = tmp_path / name
    home.mkdir(mode=0o700)
    config = home / "config.toml"
    config.write_bytes(f'backend = "hikerapi"\n[hikerapi]\ntoken = "{TOKEN}"\n'.encode())
    config.chmod(0o600)
    if database:
        initialize_database(home / "store.db")
    return home


def test_hello_lists_24_capabilities(tmp_path: Path) -> None:
    response = bridge(tmp_path, tmp_path / "root", "hello", {})
    assert len(EXACT_CAPABILITIES) == 24
    assert response["result"]["capabilities"] == EXACT_CAPABILITIES
    assert not (tmp_path / "root").exists()


@pytest.mark.parametrize("operation", ["service.migrate", "service.uninstall"])
def test_mutations_on_an_unconfigured_root_are_not_configured(
    tmp_path: Path, operation: str
) -> None:
    response = bridge(tmp_path, tmp_path / "root", operation, {})
    assert response["error"]["code"] == "not_configured"
    assert not (tmp_path / "root").exists()


def test_service_inspect_on_an_unconfigured_root(tmp_path: Path) -> None:
    response = bridge(tmp_path, tmp_path / "root", "service.inspect", {})
    assert response["result"] == {
        "registration": "none",
        "interpreter": None,
        "interpreter_exists": None,
        "loaded": None,
        "settings": None,
    }


@pytest.mark.parametrize(
    "params, code",
    [
        ({}, "invalid_params"),
        ({"path": "relative"}, "home_invalid"),
        ({"path": None}, "invalid_params"),
    ],
)
def test_home_inspect_rejects_bad_params(tmp_path: Path, params: dict[str, Any], code: str) -> None:
    assert bridge(tmp_path, tmp_path / "root", "home.inspect", params)["error"]["code"] == code


def test_home_inspect_reports_a_seeded_cli_home(tmp_path: Path) -> None:
    home = seed_home(tmp_path)
    report = bridge(tmp_path, tmp_path / "root", "home.inspect", {"path": str(home)})["result"]
    assert report["exists"] is True
    assert report["private"] is True
    assert report["config"] == "ok"
    assert report["backend"] == "hikerapi"
    assert report["database"] == "ok"
    assert report["registration"] == "none"
    assert report["adoptable"] is True
    assert report["reason"] is None
    assert sorted(p.name for p in home.iterdir()) == ["config.toml", "store.db"]
    absent = bridge(tmp_path, tmp_path / "root", "home.inspect", {"path": str(tmp_path / "no")})
    assert absent["result"]["exists"] is False
    assert absent["result"]["reason"] == "home_invalid"


def test_adoption_round_trip(tmp_path: Path) -> None:
    root = tmp_path / "root"
    home = seed_home(tmp_path, database=False)
    binding = root / "desktop-home.json"
    state = home / "desktop-state.json"
    selected = bridge(tmp_path, root, "home.select", {"path": str(home)})["result"]
    assert selected["configured"] is True
    assert selected["desired_service"] == "stopped"
    assert selected["quota_remaining"] is None
    assert selected["quota_checked_at"] is None
    assert json.loads(binding.read_bytes())["home"] == str(home)
    assert state.exists()
    assert (home / "store.db").exists()
    inspected = bridge(tmp_path, root, "settings.inspect", {})["result"]
    assert inspected["configured"] is True
    assert inspected["quota_remaining"] is None
    if DARWIN:
        # On Linux the lifecycle needs launchd and reports service_error instead.
        assert inspected["status"] == "stopped"
    facts = bridge(tmp_path, root, "service.inspect", {})["result"]
    assert facts["registration"] == "none"
    # macOS: launchctl reports the label as missing; Linux has no launchctl to ask.
    assert facts["loaded"] is (False if DARWIN else None)
    persisted = (binding.read_bytes(), state.read_bytes())
    noop = bridge(tmp_path, root, "home.select", {"path": str(home)})["result"]
    assert noop["configured"] is True
    assert (binding.read_bytes(), state.read_bytes()) == persisted
    if DARWIN:
        for operation in ("service.migrate", "service.uninstall"):
            dto = bridge(tmp_path, root, operation, {})["result"]
            assert dto["configured"] is True, operation
            assert dto["status"] == "stopped", operation
        assert not (home / "desktop-recovery.json").exists()
    back = bridge(tmp_path, root, "home.select", {"path": None})["result"]
    assert back["configured"] is False
    assert back["status"] == "unconfigured"
    assert not binding.exists()
    # Adoption locks the home in place (`.desktop.lock`) and creates the service
    # directories plus a missing database; nothing else is left behind.
    assert sorted(p.name for p in home.iterdir()) == [
        ".desktop.lock",
        "config.toml",
        "desktop-state.json",
        "services",
        "store.db",
    ]
    assert not any((tmp_path / "fake-user-home" / "Library" / "LaunchAgents").iterdir())
    # The token is persisted exactly once: in the adopted home's own config.
    for path in (*root.rglob("*"), *home.rglob("*")):
        if path.is_file():
            assert (TOKEN.encode() in path.read_bytes()) == (path == home / "config.toml"), path


def test_select_refuses_a_fake_backend_home(tmp_path: Path) -> None:
    home = tmp_path / "fake-home"
    home.mkdir(mode=0o700)
    (home / "config.toml").write_bytes(b'backend = "fake"\n')
    (home / "config.toml").chmod(0o600)
    response = bridge(tmp_path, tmp_path / "root", "home.select", {"path": str(home)})
    assert response["error"]["code"] == "home_backend_unsupported"
    assert not (tmp_path / "root" / "desktop-home.json").exists()
