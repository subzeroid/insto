"""Local usage operations and their exact, secret-safe bridge contract."""

import json

import pytest

from insto.desktop.dispatch import CAPABILITIES, handle


def request(operation, params):
    return (
        json.dumps(
            {"protocol_version": 1, "request_id": "usage", "operation": operation, "params": params}
        )
        + "\n"
    ).encode()


async def test_capabilities_append_usage_in_exact_order():
    assert CAPABILITIES[-4:] == (
        "lookup.profile",
        "lookup.activity",
        "usage.inspect",
        "usage.configure",
    )


async def test_inspect_without_setup_is_local_and_creates_nothing(tmp_path, monkeypatch):
    root = tmp_path / "desktop"
    monkeypatch.setenv("INSTO_DESKTOP_ROOT", str(root))
    result = json.loads(await handle(request("usage.inspect", {})))["result"]
    assert result.keys() == {"kind", "day", "used", "daily_limit", "reset_at"}
    assert result["kind"] == "usage" and result["used"] == 0
    assert result["daily_limit"] is None
    assert not root.exists()


async def test_configure_uses_configured_database_directory(monitoring_profile, monkeypatch):
    from insto.service.usage import UsageLedger, usage_path

    monkeypatch.setenv("INSTO_DESKTOP_ROOT", str(monitoring_profile.root))
    payload = json.loads(await handle(request("usage.configure", {"daily_limit": 5})))
    assert payload["result"]["daily_limit"] == 5
    ledger = UsageLedger(usage_path(monitoring_profile.home / "store.db"))
    await ledger.reserve()
    result = json.loads(await handle(request("usage.inspect", {})))["result"]
    assert result["used"] == 1 and result["daily_limit"] == 5
    result = json.loads(await handle(request("usage.configure", {"daily_limit": None})))["result"]
    assert result["used"] == 1 and result["daily_limit"] is None


async def test_configure_without_setup_creates_nothing(tmp_path, monkeypatch):
    root = tmp_path / "desktop"
    monkeypatch.setenv("INSTO_DESKTOP_ROOT", str(root))
    result = json.loads(await handle(request("usage.configure", {"daily_limit": 1})))
    assert result["error"]["code"] == "not_configured"
    assert not root.exists()


async def test_corrupt_usage_returns_static_storage_error(monitoring_profile, monkeypatch):
    from insto.service.usage import UsageLedger, usage_path

    monkeypatch.setenv("INSTO_DESKTOP_ROOT", str(monitoring_profile.root))
    ledger = UsageLedger(usage_path(monitoring_profile.home / "store.db"))
    await ledger.configure(1)
    ledger.path.write_bytes(b"secret-corrupt-data")
    response = await handle(request("usage.inspect", {}))
    assert b"secret-corrupt-data" not in response
    assert json.loads(response)["error"] == {
        "code": "storage_error",
        "message": "Private profile storage is unavailable.",
        "retryable": False,
    }


@pytest.mark.parametrize(
    "operation,params",
    [
        ("usage.inspect", {"extra": 1}),
        ("usage.configure", {}),
        ("usage.configure", {"daily_limit": True}),
        ("usage.configure", {"daily_limit": 0}),
        ("usage.configure", {"daily_limit": 1_000_001}),
        ("usage.configure", {"daily_limit": 2, "extra": 1}),
    ],
)
async def test_invalid_parameters_precede_profile_access(tmp_path, monkeypatch, operation, params):
    root = tmp_path / "desktop"
    monkeypatch.setenv("INSTO_DESKTOP_ROOT", str(root))
    result = json.loads(await handle(request(operation, params)))
    assert result["error"]["code"] == "invalid_params"
    assert not root.exists()
