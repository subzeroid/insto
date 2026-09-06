"""Only implemented C1 commands and exact parameters cross the bridge."""

import json
import sys
import types
from unittest.mock import AsyncMock

import pytest

from insto.desktop.dispatch import handle
from insto.desktop.errors import MESSAGES, DesktopError

CAPABILITIES = [
    "hello",
    "setup.inspect",
    "setup.configure",
    "settings.inspect",
    "credentials.replace",
    "service.start",
    "service.stop",
    "service.repair",
    "overview",
    "watches.list",
    "watches.add",
    "watches.update",
    "watches.pause",
    "watches.resume",
    "watches.remove",
    "snapshots.targets",
    "snapshots.list",
    "snapshots.compare",
    "changes.list",
    "service.inspect",
    "service.migrate",
    "service.uninstall",
    "home.inspect",
    "home.select",
]

C3_CAPABILITIES = [
    "service.inspect",
    "service.migrate",
    "service.uninstall",
    "home.inspect",
    "home.select",
]


def wire(operation, params):
    return (
        json.dumps(
            {"protocol_version": 1, "request_id": "c1", "operation": operation, "params": params}
        )
        + "\n"
    ).encode()


async def test_exact_c2_capabilities():
    response = json.loads(await handle(wire("hello", {})))
    assert response["result"]["capabilities"] == list(CAPABILITIES)
    assert len(CAPABILITIES) == 24
    assert list(CAPABILITIES[-5:]) == C3_CAPABILITIES


@pytest.mark.parametrize("operation", ["home.inspect", "home.select"])
@pytest.mark.parametrize(
    "params, code",
    [
        ({}, "invalid_params"),
        ({"path": 5}, "invalid_params"),
        ({"path": ""}, "invalid_params"),
        ({"path": "/x", "token": "offline-sentinel"}, "invalid_params"),
        ({"path": "relative"}, "home_invalid"),
        ({"path": "/a/../b"}, "home_invalid"),
        ({"path": "~alice/.insto"}, "home_invalid"),
        ({"path": "/" + "a" * 1024}, "invalid_params"),
    ],
)
async def test_home_params_are_validated_before_importing_home(
    monkeypatch, operation, params, code
):
    monkeypatch.setitem(sys.modules, "insto.desktop.home", None)
    monkeypatch.setitem(sys.modules, "insto.desktop.operations", None)
    raw = await handle(wire(operation, params))
    assert b"offline-sentinel" not in raw
    assert json.loads(raw)["error"]["code"] == code


async def test_home_inspect_rejects_a_null_path(monkeypatch):
    monkeypatch.setitem(sys.modules, "insto.desktop.home", None)
    raw = await handle(wire("home.inspect", {"path": None}))
    assert json.loads(raw)["error"]["code"] == "invalid_params"


@pytest.mark.parametrize("operation", ["service.inspect", "service.migrate", "service.uninstall"])
async def test_c3_service_operations_take_no_params(monkeypatch, operation):
    monkeypatch.setitem(sys.modules, "insto.desktop.operations", None)
    monkeypatch.setitem(sys.modules, "insto.desktop.migration", None)
    raw = await handle(wire(operation, {"token": "offline-sentinel"}))
    assert b"offline-sentinel" not in raw
    assert json.loads(raw)["error"]["code"] == "invalid_params"


async def test_c3_operations_reach_their_modules(monkeypatch, tmp_path):
    from insto.desktop import home, migration, operations

    calls = []
    monkeypatch.setenv("INSTO_DESKTOP_ROOT", str(tmp_path / "root"))

    def record(name):
        async def call(*args, **kwargs):
            calls.append(name)
            return {"ok": name}

        return call

    monkeypatch.setattr(operations, "inspect_service", record("service.inspect"))
    monkeypatch.setattr(migration, "migrate", record("service.migrate"))
    monkeypatch.setattr(migration, "uninstall", record("service.uninstall"))
    monkeypatch.setattr(home, "inspect", record("home.inspect"))
    monkeypatch.setattr(home, "select", record("home.select"))
    for operation in ("service.inspect", "service.migrate", "service.uninstall"):
        assert json.loads(await handle(wire(operation, {})))["result"] == {"ok": operation}
    probe = str(tmp_path / "x")
    assert json.loads(await handle(wire("home.inspect", {"path": probe})))["result"] == {
        "ok": "home.inspect"
    }
    assert json.loads(await handle(wire("home.select", {"path": None})))["result"] == {
        "ok": "home.select"
    }
    assert calls == [
        "service.inspect",
        "service.migrate",
        "service.uninstall",
        "home.inspect",
        "home.select",
    ]


@pytest.mark.parametrize("operation", ["setup.configure", "credentials.replace"])
@pytest.mark.parametrize(
    "params",
    [
        {},
        {"token": None},
        {"token": True},
        {"token": 8},
        {"token": "abc"},
        {"token": " a-token"},
        {"token": "token\n"},
        {"token": "токен"},
        {"token": "x" * 4097},
        {"token": "offline-sentinel", "home": "/foreign"},
        {"token": "offline-sentinel", "backend": "fake"},
    ],
)
async def test_invalid_token_params_never_import_operations(monkeypatch, operation, params):
    monkeypatch.setitem(sys.modules, "insto.desktop.operations", None)
    raw = await handle(wire(operation, params))
    assert b"offline-sentinel" not in raw
    response = json.loads(raw)
    assert response["request_id"] == "c1"
    assert response["error"]["code"] == "invalid_params"


@pytest.mark.parametrize(
    "operation",
    [
        "setup.inspect",
        "settings.inspect",
        "service.start",
        "service.stop",
        "service.repair",
    ],
)
async def test_empty_only_params(monkeypatch, operation):
    monkeypatch.setitem(sys.modules, "insto.desktop.operations", None)
    response = json.loads(await handle(wire(operation, {"runtime": "/foreign"})))
    assert response["error"]["code"] == "invalid_params"


@pytest.mark.parametrize(
    "operation,function,argument",
    [
        ("setup.inspect", "inspect_profile", None),
        ("settings.inspect", "inspect_profile", None),
        ("setup.configure", "configure", "offline-sentinel"),
        ("credentials.replace", "replace_credentials", "offline-sentinel"),
        ("service.start", "change_service", "start"),
        ("service.stop", "change_service", "stop"),
        ("service.repair", "change_service", "repair"),
    ],
)
async def test_exact_routes(monkeypatch, tmp_path, operation, function, argument):
    from insto import desktop

    module = types.ModuleType("insto.desktop.operations")
    action = AsyncMock(return_value={"status": "stopped"})
    setattr(module, function, action)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(desktop, "operations", module, raising=False)
    monkeypatch.setenv("INSTO_DESKTOP_ROOT", str(tmp_path / "gui"))
    params = {"token": argument} if operation in {"setup.configure", "credentials.replace"} else {}
    raw = await handle(wire(operation, params))
    assert raw.count(b"\n") == 1
    assert b"offline-sentinel" not in raw
    assert json.loads(raw)["result"] == {"status": "stopped"}
    args = action.await_args.args
    assert args[0].root == tmp_path / "gui"
    assert args[1:] == (() if argument is None else (argument,))


@pytest.mark.parametrize("code", MESSAGES)
async def test_domain_errors_are_static_and_preserve_id(monkeypatch, code):
    from insto.desktop import dispatch

    async def fail(request):
        raise DesktopError(code) from RuntimeError("previous-and-new-secret")

    monkeypatch.setattr(dispatch, "dispatch", fail)
    raw = await handle(wire("service.stop", {}))
    assert b"previous-and-new-secret" not in raw
    message, retryable = MESSAGES[code]
    assert json.loads(raw) == {
        "protocol_version": 1,
        "request_id": "c1",
        "error": {"code": code, "message": message, "retryable": retryable},
    }
