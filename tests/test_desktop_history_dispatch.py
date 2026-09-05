import json
import sqlite3
import subprocess
import sys
import time
import types
from contextlib import closing
from pathlib import Path

import pytest

from insto.desktop.dispatch import handle
from insto.desktop.errors import DesktopError
from insto.desktop.history_params import CAPABILITIES


def wire(operation, params, request_id="history-1"):
    return (json.dumps({"protocol_version": 1, "request_id": request_id,
                       "operation": operation, "params": params}) + "\n").encode()


async def test_hello_advertises_all_four_saved_operations():
    result = json.loads(await handle(wire("hello", {})))["result"]
    assert set(CAPABILITIES) <= set(result["capabilities"])
    assert len(result["capabilities"]) == len(set(result["capabilities"]))


@pytest.mark.parametrize("operation,params", [
    ("snapshots.targets", {"username": "@Alice"}),
    ("snapshots.list", {"target_pk": "7"}),
    ("snapshots.compare", {"target_pk": "7", "older_id": "1", "newer_id": "2"}),
    ("changes.list", {}),
])
async def test_route_uses_normalized_params_and_prevalidation_deadline(monkeypatch, tmp_path, operation, params):
    from insto import desktop
    from insto.desktop import dispatch as router

    module = types.ModuleType("insto.desktop.history")
    captured = []
    real_validate = router.validate_history_params
    started = time.monotonic()

    def validate(name, value):
        time.sleep(0.01)
        return real_validate(name, value)

    def run(profile, name, value, *, deadline):
        captured.append((profile, name, value, deadline))
        return {"items": [], "next_cursor": None, "scan_complete": True, "scanned": 0}

    module.run = run
    monkeypatch.setattr(router, "validate_history_params", validate)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(desktop, "history", module, raising=False)
    monkeypatch.setenv("INSTO_DESKTOP_ROOT", str(tmp_path / "profile-root"))
    response = json.loads(await handle(wire(operation, params)))
    assert "result" in response
    profile, name, normalized, deadline = captured[0]
    assert profile.root == tmp_path / "profile-root"
    assert name == operation
    assert started + 10 <= deadline < time.monotonic() + 9.995
    if operation == "snapshots.targets":
        assert normalized["username"] == "alice"


@pytest.mark.parametrize("operation", CAPABILITIES)
async def test_invalid_params_precede_profile_history_and_provider_imports(monkeypatch, operation):
    from insto import desktop

    monkeypatch.delattr(desktop, "history", raising=False)
    for name in ("insto.desktop.profile", "insto.desktop.history", "insto.desktop.operations",
                 "insto.service.runtime", "hikerapi", "aiograpi"):
        monkeypatch.setitem(sys.modules, name, None)
    raw = await handle(wire(operation, {"home": "/foreign/private-token", "backend": "fake"}))
    assert b"private-token" not in raw
    assert json.loads(raw)["error"]["code"] == "invalid_params"


async def test_populated_timeout_is_one_static_error_envelope(monitoring_profile, monkeypatch):
    # Real boundary: a deadline expiry after a populated page was encoded must
    # leave exactly one static error line with the original request ID, no
    # `result`, and no saved bytes; the read transaction is released.
    from insto.desktop import history as desktop_history
    from tests.test_desktop_saved_history import fields, insert

    p = monitoring_profile
    insert(p, payload=fields(biography="private-observed-biography"))
    original = desktop_history._wire
    reached = []

    def expired(deadline):
        raise DesktopError("operation_timeout")

    def expire_after_populated_encoding(result):
        encoded = original(result)
        if result["items"]:
            reached.append(len(result["items"]))
            monkeypatch.setattr(desktop_history, "check_deadline", expired)
        return encoded

    monkeypatch.setattr(desktop_history, "_wire", expire_after_populated_encoding)
    monkeypatch.setenv("INSTO_DESKTOP_ROOT", str(p.root))
    raw = await handle(wire("snapshots.list", {"target_pk": "7"}, request_id="history-timeout"))
    assert reached == [1]
    assert raw.count(b"\n") == 1 and raw.endswith(b"\n")
    assert b"private-observed-biography" not in raw
    assert json.loads(raw) == {
        "protocol_version": 1,
        "request_id": "history-timeout",
        "error": {
            "code": "operation_timeout",
            "message": "The operation timed out; inspect its state before retrying.",
            "retryable": False,
        },
    }
    with closing(sqlite3.connect(p.home / "store.db", timeout=0.1)) as writer:
        assert writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone() == (0, 0, 0)


async def test_populated_list_and_compare_through_real_handle(monitoring_profile, monkeypatch):
    # Review gate G8: the routing patch, the real adapter and the C1 encoder are
    # exercised together with saved rows, not only on the error path.
    from tests.test_desktop_saved_history import fields, insert

    p = monitoring_profile
    older = insert(p, stamp=1, payload=fields(follower_count=1))
    newer = insert(
        p, stamp=2, payload=fields(follower_count=2, biography="private-observed-biography")
    )
    monkeypatch.setenv("INSTO_DESKTOP_ROOT", str(p.root))
    raw = await handle(wire("snapshots.list", {"target_pk": "7"}, request_id="list-1"))
    assert raw.count(b"\n") == 1 and b"private-observed-biography" not in raw
    assert json.loads(raw) == {
        "protocol_version": 1,
        "request_id": "list-1",
        "result": {
            "items": [
                {
                    "kind": "snapshot",
                    "snapshot": {"id": newer, "target_pk": "7", "captured_at": 2},
                },
                {
                    "kind": "snapshot",
                    "snapshot": {"id": older, "target_pk": "7", "captured_at": 1},
                },
            ],
            "next_cursor": None,
            "scan_complete": True,
            "scanned": 2,
        },
    }
    params = {"target_pk": "7", "older_id": older, "newer_id": newer}
    response = json.loads(await handle(wire("snapshots.compare", params, request_id="compare-1")))
    assert response["request_id"] == "compare-1" and "error" not in response
    result = response["result"]
    assert result["older"]["id"] == older and result["newer"]["id"] == newer
    assert {c["field"]: (c["old"], c["new"]) for c in result["changes"]} == {
        "biography": ("", "private-observed-biography"),
        "follower_count": (1, 2),
    }
    assert result["unknown_fields"] == []


def test_fresh_process_reads_without_provider_or_native_work(monitoring_profile):
    root = Path(__file__).resolve().parents[1]
    script = '''
import asyncio
import importlib.abc
import json
import socket
import subprocess
import sys

class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'hikerapi', 'aiograpi'} or fullname in {
            'insto.service.runtime', 'insto.service.watch_service',
            'insto.service.watch_daemon', 'insto.desktop.operations'
        }:
            raise AssertionError('forbidden import')

def forbidden(*args, **kwargs):
    raise AssertionError('forbidden effect')

sys.meta_path.insert(0, Block())
subprocess.Popen = forbidden
socket.create_connection = forbidden
from insto.desktop.dispatch import handle
sys.stdout.buffer.write(asyncio.run(handle(sys.stdin.buffer.read())))
'''
    environment = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C.UTF-8",
                   "PYTHONDONTWRITEBYTECODE": "1",
                   "INSTO_DESKTOP_ROOT": str(monitoring_profile.root),
                   "INSTO_HOME": "/foreign", "HIKERAPI_TOKEN": "foreign-private-token"}
    result = subprocess.run([sys.executable, "-B", "-c", script], cwd=root, env=environment,
                            input=wire("changes.list", {}), capture_output=True, timeout=12)
    assert result.returncode == 0 and result.stderr == b""
    assert result.stdout.count(b"\n") == 1
    assert b"foreign-private-token" not in result.stdout
    assert json.loads(result.stdout)["result"] == {
        "items": [], "next_cursor": None, "scan_complete": True, "scanned": 0,
    }
