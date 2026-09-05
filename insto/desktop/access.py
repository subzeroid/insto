"""Explicit, bounded provider validation without ambient configuration."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Protocol

from insto._redact import register_secret
from insto.desktop.errors import DesktopError
from insto.exceptions import AuthInvalid, QuotaExhausted, RateLimited, Transient
from insto.models import Quota

VALIDATION_SECONDS = 30.0


class AccessBackend(Protocol):
    async def validate_access(self) -> Quota: ...

    async def aclose(self) -> None: ...


def validate_token(token: str) -> None:
    if (
        not isinstance(token, str)
        or not 4 <= len(token) <= 4096
        or any(not 33 <= ord(character) <= 126 for character in token)
    ):
        raise DesktopError("invalid_params")


def make_backend(token: str) -> AccessBackend:
    """Adapt the pinned SDK constructor without mutating process environment.

    SDK BaseAsyncClient currently reads proxy/CA settings and HIKERAPI_HOST.
    Use its BaseClient initializer with an explicit packaged host, then supply
    the same HTTP transport with environment lookup disabled. This deliberately
    narrow adapter is covered by an actual SDK construction regression.
    """
    import hikerapi
    import httpx
    from hikerapi.__version__ import __host__
    from hikerapi.base import BaseClient

    from insto.backends.hiker import HikerBackend

    class DesktopClient(hikerapi.AsyncClient):  # type: ignore[misc]
        def __init__(self) -> None:
            BaseClient.__init__(self, token=token, timeout=10.0, host=__host__)
            self._client = httpx.AsyncClient(
                base_url=self._url,
                headers=self._headers,
                timeout=10.0,
                trust_env=False,
                follow_redirects=False,
            )

    return HikerBackend(client=DesktopClient())


async def _close(backend: AccessBackend, deadline: float) -> None:
    worker = asyncio.create_task(backend.aclose())
    cancellation: asyncio.CancelledError | None = None
    while not worker.done():
        try:
            async with asyncio.timeout_at(deadline):
                await asyncio.shield(worker)
        except asyncio.CancelledError as exc:
            cancellation = exc
        except TimeoutError:
            worker.cancel()
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            with contextlib.suppress(BaseException):
                worker.result()
            raise DesktopError("operation_timeout") from None
    worker.result()
    if cancellation is not None:
        raise cancellation


async def validate_candidate(token: str) -> int:
    validate_token(token)
    register_secret(token)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + VALIDATION_SECONDS
    backend: AccessBackend | None = None
    failure: BaseException | None = None
    remaining: int | None = None
    try:
        # Reserve a small part of the same budget for closing the HTTP client.
        request_deadline = deadline - min(2.0, VALIDATION_SECONDS / 2)
        async with asyncio.timeout_at(request_deadline):
            backend = make_backend(token)
            quota = await backend.validate_access()
            remaining = quota.remaining
            if type(remaining) is not int or remaining < 0:
                raise DesktopError("access_unconfirmed")
    except BaseException as exc:
        failure = exc
    finally:
        if backend is not None:
            try:
                await _close(backend, deadline)
            except BaseException as exc:
                if failure is None or isinstance(exc, asyncio.CancelledError):
                    failure = exc
    if isinstance(failure, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
        raise failure
    if failure is not None:
        if isinstance(failure, DesktopError):
            raise failure from None
        code = (
            "invalid_token"
            if isinstance(failure, AuthInvalid)
            else "quota_exhausted"
            if isinstance(failure, QuotaExhausted)
            else "rate_limited"
            if isinstance(failure, RateLimited)
            else "network_error"
            if isinstance(failure, Transient)
            else "operation_timeout"
            if isinstance(failure, TimeoutError)
            else "access_unconfirmed"
        )
        raise DesktopError(code) from None
    assert remaining is not None
    return remaining
