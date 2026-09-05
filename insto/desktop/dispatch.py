"""The capability allowlist and safe desktop response boundary."""

from __future__ import annotations

from typing import Any

from insto import __version__
from insto.desktop.protocol import PROTOCOL_VERSION, ProtocolError, Request, decode, encode
from insto.service.history import _SCHEMA_VERSION

_MESSAGES = {
    "invalid_request": "Invalid desktop request.",
    "unsupported_protocol": "Unsupported desktop protocol.",
    "unsupported_operation": "Unsupported desktop operation.",
    "invalid_params": "Invalid operation parameters.",
    "internal_error": "Desktop operation failed.",
}


async def dispatch(request: Request) -> dict[str, Any]:
    """Report only capabilities available without opening an application runtime."""
    if request.operation != "hello":
        raise ProtocolError("unsupported_operation", request.request_id)
    if request.params:
        raise ProtocolError("invalid_params", request.request_id)
    return {
        "core_version": __version__,
        "schema_version_supported": _SCHEMA_VERSION,
        "capabilities": ["hello"],
    }


async def handle(raw: bytes) -> bytes:
    """Contain protocol and operation failures without reflecting their details."""
    request_id: str | None = None
    try:
        request = decode(raw)
        request_id = request.request_id
        result = await dispatch(request)
        return encode(
            {
                "protocol_version": PROTOCOL_VERSION,
                "request_id": request_id,
                "result": result,
            }
        )
    except ProtocolError as error:
        code = error.code if error.code in _MESSAGES else "internal_error"
        if request_id is None and code == "unsupported_protocol":
            request_id = error.request_id
    except Exception:
        code = "internal_error"
    return encode(
        {
            "protocol_version": PROTOCOL_VERSION,
            "request_id": request_id,
            "error": {"code": code, "message": _MESSAGES[code], "retryable": False},
        }
    )
