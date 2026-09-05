"""Bounded single-line JSON envelopes for the private desktop interface."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any

PROTOCOL_VERSION = 1
MAX_INPUT_BYTES = 64 * 1024
MAX_OUTPUT_BYTES = 2 * 1024 * 1024

_REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_KEYS = {"protocol_version", "request_id", "operation", "params"}


@dataclass(frozen=True, slots=True)
class Request:
    request_id: str
    operation: str
    params: dict[str, Any]


class ProtocolError(Exception):
    def __init__(self, code: str, request_id: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.request_id = request_id


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    raise ValueError("nonfinite number")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("nonfinite number")
    return number


def decode(raw: bytes) -> Request:
    """Validate the whole envelope before trusting its request identifier."""
    if len(raw) > MAX_INPUT_BYTES or not raw.endswith(b"\n") or raw.count(b"\n") != 1:
        raise ProtocolError("invalid_request")
    try:
        envelope = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ProtocolError("invalid_request") from None
    if (
        not isinstance(envelope, dict)
        or envelope.keys() != _KEYS
        or type(envelope["protocol_version"]) is not int
        or not isinstance(envelope["request_id"], str)
        or _REQUEST_ID.fullmatch(envelope["request_id"]) is None
        or not isinstance(envelope["operation"], str)
        or not isinstance(envelope["params"], dict)
    ):
        raise ProtocolError("invalid_request")
    request_id = envelope["request_id"]
    if envelope["protocol_version"] != PROTOCOL_VERSION:
        raise ProtocolError("unsupported_protocol", request_id)
    return Request(request_id, envelope["operation"], envelope["params"])


def encode(response: dict[str, Any]) -> bytes:
    """Serialize one ASCII JSON line, enforcing the full wire byte budget."""
    raw = (
        json.dumps(response, ensure_ascii=True, allow_nan=False, separators=(",", ":")) + "\n"
    ).encode("ascii")
    if len(raw) > MAX_OUTPUT_BYTES:
        raise ValueError("desktop response exceeds byte limit")
    return raw
