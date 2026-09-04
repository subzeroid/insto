"""Tests for the watch webhook event and endpoint contracts."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from insto.exceptions import BackendError
from insto.service import watch_webhook
from insto.service.watch_webhook import (
    WebhookDeliveryError,
    WebhookNotifier,
    build_watch_event,
    validate_webhook_url,
)

ENDPOINT = "https://receiver.example/private-hook?token=endpoint-secret"
PAYLOAD: dict[str, Any] = {
    "schema_version": 1,
    "event": "watch.changed",
    "event_id": "evt-delivery-123",
    "username": "alice",
    "observed_at": "2026-09-04T18:30:45Z",
    "changes": {"biography": {"old": "old", "new": "new"}},
    "previous_usernames": ["old_alice"],
}


class GuardedStream(httpx.AsyncByteStream):
    """Response stream that records closure and fails if anyone reads it."""

    def __init__(self) -> None:
        self.read = False
        self.close_calls = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.read = True
        raise AssertionError("webhook response body must not be read")
        yield b"unreachable"

    async def aclose(self) -> None:
        self.close_calls += 1


class BlockingCloseStream(GuardedStream):
    """A response stream whose close can be released or cancelled by a test."""

    def __init__(self) -> None:
        super().__init__()
        self.close_started = asyncio.Event()
        self.allow_close = asyncio.Event()
        self.close_completed = False
        self.close_cancelled = False

    async def aclose(self) -> None:
        self.close_calls += 1
        self.close_started.set()
        try:
            await self.allow_close.wait()
        except asyncio.CancelledError:
            self.close_cancelled = True
            raise
        self.close_completed = True


class RealCloseProbeTransport(httpx.AsyncBaseTransport):
    """Exercise the real AsyncClient close state around a controllable transport."""

    def __init__(self, *, failure: RuntimeError | None = None) -> None:
        self.failure = failure
        self.close_calls = 0
        self.close_started = asyncio.Event()
        self.allow_close = asyncio.Event()
        self.close_completed = False
        self.close_cancelled = False

    async def handle_async_request(self, _request: httpx.Request) -> httpx.Response:
        raise AssertionError("this transport is only a client-close probe")

    async def aclose(self) -> None:
        self.close_calls += 1
        self.close_started.set()
        if self.failure is not None:
            raise self.failure
        try:
            await self.allow_close.wait()
        except asyncio.CancelledError:
            self.close_cancelled = True
            raise
        self.close_completed = True


async def _close_notifier(notifier: WebhookNotifier) -> None:
    await notifier.aclose()


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://receiver.example/hooks/watch?token=secret",
        "https://receiver.example:8443/hooks/watch",
        "https://receiver.example:65535/hooks/watch",
        "http://localhost/hooks/watch",
        "http://127.0.0.1/hooks/watch",
        "http://127.255.255.254/hooks/watch",
        "http://[::1]/hooks/watch",
    ],
)
def test_validate_webhook_url_accepts_secure_and_local_endpoints(endpoint: str) -> None:
    assert validate_webhook_url(endpoint) == endpoint


@pytest.mark.parametrize(
    ("endpoint", "reason"),
    [
        ("receiver.example/secret/path?token=shh", "absolute"),
        ("https:///secret/path?token=shh", "host"),
        ("https://receiver.example/secret/path?token=shh#private", "fragment"),
        ("ftp://receiver.example/secret/path?token=shh", "scheme"),
        ("http://receiver.example/secret/path?token=shh", "HTTPS"),
        ("http://192.0.2.1/secret/path?token=shh", "HTTPS"),
        ("https://receiver.example:65536/secret/path?token=shh", "port"),
    ],
)
def test_validate_webhook_url_rejects_safely(endpoint: str, reason: str) -> None:
    with pytest.raises(BackendError) as caught:
        validate_webhook_url(endpoint)

    message = str(caught.value)
    assert message.startswith("invalid watch webhook URL:")
    assert reason in message
    assert endpoint not in message
    assert "secret" not in message
    assert "token" not in message
    assert "shh" not in message


@pytest.mark.parametrize(
    "diff",
    [
        {"first_seen": True, "changes": {"biography": {"old": "", "new": "hi"}}},
        {"first_seen": False, "changes": {}},
        {"first_seen": False, "changes": {}, "previous_usernames": ["old_alice"]},
        {"first_seen": False, "previous_usernames": ["old_alice"]},
    ],
)
def test_build_watch_event_suppresses_non_changes(diff: dict[str, Any]) -> None:
    event = build_watch_event(
        "alice",
        diff,
        event_id="evt-suppressed",
        observed_at=datetime(2026, 9, 4, 18, 30, tzinfo=UTC),
    )

    assert event is None


def test_build_watch_event_returns_exact_versioned_payload() -> None:
    observed_at = datetime(
        2026,
        9,
        4,
        21,
        30,
        45,
        123456,
        tzinfo=timezone(timedelta(hours=3)),
    )
    diff = {
        "first_seen": False,
        "changes": {
            "biography": {"old": "old bio", "new": "new bio"},
            "is_verified": {"old": False, "new": True},
        },
        "previous_usernames": ["old_alice"],
        "ignored": "not part of the public contract",
    }

    event = build_watch_event(
        "alice",
        diff,
        event_id="evt-123",
        observed_at=observed_at,
    )

    assert event == {
        "schema_version": 1,
        "event": "watch.changed",
        "event_id": "evt-123",
        "username": "alice",
        "observed_at": "2026-09-04T18:30:45.123456Z",
        "changes": {
            "biography": {"old": "old bio", "new": "new bio"},
            "is_verified": {"old": False, "new": True},
        },
        "previous_usernames": ["old_alice"],
    }
    assert event is not None
    assert set(event) == {
        "schema_version",
        "event",
        "event_id",
        "username",
        "observed_at",
        "changes",
        "previous_usernames",
    }


def test_build_watch_event_copies_nested_mutable_values() -> None:
    nested_new = ["one"]
    changes = {"labels": {"old": [], "new": nested_new}}
    aliases = ["old_alice"]
    diff = {
        "first_seen": False,
        "changes": changes,
        "previous_usernames": aliases,
    }

    event = build_watch_event(
        "alice",
        diff,
        event_id="evt-copy",
        observed_at=datetime(2026, 9, 4, tzinfo=UTC),
    )
    assert event is not None

    nested_new.append("two")
    changes["labels"]["old"].append("mutated")
    aliases.append("older_alice")

    assert event["changes"] == {"labels": {"old": [], "new": ["one"]}}
    assert event["previous_usernames"] == ["old_alice"]


@pytest.mark.parametrize("status", range(200, 300))
async def test_notifier_accepts_every_2xx_class_status(status: int) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, stream=GuardedStream())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = WebhookNotifier(ENDPOINT, client=client)
    try:
        await notifier.send(PAYLOAD)
    finally:
        await _close_notifier(notifier)

    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert str(requests[0].url) == ENDPOINT
    assert json.loads(requests[0].content) == PAYLOAD


@pytest.mark.parametrize(
    ("failure", "expected_message"),
    [
        ("transport", "watch webhook delivery failed: transport error"),
        ("timeout", "watch webhook delivery failed: timeout"),
        (408, "watch webhook delivery failed: HTTP 408"),
        (429, "watch webhook delivery failed: HTTP 429"),
        (500, "watch webhook delivery failed: HTTP 500"),
        (503, "watch webhook delivery failed: HTTP 503"),
        (599, "watch webhook delivery failed: HTTP 599"),
    ],
)
async def test_notifier_retries_transient_failures_three_times(
    failure: str | int,
    expected_message: str,
) -> None:
    requests: list[dict[str, Any]] = []
    streams: list[GuardedStream] = []
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if failure == "transport":
            raise httpx.ConnectError(
                "raw failure at https://internal.example/private-secret",
                request=request,
            )
        if failure == "timeout":
            raise TimeoutError("raw failure at https://internal.example/private-secret")
        stream = GuardedStream()
        streams.append(stream)
        return httpx.Response(
            int(failure),
            stream=stream,
            headers={"x-private": "body-secret"},
        )

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = WebhookNotifier(ENDPOINT, client=client, sleep=record_sleep)
    try:
        with pytest.raises(WebhookDeliveryError) as caught:
            await notifier.send(PAYLOAD)
    finally:
        await _close_notifier(notifier)

    assert str(caught.value) == expected_message
    assert ENDPOINT not in str(caught.value)
    assert "private-secret" not in str(caught.value)
    assert "body-secret" not in str(caught.value)
    assert requests == [PAYLOAD, PAYLOAD, PAYLOAD]
    assert all(request["event_id"] == "evt-delivery-123" for request in requests)
    assert delays == [0.25, 1.0]
    assert all(not stream.read and stream.close_calls == 1 for stream in streams)


async def test_notifier_reports_native_httpx_timeout_as_timeout() -> None:
    attempts = 0
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("raw timeout details", request=request)

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = WebhookNotifier(ENDPOINT, client=client, sleep=record_sleep)
    try:
        with pytest.raises(WebhookDeliveryError) as caught:
            await notifier.send(PAYLOAD)
    finally:
        await _close_notifier(notifier)

    assert str(caught.value) == "watch webhook delivery failed: timeout"
    assert attempts == 3
    assert delays == [0.25, 1.0]


@pytest.mark.parametrize(
    ("outcomes", "expected_attempts", "expected_delays"),
    [
        (("transport", 503, 204), 3, [0.25, 1.0]),
        ((429, 201), 2, [0.25]),
    ],
)
async def test_notifier_stops_retrying_after_mixed_success(
    outcomes: tuple[str | int, ...],
    expected_attempts: int,
    expected_delays: list[float],
) -> None:
    pending = list(outcomes)
    requests: list[dict[str, Any]] = []
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        outcome = pending.pop(0)
        if outcome == "transport":
            raise httpx.ReadError("raw failure", request=request)
        return httpx.Response(outcome)

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = WebhookNotifier(ENDPOINT, client=client, sleep=record_sleep)
    try:
        await notifier.send(PAYLOAD)
    finally:
        await _close_notifier(notifier)

    assert requests == [PAYLOAD] * expected_attempts
    assert delays == expected_delays


async def test_notifier_freezes_payload_for_all_attempts() -> None:
    payload = {
        **PAYLOAD,
        "changes": {"labels": {"old": [], "new": ["one"]}},
    }
    expected = json.loads(json.dumps(payload))
    request_bodies: list[bytes] = []
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        request_bodies.append(request.content)
        return httpx.Response(503 if attempts == 1 else 204)

    async def mutate_payload(_delay: float) -> None:
        payload["event_id"] = "mutated-event-id"
        payload["changes"]["labels"]["new"].append("two")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = WebhookNotifier(ENDPOINT, client=client, sleep=mutate_payload)
    try:
        await notifier.send(payload)
    finally:
        await _close_notifier(notifier)

    assert [json.loads(body) for body in request_bodies] == [expected, expected]
    assert request_bodies[0] == request_bodies[1]


async def test_notifier_converts_unexpected_client_failure_safely() -> None:
    attempts = 0
    delays: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("raw endpoint-secret failure details")

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = WebhookNotifier(ENDPOINT, client=client, sleep=record_sleep)
    try:
        with pytest.raises(WebhookDeliveryError) as caught:
            await notifier.send(PAYLOAD)
    finally:
        await _close_notifier(notifier)

    assert str(caught.value) == "watch webhook delivery failed: unexpected error"
    assert "endpoint-secret" not in str(caught.value)
    assert attempts == 1
    assert delays == []


@pytest.mark.parametrize("status", [300, 301, 307, 308, 400, 401, 404, 409, 499])
async def test_notifier_fails_permanent_status_once(status: int) -> None:
    attempts = 0
    delays: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(status, content=b"private response body sentinel")

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = WebhookNotifier(ENDPOINT, client=client, sleep=record_sleep)
    try:
        with pytest.raises(WebhookDeliveryError) as caught:
            await notifier.send(PAYLOAD)
    finally:
        await _close_notifier(notifier)

    assert str(caught.value) == f"watch webhook delivery failed: HTTP {status}"
    assert "response body sentinel" not in str(caught.value)
    assert ENDPOINT not in str(caught.value)
    assert attempts == 1
    assert delays == []


async def test_notifier_does_not_follow_redirects() -> None:
    requested_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://redirect.example/private"})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    )
    notifier = WebhookNotifier(ENDPOINT, client=client)
    try:
        with pytest.raises(WebhookDeliveryError, match="HTTP 302"):
            await notifier.send(PAYLOAD)
    finally:
        await _close_notifier(notifier)

    assert requested_urls == [ENDPOINT]


async def test_notifier_default_client_disables_redirects_and_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructor_calls: list[dict[str, Any]] = []
    real_async_client = httpx.AsyncClient

    def client_factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        assert args == ()
        constructor_calls.append(kwargs)
        return real_async_client(**kwargs)

    monkeypatch.setattr(watch_webhook.httpx, "AsyncClient", client_factory)
    notifier = WebhookNotifier(ENDPOINT)
    await notifier.aclose()

    assert constructor_calls == [{"follow_redirects": False, "trust_env": False}]


async def test_notifier_enforces_hard_attempt_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    delays: list[float] = []

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(watch_webhook, "ATTEMPT_TIMEOUT_SECONDS", 0.01)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = WebhookNotifier(ENDPOINT, client=client, sleep=record_sleep)
    try:
        with pytest.raises(WebhookDeliveryError) as caught:
            await notifier.send(PAYLOAD)
    finally:
        await _close_notifier(notifier)

    assert str(caught.value) == "watch webhook delivery failed: timeout"
    assert attempts == 3
    assert delays == [0.25, 1.0]


async def test_notifier_closes_large_stream_without_reading_body() -> None:
    stream = GuardedStream()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-length": str(10 * 1024 * 1024)},
            stream=stream,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = WebhookNotifier(ENDPOINT, client=client)
    try:
        await notifier.send(PAYLOAD)
    finally:
        await _close_notifier(notifier)

    assert stream.read is False
    assert stream.close_calls == 1


async def test_cancellation_propagates_during_request() -> None:
    request_started = asyncio.Event()
    attempts = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        request_started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = WebhookNotifier(ENDPOINT, client=client)
    task = asyncio.create_task(notifier.send(PAYLOAD))
    await request_started.wait()
    task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await _close_notifier(notifier)

    assert attempts == 1


async def test_cancellation_after_headers_defers_response_cleanup_to_teardown() -> None:
    stream = BlockingCloseStream()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = WebhookNotifier(ENDPOINT, client=client)
    send_task = asyncio.create_task(notifier.send(PAYLOAD))
    await stream.close_started.wait()
    send_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(send_task, timeout=0.1)
    assert stream.close_cancelled is False

    teardown_task = asyncio.create_task(notifier.aclose())
    await asyncio.sleep(0)
    assert teardown_task.done() is False

    stream.allow_close.set()
    await asyncio.wait_for(teardown_task, timeout=0.1)
    assert stream.close_completed is True
    assert stream.close_calls == 1
    assert stream.read is False


async def test_teardown_bounds_stuck_response_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streams: list[BlockingCloseStream] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        stream = BlockingCloseStream()
        streams.append(stream)
        return httpx.Response(200, stream=stream)

    async def no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(watch_webhook, "ATTEMPT_TIMEOUT_SECONDS", 0.01)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = WebhookNotifier(ENDPOINT, client=client, sleep=no_sleep)

    with pytest.raises(WebhookDeliveryError, match="timeout"):
        await notifier.send(PAYLOAD)
    assert len(streams) == 3
    assert all(stream.close_cancelled is False for stream in streams)

    await asyncio.wait_for(notifier.aclose(), timeout=0.1)
    await asyncio.sleep(0)
    assert all(stream.close_cancelled is True for stream in streams)


async def test_teardown_awaits_cooperative_cancellation_before_client_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []

    class CooperativeCancellationStream(GuardedStream):
        async def aclose(self) -> None:
            self.close_calls += 1
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                order.append("response close cancelled")
                await asyncio.sleep(0)
                order.append("response cleanup complete")
                raise

    class OrderingTransport(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.requests = 0

        async def handle_async_request(self, _request: httpx.Request) -> httpx.Response:
            self.requests += 1
            if self.requests == 1:
                return httpx.Response(200, stream=CooperativeCancellationStream())
            return httpx.Response(400, stream=GuardedStream())

        async def aclose(self) -> None:
            order.append("client close")

    async def no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(watch_webhook, "ATTEMPT_TIMEOUT_SECONDS", 0.01)
    client = httpx.AsyncClient(transport=OrderingTransport())
    notifier = WebhookNotifier(ENDPOINT, client=client, sleep=no_sleep)

    with pytest.raises(WebhookDeliveryError, match="HTTP 400"):
        await notifier.send(PAYLOAD)
    await notifier.aclose()

    assert order == [
        "response close cancelled",
        "response cleanup complete",
        "client close",
    ]
    assert not notifier._response_close_tasks


async def test_teardown_does_not_wait_forever_for_stubborn_response_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StubbornCloseStream(GuardedStream):
        def __init__(self) -> None:
            super().__init__()
            self.allow_close = asyncio.Event()
            self.close_completed = asyncio.Event()
            self.cancellations = 0

        async def aclose(self) -> None:
            self.close_calls += 1
            while not self.allow_close.is_set():
                try:
                    await self.allow_close.wait()
                except asyncio.CancelledError:
                    self.cancellations += 1
            self.close_completed.set()

    stream = StubbornCloseStream()
    client_closed = asyncio.Event()

    class StubbornTransport(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.requests = 0

        async def handle_async_request(self, _request: httpx.Request) -> httpx.Response:
            self.requests += 1
            if self.requests == 1:
                return httpx.Response(200, stream=stream)
            return httpx.Response(400, stream=GuardedStream())

        async def aclose(self) -> None:
            client_closed.set()

    async def no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(watch_webhook, "ATTEMPT_TIMEOUT_SECONDS", 0.01)
    client = httpx.AsyncClient(transport=StubbornTransport())
    notifier = WebhookNotifier(ENDPOINT, client=client, sleep=no_sleep)

    with pytest.raises(WebhookDeliveryError, match="HTTP 400"):
        await notifier.send(PAYLOAD)
    await asyncio.wait_for(notifier.aclose(), timeout=0.1)

    assert client_closed.is_set()
    assert stream.cancellations >= 1
    assert not stream.close_completed.is_set()

    stream.allow_close.set()
    await asyncio.wait_for(stream.close_completed.wait(), timeout=0.1)
    await asyncio.sleep(0)
    assert not notifier._response_close_tasks


async def test_cancellation_propagates_during_retry_sleep() -> None:
    sleep_started = asyncio.Event()
    attempts = 0
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("raw failure", request=request)

    async def blocking_sleep(delay: float) -> None:
        delays.append(delay)
        sleep_started.set()
        await asyncio.Event().wait()

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = WebhookNotifier(ENDPOINT, client=client, sleep=blocking_sleep)
    task = asyncio.create_task(notifier.send(PAYLOAD))
    await sleep_started.wait()
    task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await _close_notifier(notifier)

    assert attempts == 1
    assert delays == [0.25]


async def test_notifier_closes_client_exactly_once() -> None:
    class CloseCountingClient(httpx.AsyncClient):
        def __init__(self) -> None:
            super().__init__(transport=httpx.MockTransport(lambda _request: httpx.Response(204)))
            self.close_calls = 0

        async def aclose(self) -> None:
            self.close_calls += 1
            await super().aclose()

    client = CloseCountingClient()
    notifier = WebhookNotifier(ENDPOINT, client=client)

    await notifier.aclose()
    await notifier.aclose()

    assert client.close_calls == 1


async def test_notifier_reuses_failed_real_client_close_result() -> None:
    failure = RuntimeError("transport close failed")
    transport = RealCloseProbeTransport(failure=failure)
    client = httpx.AsyncClient(transport=transport)
    notifier = WebhookNotifier(ENDPOINT, client=client)

    with pytest.raises(RuntimeError) as first:
        await notifier.aclose()
    with pytest.raises(RuntimeError) as second:
        await notifier.aclose()

    assert first.value is failure
    assert second.value is failure
    assert transport.close_calls == 1


async def test_caller_cancellation_does_not_cancel_real_client_close() -> None:
    transport = RealCloseProbeTransport()
    client = httpx.AsyncClient(transport=transport)
    notifier = WebhookNotifier(ENDPOINT, client=client)
    first_close = asyncio.create_task(notifier.aclose())
    await transport.close_started.wait()
    first_close.cancel()

    with pytest.raises(asyncio.CancelledError):
        await first_close
    assert transport.close_cancelled is False

    second_close = asyncio.create_task(notifier.aclose())
    await asyncio.sleep(0)
    assert second_close.done() is False
    transport.allow_close.set()
    await second_close

    assert transport.close_completed is True
    assert transport.close_calls == 1


async def test_concurrent_notifier_close_calls_share_real_client_close() -> None:
    transport = RealCloseProbeTransport()
    client = httpx.AsyncClient(transport=transport)
    notifier = WebhookNotifier(ENDPOINT, client=client)
    first_close = asyncio.create_task(notifier.aclose())
    await transport.close_started.wait()
    second_close = asyncio.create_task(notifier.aclose())
    await asyncio.sleep(0)

    assert transport.close_calls == 1
    transport.allow_close.set()
    await asyncio.gather(first_close, second_close)

    assert transport.close_completed is True
    assert transport.close_calls == 1
