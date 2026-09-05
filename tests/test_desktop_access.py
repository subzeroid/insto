"""Candidate validation never trusts ambient credentials or leaks diagnostics."""

import asyncio
import os
from unittest.mock import AsyncMock

import pytest

from insto._redact import redact_secrets
from insto.desktop import access
from insto.desktop.errors import DesktopError
from insto.exceptions import (
    AuthInvalid,
    BackendError,
    QuotaExhausted,
    RateLimited,
    SchemaDrift,
    Transient,
)
from insto.models import Quota


@pytest.mark.asyncio
@pytest.mark.parametrize("remaining", [0, 15])
async def test_valid_candidate_is_registered_before_constructor_and_closed(monkeypatch, remaining):
    backend = AsyncMock()
    backend.validate_access.return_value = Quota.with_remaining(remaining)

    def construct(token):
        assert token not in redact_secrets(token)
        return backend

    monkeypatch.setattr(access, "make_backend", construct)
    assert await access.validate_candidate("offline-secret-candidate") == remaining
    backend.aclose.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exception,code",
    [
        (AuthInvalid("candidate-secret"), "invalid_token"),
        (QuotaExhausted("candidate-secret"), "quota_exhausted"),
        (RateLimited(2, "candidate-secret"), "rate_limited"),
        (Transient("candidate-secret"), "network_error"),
        (SchemaDrift("candidate-secret", "unknown"), "access_unconfirmed"),
        (BackendError("candidate-secret"), "access_unconfirmed"),
        (RuntimeError("candidate-secret"), "access_unconfirmed"),
    ],
)
async def test_failed_access_preserves_safe_code_and_closes(monkeypatch, exception, code):
    backend = AsyncMock()
    backend.validate_access.side_effect = exception
    monkeypatch.setattr(access, "make_backend", lambda token: backend)
    with pytest.raises(DesktopError) as raised:
        await access.validate_candidate("candidate-secret")
    assert raised.value.code == code
    assert "candidate-secret" not in str(raised.value)
    backend.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_constructor_failure_is_safe(monkeypatch):
    def broken(token):
        raise RuntimeError(token)

    monkeypatch.setattr(access, "make_backend", broken)
    with pytest.raises(DesktopError, match="access_unconfirmed"):
        await access.validate_candidate("candidate-secret")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "token", ["", "abc", " has-spaces ", "line\nbreak", "токен", "x" * 4097, 4, None]
)
async def test_invalid_token_never_constructs(monkeypatch, token):
    constructor = AsyncMock()
    monkeypatch.setattr(access, "make_backend", constructor)
    with pytest.raises(DesktopError, match="invalid_params"):
        await access.validate_candidate(token)
    constructor.assert_not_called()


@pytest.mark.asyncio
async def test_timeout_closes_once(monkeypatch):
    backend = AsyncMock()
    backend.validate_access.side_effect = lambda: None

    async def slow():
        await asyncio.sleep(5)

    backend.validate_access.side_effect = slow
    monkeypatch.setattr(access, "make_backend", lambda token: backend)
    monkeypatch.setattr(access, "VALIDATION_SECONDS", 0.03)
    with pytest.raises(DesktopError, match="operation_timeout"):
        await access.validate_candidate("candidate-secret")
    backend.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_repeated_cancel_drains_close_once(monkeypatch):
    started, closing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    backend = AsyncMock()

    async def validate():
        started.set()
        await asyncio.Event().wait()

    async def close():
        closing.set()
        await release.wait()

    backend.validate_access.side_effect = validate
    backend.aclose.side_effect = close
    monkeypatch.setattr(access, "make_backend", lambda token: backend)
    task = asyncio.create_task(access.validate_candidate("candidate-secret"))
    await started.wait()
    task.cancel()
    await closing.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    backend.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_sdk_transport_ignores_poisoned_environment(monkeypatch):
    from insto.backends.hiker import HikerBackend

    poisoned = {
        "INSTO_BACKEND": "fake",
        "HIKERAPI_HOST": "untrusted.invalid",
        "HTTP_PROXY": "not-a-url",
        "HTTPS_PROXY": "not-a-url",
        "ALL_PROXY": "not-a-url",
        "SSL_CERT_FILE": "/missing/ca.pem",
        "HIKERAPI_TOKEN": "ambient-not-candidate",
    }
    for key, value in poisoned.items():
        monkeypatch.setenv(key, value)
    original = dict(os.environ)
    backend = access.make_backend("candidate-secret")
    try:
        assert isinstance(backend, HikerBackend)
        assert backend._client._client.trust_env is False
        assert "untrusted.invalid" not in str(backend._client._client.base_url)
        assert backend._client._client.headers["x-access-key"] == "candidate-secret"
        assert dict(os.environ) == original
    finally:
        await backend.aclose()


@pytest.mark.asyncio
async def test_close_itself_is_inside_validation_budget(monkeypatch):
    backend = AsyncMock()
    backend.validate_access.return_value = Quota.with_remaining(10)

    async def close():
        await asyncio.sleep(5)

    backend.aclose.side_effect = close
    monkeypatch.setattr(access, "make_backend", lambda token: backend)
    monkeypatch.setattr(access, "VALIDATION_SECONDS", 0.03)
    with pytest.raises(DesktopError, match="operation_timeout"):
        await asyncio.wait_for(access.validate_candidate("candidate-secret"), timeout=1)
    backend.aclose.assert_awaited_once()
