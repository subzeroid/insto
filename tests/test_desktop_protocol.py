"""The private desktop wire contract and its fail-closed boundaries."""

import dataclasses
import importlib
import json
from typing import Any

import pytest


def wire(**changes: Any) -> bytes:
    envelope = dict(protocol_version=1, request_id="req_1", operation="hello", params={})
    envelope.update(changes)
    return (json.dumps(envelope) + "\n").encode()


def test_request_contract() -> None:
    protocol = importlib.import_module("insto.desktop.protocol")
    request = protocol.decode(wire())
    assert dataclasses.is_dataclass(request)
    assert not hasattr(request, "__dict__")
    with pytest.raises(dataclasses.FrozenInstanceError):
        request.operation = "other"
    assert (request.request_id, request.operation, request.params) == ("req_1", "hello", {})
    assert protocol.PROTOCOL_VERSION == 1
    assert protocol.MAX_INPUT_BYTES == 65536
    assert protocol.MAX_OUTPUT_BYTES == 2097152
    assert str(protocol.ProtocolError("invalid_request", "secret")) == "invalid_request"


@pytest.mark.parametrize("request_id", ["a", "A_-09", "a" * 64])
def test_valid_ids(request_id: str) -> None:
    from insto.desktop.protocol import decode

    assert decode(wire(request_id=request_id)).request_id == request_id


@pytest.mark.parametrize(
    "raw",
    [
        b"\xff\n",
        b"[]\n",
        b"null\n",
        b"1\n",
        b"{}\n",
        wire()[:-1],
        wire() + b"\n",
        wire() + wire(),
        wire(extra=True),
        wire(protocol_version=True),
        wire(protocol_version=1.0),
        wire(operation=None),
        wire(operation=1),
        wire(params=[]),
        wire(params=None),
        *[
            wire(request_id=value)
            for value in ["", "a" * 65, None, 1, True, [], {}, "a b", "é", "a\n"]
        ],
        *[
            wire().replace(b'"params": {}', b'"params": {"x": ' + value + b"}")
            for value in [b"NaN", b"Infinity", b"-Infinity", b"1e999", b"-1e999"]
        ],
        wire().replace(b'"params": {}', b'"params": {"x": {"a": 1, "a": 2}}'),
        wire().replace(b'"params": {}', b'"params": {}, "params": {}'),
        wire().replace(
            b'"params": {}', b'"params": {"x":' + b"[" * 20000 + b"0" + b"]" * 20000 + b"}"
        ),
        *[
            (
                json.dumps(
                    {key: value for key, value in json.loads(wire()).items() if key != missing}
                )
                + "\n"
            ).encode()
            for missing in ["protocol_version", "request_id", "operation", "params"]
        ],
    ],
)
def test_invalid_envelopes(raw: bytes) -> None:
    from insto.desktop.protocol import ProtocolError, decode

    with pytest.raises(ProtocolError) as error:
        decode(raw)
    assert (error.value.code, error.value.request_id) == ("invalid_request", None)


def test_input_budget_includes_newline() -> None:
    from insto.desktop.protocol import MAX_INPUT_BYTES, ProtocolError, decode

    raw = wire()
    exact = raw[:-1] + b" " * (MAX_INPUT_BYTES - len(raw)) + b"\n"
    assert len(exact) == MAX_INPUT_BYTES
    assert decode(exact).operation == "hello"
    with pytest.raises(ProtocolError, match=r"^invalid_request$"):
        decode(exact[:-1] + b" \n")


@pytest.mark.parametrize("version", [0, -1, 2, 999999999999999999999])
def test_unsupported_version(version: int) -> None:
    from insto.desktop.protocol import ProtocolError, decode

    with pytest.raises(ProtocolError) as error:
        decode(wire(protocol_version=version))
    assert (error.value.code, error.value.request_id) == ("unsupported_protocol", "req_1")
    with pytest.raises(ProtocolError) as error:
        decode(wire(protocol_version=version, operation=None))
    assert (error.value.code, error.value.request_id) == ("invalid_request", None)


def test_output_budget_and_escaping() -> None:
    from insto.desktop.protocol import MAX_OUTPUT_BYTES, encode

    overhead = len(encode({"x": ""}))
    exact = encode({"x": "a" * (MAX_OUTPUT_BYTES - overhead)})
    assert len(exact) == MAX_OUTPUT_BYTES
    with pytest.raises(ValueError):
        encode({"x": "a" * (MAX_OUTPUT_BYTES - overhead + 1)})
    value = {"x": 'é\n\r\t\u2028"\\'}
    encoded = encode(value)
    assert encoded.isascii() and encoded.count(b"\n") == 1 and encoded.endswith(b"\n")
    assert json.loads(encoded) == value


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_output_rejects_nonfinite(value: float) -> None:
    from insto.desktop.protocol import encode

    with pytest.raises(ValueError):
        encode({"nested": [value]})


async def test_hello() -> None:
    from insto import __version__
    from insto.desktop.dispatch import handle
    from insto.service.history import _SCHEMA_VERSION

    assert json.loads(await handle(wire())) == {
        "protocol_version": 1,
        "request_id": "req_1",
        "result": {
            "core_version": __version__,
            "schema_version_supported": _SCHEMA_VERSION,
            "capabilities": [
                "hello",
                "setup.inspect",
                "setup.configure",
                "settings.inspect",
                "credentials.replace",
                "service.start",
                "service.stop",
                "service.repair",
            ],
        },
    }


@pytest.mark.parametrize(
    "raw,code,message,request_id",
    [
        (
            wire(operation="secret /tmp/file; DROP TABLE x"),
            "unsupported_operation",
            "Unsupported desktop operation.",
            "req_1",
        ),
        (
            wire(params={"secret": "value"}),
            "invalid_params",
            "Invalid operation parameters.",
            "req_1",
        ),
        (
            wire(protocol_version=2),
            "unsupported_protocol",
            "Unsupported desktop protocol.",
            "req_1",
        ),
        (wire(request_id="bad id"), "invalid_request", "Invalid desktop request.", None),
        (b"secret\n", "invalid_request", "Invalid desktop request.", None),
    ],
)
async def test_safe_errors(raw: bytes, code: str, message: str, request_id: str | None) -> None:
    from insto.desktop.dispatch import handle

    assert json.loads(await handle(raw)) == {
        "protocol_version": 1,
        "request_id": request_id,
        "error": {"code": code, "message": message, "retryable": False},
    }


@pytest.mark.parametrize("failure", ["exception", "serialization", "oversized"])
async def test_internal_errors_preserve_id_without_details(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    from insto.desktop import dispatch as module

    async def broken(request: Any) -> dict[str, Any]:
        if failure == "exception":
            raise RuntimeError("SENTINEL_SECRET")
        if failure == "oversized":
            return {"SENTINEL_SECRET": "a" * (2 * 1024 * 1024)}
        return {"SENTINEL_SECRET": object()}

    monkeypatch.setattr(module, "dispatch", broken)
    raw = await module.handle(wire())
    assert b"SENTINEL_SECRET" not in raw
    assert json.loads(raw) == {
        "protocol_version": 1,
        "request_id": "req_1",
        "error": {
            "code": "internal_error",
            "message": "Desktop operation failed.",
            "retryable": False,
        },
    }
