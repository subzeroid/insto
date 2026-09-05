"""Strict access validation over the real SDK transport, without network access."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import hikerapi
import httpx
import pytest

from insto.backends.hiker import HikerBackend
from insto.exceptions import (
    AuthInvalid,
    BackendError,
    QuotaExhausted,
    RateLimited,
    SchemaDrift,
    Transient,
)
from insto.models import Quota
from tests.test_hiker_backend import _no_retry


@asynccontextmanager
async def _backend(
    handler: Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]],
) -> AsyncIterator[HikerBackend]:
    sdk = hikerapi.AsyncClient(token="test-credential", timeout=1)
    await sdk._client.aclose()
    sdk._client = httpx.AsyncClient(
        base_url=sdk._url,
        headers=sdk._headers,
        transport=httpx.MockTransport(handler),
    )
    backend = HikerBackend(client=sdk, retry_decorator=_no_retry())
    try:
        yield backend
    finally:
        await backend.aclose()
        assert sdk._client.is_closed


@pytest.mark.parametrize("remaining", [0, 1, 100])
async def test_validate_access_returns_and_caches_quota(remaining: int) -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        assert request.url.path == "/sys/balance"
        assert not request.url.query
        assert request.headers["x-access-key"] == "test-credential"
        return httpx.Response(200, json={"requests": remaining})

    async with _backend(handler) as backend:
        quota = await backend.validate_access()
        assert isinstance(quota, Quota)
        assert quota == Quota.with_remaining(remaining)
        assert backend.get_quota() is quota
        assert backend.get_last_error() is None
        assert len(requests) == 1


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        None,
        {"rate": 1},
        {"requests": None},
        {"requests": True},
        {"requests": False},
        {"requests": -1},
        {"requests": "1"},
        {"requests": 1.0},
    ],
)
async def test_validate_access_rejects_invalid_balance(payload: object) -> None:
    async with _backend(lambda _: httpx.Response(200, content=json.dumps(payload))) as backend:
        with pytest.raises(SchemaDrift) as caught:
            await backend.validate_access()
        assert caught.value.endpoint == "/sys/balance"
        assert caught.value.missing_field == "nonnegative integer requests"
        assert backend.get_last_error() is caught.value
        assert backend.get_schema_drift_count() == 1
        assert backend.get_quota() == Quota.unknown()


@pytest.mark.parametrize(
    "body,field", [(b"null", "nonnegative integer requests"), (b"not-json", "valid JSON object")]
)
async def test_validate_access_rejects_raw_invalid_balance(body: bytes, field: str) -> None:
    async with _backend(lambda _: httpx.Response(200, content=body)) as backend:
        with pytest.raises(SchemaDrift) as caught:
            await backend.validate_access()
        assert caught.value.missing_field == field
        assert backend.get_last_error() is caught.value
        assert backend.get_schema_drift_count() == 1


@pytest.mark.parametrize(
    "status,error_type",
    [
        (401, AuthInvalid),
        (402, QuotaExhausted),
        (429, RateLimited),
        (500, Transient),
        (503, Transient),
    ],
)
async def test_validate_access_uses_http_taxonomy(
    status: int, error_type: type[BackendError]
) -> None:
    async with _backend(lambda _: httpx.Response(status)) as backend:
        with pytest.raises(error_type) as caught:
            await backend.validate_access()
        assert backend.get_last_error() is caught.value


@pytest.mark.parametrize("status", [403, 404])
async def test_balance_lookup_errors_are_plain_backend_errors(status: int) -> None:
    async with _backend(
        lambda _: httpx.Response(status, text="SENTINEL-SENSITIVE-BODY")
    ) as backend:
        with pytest.raises(BackendError) as caught:
            await backend.validate_access()
        assert type(caught.value) is BackendError
        assert str(caught.value) == "HikerAPI access check could not confirm access"
        assert "SENTINEL" not in str(caught.value)
        assert caught.value.__cause__ is None
        assert caught.value.__suppress_context__
        assert backend.get_last_error() is caught.value


@pytest.mark.parametrize("status", [301, 302, 307, 308])
async def test_validate_access_rejects_redirect_balance(status: int) -> None:
    async with _backend(lambda _: httpx.Response(status, json={"requests": 10})) as backend:
        with pytest.raises(BackendError) as caught:
            await backend.validate_access()
        assert type(caught.value) is BackendError
        assert str(caught.value) == f"unexpected HikerAPI status {status}"
        assert backend.get_last_error() is caught.value
        assert backend.get_quota() == Quota.unknown()


@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ReadTimeout])
async def test_validate_access_network_failure_is_transient(
    error_type: type[httpx.RequestError],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error_type("unavailable", request=request)

    async with _backend(handler) as backend:
        with pytest.raises(Transient) as caught:
            await backend.validate_access()
        assert not isinstance(caught.value, AuthInvalid)
        assert backend.get_last_error() is caught.value


async def test_refresh_quota_remains_soft() -> None:
    async with _backend(lambda _: httpx.Response(401)) as backend:
        assert await backend.refresh_quota() == Quota.unknown()


async def test_validate_access_recovers_and_preserves_error_history() -> None:
    responses = iter([httpx.Response(401), httpx.Response(200, json={"requests": 100})])
    async with _backend(lambda _: next(responses)) as backend:
        with pytest.raises(AuthInvalid) as caught:
            await backend.validate_access()
        assert await backend.validate_access() == Quota.with_remaining(100)
        assert backend.get_last_error() is caught.value


async def test_validate_access_propagates_cancellation() -> None:
    started = asyncio.Event()

    async def handler(_: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("cancelled request unexpectedly resumed")

    async with _backend(handler) as backend:
        task = asyncio.create_task(backend.validate_access())
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert backend.get_quota() == Quota.unknown()
            assert backend.get_last_error() is None
        finally:
            if not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
