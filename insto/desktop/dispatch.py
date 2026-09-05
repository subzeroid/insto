"""The capability allowlist and safe desktop response boundary."""

from __future__ import annotations

import time
from typing import Any

from insto import __version__
from insto.desktop.errors import MESSAGES as DOMAIN_MESSAGES
from insto.desktop.errors import DesktopError
from insto.desktop.history_params import CAPABILITIES as HISTORY_CAPABILITIES
from insto.desktop.history_params import validate_params as validate_history_params
from insto.desktop.protocol import PROTOCOL_VERSION, ProtocolError, Request, decode, encode
from insto.desktop.watch_params import CAPABILITIES as WATCH_CAPABILITIES
from insto.desktop.watch_params import validate_params as validate_watch_params
from insto.service.history import _SCHEMA_VERSION

_MESSAGES = {
    "invalid_request": "Invalid desktop request.",
    "unsupported_protocol": "Unsupported desktop protocol.",
    "unsupported_operation": "Unsupported desktop operation.",
    "invalid_params": "Invalid operation parameters.",
    "internal_error": "Desktop operation failed.",
}

CAPABILITIES = (
    "hello",
    "setup.inspect",
    "setup.configure",
    "settings.inspect",
    "credentials.replace",
    "service.start",
    "service.stop",
    "service.repair",
    *WATCH_CAPABILITIES,
    *HISTORY_CAPABILITIES,
)


async def dispatch(request: Request) -> dict[str, Any]:
    """Validate the exact allowlist before lazily importing operation code."""
    deadline = time.monotonic() + 10.0
    operation = request.operation
    if operation not in CAPABILITIES:
        raise ProtocolError("unsupported_operation", request.request_id)
    if operation in WATCH_CAPABILITIES:
        params = validate_watch_params(operation, request.params)
        from insto.desktop import watches
        from insto.desktop.profile import Profile

        profile = Profile.from_environment()
        if operation == "overview":
            return await watches.overview(profile, deadline=deadline)
        return watches.run(profile, operation, params, deadline=deadline)
    if operation in HISTORY_CAPABILITIES:
        params = validate_history_params(operation, request.params)
        from insto.desktop import history
        from insto.desktop.profile import Profile

        profile = Profile.from_environment()
        return history.run(profile, operation, params, deadline=deadline)
    token_operation = operation in {"setup.configure", "credentials.replace"}
    if token_operation:
        if request.params.keys() != {"token"}:
            raise ProtocolError("invalid_params", request.request_id)
        from insto.desktop.access import validate_token

        validate_token(request.params["token"])
    elif request.params:
        raise ProtocolError("invalid_params", request.request_id)
    if operation == "hello":
        return {
            "core_version": __version__,
            "schema_version_supported": _SCHEMA_VERSION,
            "capabilities": list(CAPABILITIES),
        }

    from insto.desktop import operations
    from insto.desktop.profile import Profile

    profile = Profile.from_environment()
    if operation in {"setup.inspect", "settings.inspect"}:
        return await operations.inspect_profile(profile)
    if operation == "setup.configure":
        return await operations.configure(profile, request.params["token"])
    if operation == "credentials.replace":
        return await operations.replace_credentials(profile, request.params["token"])
    return await operations.change_service(profile, operation.removeprefix("service."))


async def handle(raw: bytes) -> bytes:
    """Contain protocol and operation failures without reflecting their details."""
    request_id: str | None = None
    retryable = False
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
        message = _MESSAGES[code]
        if request_id is None and code == "unsupported_protocol":
            request_id = error.request_id
    except DesktopError as error:
        code = error.code
        message, retryable = DOMAIN_MESSAGES[code]
    except Exception:
        code = "internal_error"
        message = _MESSAGES[code]
    return encode(
        {
            "protocol_version": PROTOCOL_VERSION,
            "request_id": request_id,
            "error": {"code": code, "message": message, "retryable": retryable},
        }
    )
