import base64
import sys

import pytest

from insto.desktop.errors import DesktopError
from insto.desktop.history_params import (
    CAPABILITIES,
    decode_cursor,
    encode_cursor,
    validate_params,
)


def test_normalized_defaults_and_decimal_identity():
    assert validate_params("snapshots.targets", {"username": "@Alice"}) == {
        "username": "alice",
        "limit": 50,
        "cursor": None,
    }
    assert validate_params("snapshots.list", {"target_pk": "9007199254740993"}) == {
        "target_pk": "9007199254740993",
        "limit": 50,
        "cursor": None,
    }
    assert validate_params("changes.list", {}) == {
        "target_pk": None,
        "limit": 50,
        "cursor": None,
    }
    assert validate_params("snapshots.targets", {"username": "A" * 255})["username"] == "a" * 255
    # Review gate G12: one canonical form shared with the CLI and watches.add.
    from insto.service.history import _canonical_watch_user

    for raw in ("@@Alice ", "@alice", "ALICE", "  alice  "):
        assert validate_params("snapshots.targets", {"username": raw})["username"] == "alice"
        assert _canonical_watch_user(raw) == "alice"
    with pytest.raises(ValueError):
        _canonical_watch_user(" @alice")  # the CLI order keeps this invalid; so do we


@pytest.mark.parametrize("value", [True, 1.0, 0, 51, "3", None])
def test_limit_is_a_bounded_actual_integer(value):
    with pytest.raises(DesktopError, match="invalid_params"):
        validate_params("changes.list", {"limit": value})


@pytest.mark.parametrize(
    "operation,params",
    [
        ("snapshots.targets", {}),
        ("snapshots.targets", {"username": ""}),
        ("snapshots.targets", {"username": " @ "}),
        ("snapshots.targets", {"username": " @alice"}),
        ("snapshots.targets", {"username": ".."}),
        ("snapshots.targets", {"username": "é"}),
        ("snapshots.targets", {"username": "a" * 256}),
        ("snapshots.list", {"target_pk": 123}),
        ("snapshots.list", {"target_pk": "01"}),
        ("snapshots.list", {"target_pk": "1" * 65}),
        ("changes.list", {"target_pk": None}),
        ("changes.list", {"cursor": None}),
        ("changes.list", {"cursor": "x" * 1025}),
        ("changes.list", {"home": "/foreign"}),
        ("changes.list", {"backend": "hikerapi"}),
        ("snapshots.compare", {"target_pk": "1", "older_id": "1", "newer_id": "1"}),
        ("snapshots.compare", {"target_pk": "1", "older_id": "01", "newer_id": "2"}),
        (
            "snapshots.compare",
            {"target_pk": "1", "older_id": "1", "newer_id": "9223372036854775808"},
        ),
        ("snapshots.compare", {"target_pk": "1", "older_id": "1", "newer_id": True}),
    ],
)
def test_rejects_ambiguous_or_expansive_parameters(operation, params):
    with pytest.raises(DesktopError, match="invalid_params"):
        validate_params(operation, params)


def raw_cursor(value):
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def test_cursor_binds_operation_filter_and_ceiling():
    token = encode_cursor("snapshots.list", "7", 99, (123, 40))
    assert decode_cursor(token, "snapshots.list", "7") == (99, (123, 40))
    assert len(token) <= 1024
    for operation, value in [("changes.list", "7"), ("snapshots.list", "8")]:
        with pytest.raises(DesktopError, match="invalid_params"):
            decode_cursor(token, operation, value)
    params = validate_params("snapshots.list", {"target_pk": "7", "cursor": token})
    assert params["cursor"] == (99, (123, 40))


@pytest.mark.parametrize(
    "token",
    [
        "!",
        "e30=",
        raw_cursor("{}"),
        raw_cursor('{"v":1,"v":1,"o":"changes.list","f":null,"c":"9","t":1,"i":"1"}'),
        raw_cursor('{"v":true,"o":"changes.list","f":null,"c":"9","t":1,"i":"1"}'),
        raw_cursor('{"v":2,"o":"changes.list","f":null,"c":"9","t":1,"i":"1"}'),
        raw_cursor('{"v":1,"o":"changes.list","f":null,"c":"9","t":NaN,"i":"1"}'),
        raw_cursor('{"v":1,"o":"changes.list","f":null,"c":"9","t":1.0,"i":"1"}'),
        raw_cursor('{"v":1,"o":"changes.list","f":null,"c":"9","t":1,"i":"10"}'),
        raw_cursor('{"v":1,"o":"changes.list","f":null,"c":"9","t":-1,"i":"1"}'),
        raw_cursor('{"v":1,"o":"changes.list","f":null,"c":"9","t":1,"i":"1","x":0}'),
    ],
)
def test_cursor_is_strict(token):
    with pytest.raises(DesktopError, match="invalid_params"):
        decode_cursor(token, "changes.list", None)


def test_parameter_module_has_no_profile_or_provider_imports(monkeypatch):
    import importlib

    for name in ("insto.desktop.profile", "insto.desktop.history", "hikerapi", "aiograpi"):
        monkeypatch.setitem(sys.modules, name, None)
    module = importlib.reload(sys.modules["insto.desktop.history_params"])
    assert len(module.CAPABILITIES) == len(CAPABILITIES) == 4
    assert module.validate_params("changes.list", {})["limit"] == 50
