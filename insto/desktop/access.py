"""Explicit, bounded provider validation without ambient configuration."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, Protocol, TypeVar

from insto._redact import register_secret
from insto.desktop.errors import DesktopError
from insto.exceptions import AuthInvalid, QuotaExhausted, RateLimited, Transient
from insto.models import Quota

VALIDATION_SECONDS = 30.0
_PENDING_WORKERS: set[asyncio.Task[Any]] = set()
_T = TypeVar("_T")


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


async def _await_worker(
    worker: asyncio.Task[_T], deadline: float, *, cancel_on_interrupt: bool = False
) -> _T:
    cancellation: asyncio.CancelledError | None = None
    while not worker.done():
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            # Cancellation cleanup cannot extend credential validation. Retain
            # the cancelled worker until it finishes and consume its exception;
            # the one-shot process owner's deadline is the final hard bound if
            # a third-party close ignores cancellation. No config is committed.
            _PENDING_WORKERS.add(worker)

            def finished(task: asyncio.Task[_T]) -> None:
                _PENDING_WORKERS.discard(task)
                with contextlib.suppress(BaseException):
                    task.result()

            worker.add_done_callback(finished)
            worker.cancel()
            if cancellation is not None:
                raise cancellation
            raise DesktopError("operation_timeout")
        try:
            await asyncio.wait({worker}, timeout=remaining)
        except asyncio.CancelledError as exc:
            if cancellation is None and cancel_on_interrupt:
                worker.cancel()
            cancellation = exc
    if cancellation is not None:
        with contextlib.suppress(BaseException):
            worker.result()
        raise cancellation
    return worker.result()


async def _close(backend: AccessBackend, deadline: float) -> None:
    await _await_worker(asyncio.create_task(backend.aclose()), deadline)


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
        backend = make_backend(token)
        quota = await _await_worker(
            asyncio.create_task(backend.validate_access()),
            request_deadline,
            cancel_on_interrupt=True,
        )
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
