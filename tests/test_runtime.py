"""Tests for shared one-shot, REPL, and daemon resource construction."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest

from insto.config import Config
from insto.exceptions import BackendError, Transient
from insto.models import Profile
from insto.service import runtime as runtime_service
from insto.service.history import HistoryStore
from insto.service.runtime import RuntimeRole, open_runtime
from insto.service.watch_lock import WatchLockBusyError, WatchProcessLock
from insto.service.watch_webhook import WebhookDeliveryError
from tests.fakes import FakeBackend


class ClosingBackend(FakeBackend):
    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self.events = events

    async def aclose(self) -> None:
        self.events.append("backend")


class FailingBackend(ClosingBackend):
    async def resolve_target(self, username: str) -> str:
        raise Transient(f"backend unavailable for {username}")


class ClosingClient(httpx.AsyncClient):
    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self.events = events

    async def aclose(self) -> None:
        self.events.append("cdn")
        await super().aclose()


class RecordingNotifier:
    def __init__(
        self,
        events: list[str] | None = None,
        *,
        send_error: Exception | None = None,
        close_error: Exception | None = None,
        on_send: Callable[[dict[str, Any]], None] | None = None,
        on_close: Callable[[], None] | None = None,
    ) -> None:
        self.events = events
        self.send_error = send_error
        self.close_error = close_error
        self.on_send = on_send
        self.on_close = on_close
        self.payloads: list[dict[str, Any]] = []
        self.close_calls = 0

    async def send(self, payload: dict[str, Any]) -> None:
        self.payloads.append(payload)
        if self.on_send is not None:
            self.on_send(payload)
        if self.events is not None:
            self.events.append("delivery")
        if self.send_error is not None:
            raise self.send_error

    async def aclose(self) -> None:
        self.close_calls += 1
        if self.on_close is not None:
            self.on_close()
        if self.events is not None:
            self.events.append("webhook")
        if self.close_error is not None:
            raise self.close_error


class BlockingCloseNotifier(RecordingNotifier):
    def __init__(self, events: list[str]) -> None:
        super().__init__(events)
        self.close_started = asyncio.Event()
        self.allow_close = asyncio.Event()

    async def aclose(self) -> None:
        self.close_calls += 1
        self.close_started.set()
        await self.allow_close.wait()
        if self.events is not None:
            self.events.append("webhook")


def _config(tmp_path: Path, *, webhook_url: str | None = None) -> Config:
    return Config(
        output_dir=tmp_path / "out",
        db_path=tmp_path / "store.db",
        watch_webhook_url=webhook_url,
    )


def _seed_watch(config: Config, *, profile: Profile | None = None) -> None:
    history = HistoryStore(config.db_path)
    try:
        assert history.register_watch("alice", 300).spec is not None
        if profile is not None:
            history.add_snapshot(history.snapshot_from_profile(profile, post_pks=[]))
    finally:
        history.close()


def _assert_successful_state(state: Any) -> None:
    assert state.status == "active"
    assert state.last_ok is not None
    assert state.consecutive_errors == 0
    assert state.last_error is None


async def test_one_shot_runtime_never_acquires_executor_and_cleans_up(tmp_path: Path) -> None:
    config = _config(tmp_path)
    events: list[str] = []
    backend = ClosingBackend(events)
    client = ClosingClient(events)
    history: HistoryStore | None = None

    async with open_runtime(
        config,
        role="oneshot",
        backend_factory=lambda _: backend,
        cdn_client_factory=lambda _: client,
    ) as runtime:
        history = runtime.history
        assert runtime.role == "oneshot"
        assert runtime.coordinator is None
        assert runtime.facade.watches is runtime.manager
        assert runtime.manager.executor_acquired is False
        assert runtime.facade.watch_role == "oneshot"

    assert events == ["cdn", "backend"]
    assert history is not None
    with pytest.raises(sqlite3.ProgrammingError):
        history.schema_version()


async def test_one_shot_runtime_ignores_invalid_webhook_without_allocating(
    tmp_path: Path,
) -> None:
    config = _config(
        tmp_path,
        webhook_url="http://receiver.example/endpoint-secret",
    )
    factory_calls: list[str] = []

    def notifier_factory(endpoint: str) -> RecordingNotifier:
        factory_calls.append(endpoint)
        raise AssertionError("one-shot mode must not allocate a webhook notifier")

    async with open_runtime(
        config,
        role="oneshot",
        backend_factory=lambda _: ClosingBackend([]),
        cdn_client_factory=lambda _: ClosingClient([]),
        webhook_notifier_factory=notifier_factory,
    ) as runtime:
        assert runtime.webhook_notifier is None

    assert factory_calls == []


@pytest.mark.parametrize("role", ["repl", "daemon"])
async def test_disabled_long_running_runtime_has_no_webhook_notifier(
    tmp_path: Path,
    role: RuntimeRole,
) -> None:
    factory_calls: list[str] = []

    def notifier_factory(endpoint: str) -> RecordingNotifier:
        factory_calls.append(endpoint)
        raise AssertionError("disabled webhook must not allocate a notifier")

    async with open_runtime(
        _config(tmp_path),
        role=role,
        backend_factory=lambda _: ClosingBackend([]),
        cdn_client_factory=lambda _: ClosingClient([]),
        webhook_notifier_factory=notifier_factory,
    ) as runtime:
        assert runtime.webhook_notifier is None

    assert factory_calls == []


@pytest.mark.parametrize("role", ["repl", "daemon"])
async def test_configured_long_running_runtime_constructs_one_notifier_before_lock(
    tmp_path: Path,
    role: RuntimeRole,
) -> None:
    endpoint = "https://receiver.example/endpoint-secret"
    config = _config(tmp_path, webhook_url=endpoint)
    if role == "daemon":
        _seed_watch(config)
    notifier = RecordingNotifier()
    factory_calls: list[str] = []

    def notifier_factory(value: str) -> RecordingNotifier:
        factory_calls.append(value)
        probe = WatchProcessLock(config.db_path)
        probe.acquire()
        probe.release()
        return notifier

    async with open_runtime(
        config,
        role=role,
        backend_factory=lambda _: ClosingBackend([]),
        cdn_client_factory=lambda _: ClosingClient([]),
        webhook_notifier_factory=notifier_factory,
    ) as runtime:
        assert runtime.webhook_notifier is notifier
        if role == "daemon":
            assert runtime.manager.executor_acquired is True

    assert factory_calls == [endpoint]
    assert notifier.close_calls == 1


@pytest.mark.parametrize("role", ["repl", "daemon"])
async def test_invalid_webhook_url_fails_without_factory_or_endpoint_disclosure(
    tmp_path: Path,
    role: RuntimeRole,
) -> None:
    endpoint = "http://receiver.example/endpoint-secret?token=query-secret"
    config = _config(tmp_path, webhook_url=endpoint)
    factory_calls: list[str] = []

    def notifier_factory(value: str) -> RecordingNotifier:
        factory_calls.append(value)
        return RecordingNotifier()

    with pytest.raises(BackendError) as caught:
        async with open_runtime(
            config,
            role=role,
            backend_factory=lambda _: ClosingBackend([]),
            cdn_client_factory=lambda _: ClosingClient([]),
            webhook_notifier_factory=notifier_factory,
        ):
            pytest.fail("runtime yielded with an invalid webhook URL")

    assert factory_calls == []
    message = str(caught.value)
    assert "HTTPS required" in message
    assert "endpoint-secret" not in message
    assert "query-secret" not in message


async def test_daemon_runtime_acquires_before_yield_and_releases_last(tmp_path: Path) -> None:
    config = _config(tmp_path)
    seed = HistoryStore(config.db_path)
    try:
        assert seed.register_watch("alice", 300).spec is not None
    finally:
        seed.close()

    events: list[str] = []
    backend = ClosingBackend(events)
    client = ClosingClient(events)
    runtime_manager = None
    async with open_runtime(
        config,
        role="daemon",
        backend_factory=lambda _: backend,
        cdn_client_factory=lambda _: client,
    ) as runtime:
        runtime_manager = runtime.manager
        assert runtime.coordinator is not None
        assert runtime.facade.watches is runtime.manager
        assert runtime.manager.executor_acquired is True
        assert [spec.user for spec in runtime.manager.list()] == ["alice"]

    assert events == ["cdn", "backend"]
    assert runtime_manager is not None and runtime_manager.executor_acquired is False
    probe = WatchProcessLock(config.db_path)
    probe.acquire()
    probe.release()


async def test_repl_runtime_claims_lazily_when_registration_appears(tmp_path: Path) -> None:
    config = _config(tmp_path)
    events: list[str] = []
    async with open_runtime(
        config,
        role="repl",
        backend_factory=lambda _: ClosingBackend(events),
        cdn_client_factory=lambda _: ClosingClient(events),
    ) as runtime:
        assert runtime.coordinator is not None
        assert runtime.manager.executor_acquired is False
        assert runtime.history.register_watch("alice", 300).spec is not None
        runtime.coordinator.request_reconcile()

        for _ in range(20):
            if runtime.manager.executor_acquired:
                break
            await asyncio.sleep(0.01)
        assert runtime.manager.executor_acquired is True
        assert [spec.user for spec in runtime.manager.list()] == ["alice"]

    assert events == ["cdn", "backend"]


async def test_partial_construction_failure_closes_backend(tmp_path: Path) -> None:
    config = _config(tmp_path)
    events: list[str] = []
    backend = ClosingBackend(events)

    def broken_client(_: Config) -> httpx.AsyncClient:
        raise RuntimeError("cdn init failed")

    with pytest.raises(RuntimeError, match="cdn init failed"):
        async with open_runtime(
            config,
            role="oneshot",
            backend_factory=lambda _: backend,
            cdn_client_factory=broken_client,
        ):
            pytest.fail("runtime yielded after construction failure")

    assert events == ["backend"]
    reopened = HistoryStore(config.db_path)
    reopened.close()


async def test_partial_daemon_start_failure_closes_notifier_and_existing_resources(
    tmp_path: Path,
) -> None:
    endpoint = "https://receiver.example/endpoint-secret"
    config = _config(tmp_path, webhook_url=endpoint)
    _seed_watch(config)
    events: list[str] = []
    notifier = RecordingNotifier(events)
    external_lock = WatchProcessLock(config.db_path)
    external_lock.acquire()
    try:
        with pytest.raises(WatchLockBusyError):
            async with open_runtime(
                config,
                role="daemon",
                backend_factory=lambda _: ClosingBackend(events),
                cdn_client_factory=lambda _: ClosingClient(events),
                webhook_notifier_factory=lambda _: notifier,
            ):
                pytest.fail("runtime yielded while the executor lock was held")
    finally:
        external_lock.release()

    assert events == ["webhook", "cdn", "backend"]
    assert notifier.close_calls == 1


async def test_notifier_factory_failure_closes_existing_resources_without_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, webhook_url="https://receiver.example/endpoint-secret")
    events: list[str] = []
    backend = ClosingBackend(events)
    client = ClosingClient(events)
    history_instances: list[HistoryStore] = []
    factory_calls = 0
    history_type = HistoryStore

    def history_factory(path: Path) -> HistoryStore:
        history = history_type(path)
        history_instances.append(history)
        return history

    def broken_notifier_factory(_: str) -> RecordingNotifier:
        nonlocal factory_calls
        factory_calls += 1
        raise RuntimeError("notifier init failed")

    monkeypatch.setattr(runtime_service, "HistoryStore", history_factory)
    with pytest.raises(RuntimeError, match="notifier init failed"):
        async with open_runtime(
            config,
            role="daemon",
            backend_factory=lambda _: backend,
            cdn_client_factory=lambda _: client,
            webhook_notifier_factory=broken_notifier_factory,
        ):
            pytest.fail("runtime yielded after notifier construction failure")

    assert factory_calls == 1
    assert events == ["cdn", "backend"]
    assert len(history_instances) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        history_instances[0].schema_version()
    probe = WatchProcessLock(config.db_path)
    probe.acquire()
    probe.release()


async def test_cleanup_continues_after_coordinator_stop_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, webhook_url="https://receiver.example/endpoint-secret")
    events: list[str] = []
    backend = ClosingBackend(events)
    client = ClosingClient(events)
    notifier = RecordingNotifier(events)
    history: HistoryStore | None = None
    manager = None

    async with open_runtime(
        config,
        role="daemon",
        backend_factory=lambda _: backend,
        cdn_client_factory=lambda _: client,
        webhook_notifier_factory=lambda _: notifier,
    ) as runtime:
        history = runtime.history
        manager = runtime.manager
        assert runtime.coordinator is not None

        async def broken_stop() -> None:
            raise RuntimeError("coordinator stop failed")

        monkeypatch.setattr(runtime.coordinator, "stop", broken_stop)

    assert events == ["webhook", "cdn", "backend"]
    assert notifier.close_calls == 1
    assert history is not None
    with pytest.raises(sqlite3.ProgrammingError):
        history.schema_version()
    assert manager is not None and manager.executor_acquired is False


async def test_watch_output_failure_does_not_turn_successful_tick_into_error(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, webhook_url="https://receiver.example/endpoint-secret")
    _seed_watch(
        config,
        profile=Profile(pk="1", username="alice", biography="old", access="public"),
    )

    events: list[str] = []
    backend = ClosingBackend(events)
    backend.profiles["1"] = Profile(
        pk="1",
        username="alice",
        biography="new",
        access="public",
    )
    notifier = RecordingNotifier()

    def broken_output(_: str) -> None:
        raise RuntimeError("terminal unavailable")

    async with open_runtime(
        config,
        role="daemon",
        backend_factory=lambda _: backend,
        cdn_client_factory=lambda _: ClosingClient([]),
        watch_output=broken_output,
        webhook_notifier_factory=lambda _: notifier,
    ) as runtime:
        state = await runtime.manager.tick_once("alice")

        _assert_successful_state(state)
        assert len(notifier.payloads) == 1


async def test_changed_tick_sends_one_versioned_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, webhook_url="https://receiver.example/endpoint-secret")
    _seed_watch(
        config,
        profile=Profile(pk="1", username="alice", biography="old", access="public"),
    )
    backend = ClosingBackend([])
    backend.profiles["1"] = Profile(
        pk="1",
        username="alice",
        biography="new",
        access="public",
    )
    notifier = RecordingNotifier()
    event_id = UUID("12345678-1234-5678-1234-567812345678")
    observed_at = datetime(2026, 9, 4, 18, 30, 45, 123456, tzinfo=UTC)
    uuid_calls = 0
    clock_calls = 0

    def next_event_id() -> UUID:
        nonlocal uuid_calls
        uuid_calls += 1
        return event_id

    def utc_now() -> datetime:
        nonlocal clock_calls
        clock_calls += 1
        return observed_at

    monkeypatch.setattr(runtime_service, "uuid4", next_event_id)
    monkeypatch.setattr(runtime_service, "_utc_now", utc_now)

    async with open_runtime(
        config,
        role="daemon",
        backend_factory=lambda _: backend,
        cdn_client_factory=lambda _: ClosingClient([]),
        webhook_notifier_factory=lambda _: notifier,
    ) as runtime:
        state = await runtime.manager.tick_once("alice")

        _assert_successful_state(state)
        assert len(notifier.payloads) == 1
        event = notifier.payloads[0]
        assert event["schema_version"] == 1
        assert event["event"] == "watch.changed"
        assert event["username"] == "alice"
        assert event["changes"]["biography"] == {"old": "old", "new": "new"}
        assert event["event_id"] == str(event_id)
        assert event["observed_at"] == "2026-09-04T18:30:45.123456Z"
        assert uuid_calls == 1
        assert clock_calls == 1


@pytest.mark.parametrize("case", ["first_seen", "unchanged", "alias_history_only"])
async def test_suppressed_tick_does_not_send(tmp_path: Path, case: str) -> None:
    config = _config(tmp_path, webhook_url="https://receiver.example/endpoint-secret")
    backend = ClosingBackend([])
    current = Profile(pk="1", username="alice", biography="same", access="public")
    backend.profiles["1"] = current
    if case == "first_seen":
        _seed_watch(config)
    else:
        _seed_watch(config, profile=current)
        if case == "alias_history_only":
            history = HistoryStore(config.db_path)
            try:
                old_alias = Profile(
                    pk="1",
                    username="old_alice",
                    biography="same",
                    access="public",
                )
                snapshot = history.snapshot_from_profile(old_alias, post_pks=[])
                snapshot.captured_at -= 1
                history.add_snapshot(snapshot)
            finally:
                history.close()
    notifier = RecordingNotifier()

    async with open_runtime(
        config,
        role="daemon",
        backend_factory=lambda _: backend,
        cdn_client_factory=lambda _: ClosingClient([]),
        webhook_notifier_factory=lambda _: notifier,
    ) as runtime:
        state = await runtime.manager.tick_once("alice")

        _assert_successful_state(state)
        assert notifier.payloads == []


async def test_backend_failure_does_not_send(tmp_path: Path) -> None:
    config = _config(tmp_path, webhook_url="https://receiver.example/endpoint-secret")
    _seed_watch(config)
    backend = FailingBackend([])
    notifier = RecordingNotifier()

    async with open_runtime(
        config,
        role="daemon",
        backend_factory=lambda _: backend,
        cdn_client_factory=lambda _: ClosingClient([]),
        webhook_notifier_factory=lambda _: notifier,
    ) as runtime:
        state = await runtime.manager.tick_once("alice")

        assert state.status == "active"
        assert state.consecutive_errors == 1
        assert notifier.payloads == []


async def test_snapshot_terminal_and_delivery_order_is_exact(tmp_path: Path) -> None:
    config = _config(tmp_path, webhook_url="https://receiver.example/endpoint-secret")
    _seed_watch(
        config,
        profile=Profile(pk="1", username="alice", biography="old", access="public"),
    )
    backend = ClosingBackend([])
    backend.profiles["1"] = Profile(
        pk="1",
        username="alice",
        biography="new",
        access="public",
    )
    events: list[str] = []

    def inspect_persisted_snapshot(_: dict[str, Any]) -> None:
        probe = HistoryStore(config.db_path)
        try:
            snapshot = probe.last_snapshot("1")
            assert snapshot is not None
            assert snapshot.profile_fields["biography"] == "new"
        finally:
            probe.close()
        events.append("delivery")

    notifier = RecordingNotifier(on_send=inspect_persisted_snapshot)

    async with open_runtime(
        config,
        role="daemon",
        backend_factory=lambda _: backend,
        cdn_client_factory=lambda _: ClosingClient([]),
        watch_output=lambda _: events.append("terminal"),
        webhook_notifier_factory=lambda _: notifier,
    ) as runtime:
        original_add_snapshot = runtime.history.add_snapshot_async

        async def record_snapshot(snapshot: Any) -> None:
            await original_add_snapshot(snapshot)
            events.append("snapshot")

        runtime.history.add_snapshot_async = record_snapshot  # type: ignore[method-assign]
        state = await runtime.manager.tick_once("alice")

        _assert_successful_state(state)
        assert events == ["snapshot", "terminal", "delivery"]


async def test_known_delivery_failure_warns_once_without_changing_watch_state(
    tmp_path: Path,
) -> None:
    endpoint_sentinel = "endpoint-secret-9701"
    response_body_sentinel = "response-body-secret-9702"
    config = _config(
        tmp_path,
        webhook_url=f"https://receiver.example/{endpoint_sentinel}",
    )
    _seed_watch(
        config,
        profile=Profile(pk="1", username="alice", biography="old", access="public"),
    )
    backend = ClosingBackend([])
    backend.profiles["1"] = Profile(pk="1", username="alice", biography="new", access="public")
    notifier = RecordingNotifier(
        send_error=WebhookDeliveryError("watch webhook delivery failed: HTTP 503")
    )
    output: list[str] = []

    async with open_runtime(
        config,
        role="daemon",
        backend_factory=lambda _: backend,
        cdn_client_factory=lambda _: ClosingClient([]),
        watch_output=output.append,
        webhook_notifier_factory=lambda _: notifier,
    ) as runtime:
        state = await runtime.manager.tick_once("alice")

        _assert_successful_state(state)

    warnings = [line for line in output if "webhook warning" in line]
    assert len(warnings) == 1
    assert "HTTP 503" in warnings[0]
    captured = "\n".join(output)
    assert endpoint_sentinel not in captured
    assert response_body_sentinel not in captured


async def test_unexpected_delivery_failure_warns_generically_and_preserves_success(
    tmp_path: Path,
) -> None:
    endpoint_sentinel = "endpoint-secret-9801"
    response_body_sentinel = "response-body-secret-9802"
    config = _config(
        tmp_path,
        webhook_url=f"https://receiver.example/{endpoint_sentinel}",
    )
    _seed_watch(
        config,
        profile=Profile(pk="1", username="alice", biography="old", access="public"),
    )
    backend = ClosingBackend([])
    backend.profiles["1"] = Profile(pk="1", username="alice", biography="new", access="public")
    notifier = RecordingNotifier(
        send_error=RuntimeError(f"{endpoint_sentinel}: {response_body_sentinel}")
    )
    output: list[str] = []

    async with open_runtime(
        config,
        role="daemon",
        backend_factory=lambda _: backend,
        cdn_client_factory=lambda _: ClosingClient([]),
        watch_output=output.append,
        webhook_notifier_factory=lambda _: notifier,
    ) as runtime:
        state = await runtime.manager.tick_once("alice")

        _assert_successful_state(state)

    warnings = [line for line in output if "webhook warning" in line]
    assert warnings == ["@alice: watch webhook warning: unexpected delivery error"]
    captured = "\n".join(output)
    assert endpoint_sentinel not in captured
    assert response_body_sentinel not in captured


async def test_output_failures_cannot_block_delivery_warning_or_successful_state(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    endpoint_sentinel = "endpoint-secret-9851"
    response_body_sentinel = "response-body-secret-9852"
    config = _config(
        tmp_path,
        webhook_url=f"https://receiver.example/{endpoint_sentinel}",
    )
    _seed_watch(
        config,
        profile=Profile(pk="1", username="alice", biography="old", access="public"),
    )
    backend = ClosingBackend([])
    backend.profiles["1"] = Profile(pk="1", username="alice", biography="new", access="public")
    notifier = RecordingNotifier(
        send_error=RuntimeError(f"{endpoint_sentinel}: {response_body_sentinel}")
    )
    output_attempts: list[str] = []

    def broken_output(message: str) -> None:
        output_attempts.append(message)
        raise RuntimeError(f"output unavailable: {response_body_sentinel}")

    async with open_runtime(
        config,
        role="daemon",
        backend_factory=lambda _: backend,
        cdn_client_factory=lambda _: ClosingClient([]),
        watch_output=broken_output,
        webhook_notifier_factory=lambda _: notifier,
    ) as runtime:
        state = await runtime.manager.tick_once("alice")

        _assert_successful_state(state)

    assert len(output_attempts) == 2
    assert output_attempts[0].startswith("@alice changed")
    assert output_attempts[1] == "@alice: watch webhook warning: unexpected delivery error"
    assert notifier.close_calls == 1
    captured = capsys.readouterr()
    all_surfaces = "\n".join([*output_attempts, captured.out, captured.err])
    assert endpoint_sentinel not in all_surfaces
    assert response_body_sentinel not in all_surfaces


async def test_event_conversion_failure_warns_generically_and_preserves_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint_sentinel = "endpoint-secret-9901"
    response_body_sentinel = "response-body-secret-9902"
    config = _config(
        tmp_path,
        webhook_url=f"https://receiver.example/{endpoint_sentinel}",
    )
    _seed_watch(
        config,
        profile=Profile(pk="1", username="alice", biography="old", access="public"),
    )
    backend = ClosingBackend([])
    backend.profiles["1"] = Profile(pk="1", username="alice", biography="new", access="public")
    notifier = RecordingNotifier()
    output: list[str] = []

    def broken_conversion(*_: Any, **__: Any) -> dict[str, Any]:
        raise RuntimeError(f"{endpoint_sentinel}: {response_body_sentinel}")

    monkeypatch.setattr(runtime_service, "build_watch_event", broken_conversion)
    async with open_runtime(
        config,
        role="daemon",
        backend_factory=lambda _: backend,
        cdn_client_factory=lambda _: ClosingClient([]),
        watch_output=output.append,
        webhook_notifier_factory=lambda _: notifier,
    ) as runtime:
        state = await runtime.manager.tick_once("alice")

        _assert_successful_state(state)
        assert notifier.payloads == []

    warnings = [line for line in output if "webhook warning" in line]
    assert warnings == ["@alice: watch webhook warning: unexpected delivery error"]
    captured = "\n".join(output)
    assert endpoint_sentinel not in captured
    assert response_body_sentinel not in captured


async def test_delivery_cancellation_propagates_and_runtime_still_closes(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, webhook_url="https://receiver.example/endpoint-secret")
    _seed_watch(
        config,
        profile=Profile(pk="1", username="alice", biography="old", access="public"),
    )
    events: list[str] = []
    backend = ClosingBackend(events)
    backend.profiles["1"] = Profile(pk="1", username="alice", biography="new", access="public")
    notifier = RecordingNotifier(events)

    async def cancel_delivery(_: dict[str, Any]) -> None:
        raise asyncio.CancelledError

    notifier.send = cancel_delivery  # type: ignore[method-assign]
    with pytest.raises(asyncio.CancelledError):
        async with open_runtime(
            config,
            role="daemon",
            backend_factory=lambda _: backend,
            cdn_client_factory=lambda _: ClosingClient(events),
            webhook_notifier_factory=lambda _: notifier,
        ) as runtime:
            await runtime.manager.tick_once("alice")

    assert events == ["webhook", "cdn", "backend"]
    assert notifier.close_calls == 1


async def test_normal_teardown_cancels_watches_before_notifier_and_closes_in_order(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, webhook_url="https://receiver.example/endpoint-secret")
    _seed_watch(config)
    events: list[str] = []
    runtime_holder: dict[str, Any] = {}

    def assert_watches_cancelled() -> None:
        assert runtime_holder["runtime"].manager.list() == []

    notifier = RecordingNotifier(events, on_close=assert_watches_cancelled)
    async with open_runtime(
        config,
        role="daemon",
        backend_factory=lambda _: ClosingBackend(events),
        cdn_client_factory=lambda _: ClosingClient(events),
        webhook_notifier_factory=lambda _: notifier,
    ) as runtime:
        runtime_holder["runtime"] = runtime

    assert events == ["webhook", "cdn", "backend"]
    assert notifier.close_calls == 1


async def test_notifier_close_failure_is_observational_and_cleanup_continues(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, webhook_url="https://receiver.example/endpoint-secret")
    events: list[str] = []
    notifier = RecordingNotifier(events, close_error=RuntimeError("close failed"))

    async with open_runtime(
        config,
        role="repl",
        backend_factory=lambda _: ClosingBackend(events),
        cdn_client_factory=lambda _: ClosingClient(events),
        webhook_notifier_factory=lambda _: notifier,
    ):
        pass

    assert events == ["webhook", "cdn", "backend"]
    assert notifier.close_calls == 1


async def test_task_cancellation_propagates_after_all_runtime_cleanup(tmp_path: Path) -> None:
    config = _config(tmp_path, webhook_url="https://receiver.example/endpoint-secret")
    _seed_watch(config)
    events: list[str] = []
    notifier = RecordingNotifier(events)
    entered = asyncio.Event()
    never = asyncio.Event()
    history_holder: list[HistoryStore] = []
    manager_holder: list[Any] = []

    async def run() -> None:
        async with open_runtime(
            config,
            role="daemon",
            backend_factory=lambda _: ClosingBackend(events),
            cdn_client_factory=lambda _: ClosingClient(events),
            webhook_notifier_factory=lambda _: notifier,
        ) as runtime:
            history_holder.append(runtime.history)
            manager_holder.append(runtime.manager)
            entered.set()
            await never.wait()

    task = asyncio.create_task(run())
    await asyncio.wait_for(entered.wait(), timeout=1.0)
    task.cancel()
    async with asyncio.timeout(1.0):
        with pytest.raises(asyncio.CancelledError):
            await task

    assert events == ["webhook", "cdn", "backend"]
    assert notifier.close_calls == 1
    assert manager_holder[0].executor_acquired is False
    with pytest.raises(sqlite3.ProgrammingError):
        history_holder[0].schema_version()


async def test_repeated_cancellation_during_teardown_waits_for_all_cleanup(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, webhook_url="https://receiver.example/endpoint-secret")
    events: list[str] = []
    notifier = BlockingCloseNotifier(events)
    history_holder: list[HistoryStore] = []
    manager_holder: list[Any] = []
    release_calls = 0

    async def run() -> None:
        async with open_runtime(
            config,
            role="daemon",
            backend_factory=lambda _: ClosingBackend(events),
            cdn_client_factory=lambda _: ClosingClient(events),
            webhook_notifier_factory=lambda _: notifier,
        ) as runtime:
            nonlocal release_calls
            history_holder.append(runtime.history)
            manager_holder.append(runtime.manager)
            original_release = runtime.manager.release_executor

            def record_release() -> None:
                nonlocal release_calls
                release_calls += 1
                original_release()

            runtime.manager.release_executor = record_release  # type: ignore[method-assign]

    task = asyncio.create_task(run())
    await asyncio.wait_for(notifier.close_started.wait(), timeout=1.0)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert task.done() is False
    notifier.allow_close.set()
    async with asyncio.timeout(1.0):
        with pytest.raises(asyncio.CancelledError):
            await task

    assert events == ["webhook", "cdn", "backend"]
    assert notifier.close_calls == 1
    assert release_calls == 1
    assert manager_holder[0].executor_acquired is False
    with pytest.raises(sqlite3.ProgrammingError):
        history_holder[0].schema_version()
    probe = WatchProcessLock(config.db_path)
    probe.acquire()
    probe.release()
