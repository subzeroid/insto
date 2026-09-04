"""Validation and versioned payload construction for watch webhooks."""

from __future__ import annotations

import asyncio
import copy
import ipaddress
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from insto.exceptions import BackendError

ATTEMPT_TIMEOUT_SECONDS = 5.0
_RETRY_DELAYS = (0.25, 1.0)
SleepFn = Callable[[float], Awaitable[None]]


class WebhookDeliveryError(BackendError):
    """A safe, endpoint-independent webhook delivery failure."""


class WebhookNotifier:
    """Deliver watch events through one reusable streaming HTTP client."""

    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.AsyncClient | None = None,
        sleep: SleepFn = asyncio.sleep,
    ) -> None:
        self._endpoint = endpoint
        self._client = (
            client
            if client is not None
            else httpx.AsyncClient(follow_redirects=False, trust_env=False)
        )
        self._sleep = sleep
        self._closed = False
        self._client_close_task: asyncio.Task[None] | None = None
        self._response_close_tasks: set[asyncio.Task[None]] = set()

    async def send(self, payload: dict[str, Any]) -> None:
        """POST one event, retrying only controlled transient failures."""
        delivery_payload = copy.deepcopy(payload)
        failure = "transport error"
        for attempt in range(3):
            try:
                request = self._client.build_request("POST", self._endpoint, json=delivery_payload)
                async with asyncio.timeout(ATTEMPT_TIMEOUT_SECONDS):
                    response = await self._client.send(
                        request,
                        stream=True,
                        follow_redirects=False,
                    )
                    try:
                        status = response.status_code
                    finally:
                        await self._close_response(response)
            except (httpx.TimeoutException, TimeoutError):
                failure = "timeout"
            except httpx.TransportError:
                failure = "transport error"
            except Exception:
                raise WebhookDeliveryError(
                    "watch webhook delivery failed: unexpected error"
                ) from None
            else:
                failure = f"HTTP {status}"
                if 200 <= status < 300:
                    return
                if not (status in {408, 429} or 500 <= status < 600):
                    raise WebhookDeliveryError(f"watch webhook delivery failed: {failure}")

            if attempt < len(_RETRY_DELAYS):
                await self._sleep(_RETRY_DELAYS[attempt])

        raise WebhookDeliveryError(f"watch webhook delivery failed: {failure}")

    async def aclose(self) -> None:
        """Close the reusable client once."""
        if self._closed:
            return
        close_task = self._client_close_task
        if close_task is None:
            close_task = asyncio.create_task(
                self._close_client(),
                name="insto-webhook-client-close",
            )
            close_task.add_done_callback(self._client_close_done)
            self._client_close_task = close_task
        await asyncio.shield(close_task)

    def _client_close_done(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()

    async def _close_client(self) -> None:
        await self._drain_response_closes()
        await self._client.aclose()
        self._closed = True

    async def _close_response(self, response: httpx.Response) -> None:
        close_task = asyncio.create_task(
            response.aclose(),
            name="insto-webhook-response-close",
        )
        self._response_close_tasks.add(close_task)
        close_task.add_done_callback(self._response_close_done)
        await asyncio.shield(close_task)

    def _response_close_done(self, task: asyncio.Task[None]) -> None:
        self._response_close_tasks.discard(task)
        if not task.cancelled():
            task.exception()

    async def _drain_response_closes(self) -> None:
        tasks = set(self._response_close_tasks)
        if not tasks:
            return
        done, pending = await asyncio.wait(tasks, timeout=ATTEMPT_TIMEOUT_SECONDS)
        self._response_close_tasks.difference_update(done)
        for task in pending:
            task.cancel()
        if pending:
            cancelled, _ = await asyncio.wait(pending, timeout=ATTEMPT_TIMEOUT_SECONDS)
            self._response_close_tasks.difference_update(cancelled)


def validate_webhook_url(value: str) -> str:
    """Return an accepted webhook URL without exposing rejected URL contents."""
    try:
        endpoint = httpx.URL(value)
        port = endpoint.port
    except (httpx.InvalidURL, TypeError):
        raise BackendError("invalid watch webhook URL: malformed URL") from None

    if not endpoint.scheme:
        raise BackendError("invalid watch webhook URL: absolute URL required")
    if not endpoint.host:
        raise BackendError("invalid watch webhook URL: host required")
    if "#" in value:
        raise BackendError("invalid watch webhook URL: fragments are not allowed")
    if port is not None and not 1 <= port <= 65535:
        raise BackendError("invalid watch webhook URL: port is outside the valid range")
    if endpoint.scheme not in {"http", "https"}:
        raise BackendError("invalid watch webhook URL: unsupported scheme")
    if endpoint.scheme == "https":
        return value

    host = endpoint.host.lower()
    if host == "localhost":
        return value
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and address.is_loopback:
        return value
    raise BackendError("invalid watch webhook URL: HTTPS required for non-local endpoints")


def build_watch_event(
    username: str,
    diff: dict[str, Any],
    *,
    event_id: str,
    observed_at: datetime,
) -> dict[str, Any] | None:
    """Convert a current watch change into the stable version-1 event shape."""
    if diff.get("first_seen") or not diff.get("changes"):
        return None
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")

    observed_utc = observed_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return {
        "schema_version": 1,
        "event": "watch.changed",
        "event_id": event_id,
        "username": username,
        "observed_at": observed_utc,
        "changes": copy.deepcopy(diff["changes"]),
        "previous_usernames": copy.deepcopy(diff.get("previous_usernames") or []),
    }


__all__ = [
    "WebhookDeliveryError",
    "WebhookNotifier",
    "build_watch_event",
    "validate_webhook_url",
]
