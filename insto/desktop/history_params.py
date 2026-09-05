"""Pure validation for bounded saved-history operations and traversal cursors."""

from __future__ import annotations

import base64
import json
import re
from typing import Any, NoReturn

from insto.desktop.errors import DesktopError

CAPABILITIES = (
    "snapshots.targets",
    "snapshots.list",
    "snapshots.compare",
    "changes.list",
)
MAX_CURSOR = 1024
MAX_ID = 9223372036854775807
MAX_TIME = 253402300799
_PK = re.compile(r"[1-9][0-9]{0,63}")
_ID = re.compile(r"[1-9][0-9]{0,18}")
_USER = re.compile(r"[a-z0-9._]{1,255}")
_CURSOR = re.compile(r"[A-Za-z0-9_-]{1,1024}")


def _invalid() -> NoReturn:
    raise DesktopError("invalid_params")


def _decimal(value: Any, *, snapshot: bool = False) -> str:
    pattern = _ID if snapshot else _PK
    if type(value) is not str or pattern.fullmatch(value) is None:
        _invalid()
    if snapshot and int(value) > MAX_ID:
        _invalid()
    return str(value)


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _invalid()
        result[key] = value
    return result


def _constant(value: str) -> Any:
    _invalid()


def encode_cursor(
    operation: str,
    filter_value: str | None,
    ceiling: int,
    frontier: tuple[int, int],
) -> str:
    value = {
        "v": 1,
        "o": operation,
        "f": filter_value,
        "c": str(ceiling),
        "t": frontier[0],
        "i": str(frontier[1]),
    }
    raw = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    token = base64.urlsafe_b64encode(raw.encode("ascii")).decode("ascii").rstrip("=")
    if len(token) > MAX_CURSOR:
        _invalid()
    return token


def decode_cursor(
    token: Any,
    operation: str,
    filter_value: str | None,
) -> tuple[int, tuple[int, int]]:
    if type(token) is not str or _CURSOR.fullmatch(token) is None:
        _invalid()
    try:
        payload = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
        if base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=") != token:
            _invalid()
        value = json.loads(
            payload.decode("ascii"), object_pairs_hook=_unique, parse_constant=_constant
        )
    except (ValueError, UnicodeError, RecursionError):
        raise DesktopError("invalid_params") from None
    if (
        type(value) is not dict
        or value.keys() != {"v", "o", "f", "c", "t", "i"}
        or type(value["v"]) is not int
        or value["v"] != 1
        or value["o"] != operation
        or value["f"] != filter_value
        or type(value["t"]) is not int
        or not 0 <= value["t"] <= MAX_TIME
    ):
        _invalid()
    ceiling = int(_decimal(value["c"], snapshot=True))
    identifier = int(_decimal(value["i"], snapshot=True))
    if identifier > ceiling:
        _invalid()
    return ceiling, (value["t"], identifier)


def validate_params(operation: str, params: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any]
    filter_value: str | None
    if type(params) is not dict or operation not in CAPABILITIES:
        _invalid()
    if operation == "snapshots.compare":
        if params.keys() != {"target_pk", "older_id", "newer_id"}:
            _invalid()
        result = {
            "target_pk": _decimal(params["target_pk"]),
            "older_id": _decimal(params["older_id"], snapshot=True),
            "newer_id": _decimal(params["newer_id"], snapshot=True),
        }
        if result["older_id"] == result["newer_id"]:
            _invalid()
        return result
    required = (
        {"username"}
        if operation == "snapshots.targets"
        else ({"target_pk"} if operation == "snapshots.list" else set())
    )
    allowed = required | {"limit", "cursor"}
    if operation == "changes.list":
        allowed.add("target_pk")
    if not required <= params.keys() or not params.keys() <= allowed:
        _invalid()
    limit = params.get("limit", 50)
    if type(limit) is not int or not 1 <= limit <= 50:
        _invalid()
    if operation == "snapshots.targets":
        username = params["username"]
        if type(username) is not str:
            _invalid()
        # Same canonical form and ORDER as the CLI and watches.add (review gate
        # G12): leading "@" first, then whitespace, then lowercase; one GUI field
        # means one thing everywhere and the resource bounds are unchanged.
        filter_value = username.lstrip("@").strip().lower()
        if _USER.fullmatch(filter_value) is None or filter_value in {".", ".."}:
            _invalid()
        result = {"username": filter_value}
    else:
        filter_value = _decimal(params["target_pk"]) if "target_pk" in params else None
        result = {"target_pk": filter_value}
    cursor = (
        decode_cursor(params["cursor"], operation, filter_value) if "cursor" in params else None
    )
    return {**result, "limit": limit, "cursor": cursor}
