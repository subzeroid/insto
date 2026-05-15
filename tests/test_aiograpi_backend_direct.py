"""AiograpiBackend Direct read-method wiring tests.

These tests install a tiny fake ``aiograpi`` module into ``sys.modules`` so
the optional dependency is not required in CI. They only verify insto's wiring
to read-only SDK methods; mapper behavior is covered in ``test_aiograpi_map``.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from insto.backends.aiograpi import AiograpiBackend
from insto.exceptions import BackendError


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    async def direct_threads(
        self,
        *,
        amount: int = 20,
        selected_filter: str = "",
        box: str = "",
        thread_message_limit: int | None = None,
    ) -> list[Any]:
        self.calls.append(
            (
                "direct_threads",
                (),
                {
                    "amount": amount,
                    "selected_filter": selected_filter,
                    "box": box,
                    "thread_message_limit": thread_message_limit,
                },
            )
        )
        return [
            SimpleNamespace(
                id="123",
                thread_title="Alice",
                users=[SimpleNamespace(pk="100", username="alice")],
                last_activity_at=datetime(2026, 4, 17, 17, 45, 12, tzinfo=UTC),
                messages=[],
                is_group=False,
                pending=False,
                archived=False,
                muted=False,
            )
        ]

    async def direct_messages(self, thread_id: int, *, amount: int = 20) -> list[Any]:
        self.calls.append(("direct_messages", (thread_id,), {"amount": amount}))
        return [
            SimpleNamespace(
                id="m1",
                user_id="100",
                thread_id=thread_id,
                timestamp=datetime(2026, 4, 17, 17, 45, 12, tzinfo=UTC),
                item_type="text",
                text="hello",
            )
        ]


@pytest.fixture
def fake_aiograpi(monkeypatch: pytest.MonkeyPatch) -> type[_FakeClient]:
    module = ModuleType("aiograpi")
    module.Client = _FakeClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "aiograpi", module)
    return _FakeClient


def _backend() -> AiograpiBackend:
    backend = AiograpiBackend(username="user", password="pass")
    backend._logged_in = True
    return backend


async def test_direct_threads_calls_read_only_sdk_method(fake_aiograpi: type[_FakeClient]) -> None:
    backend = _backend()
    client = backend._client

    threads = [thread async for thread in backend.iter_direct_threads(limit=3)]

    assert threads[0].pk == "123"
    assert threads[0].title == "Alice"
    assert client.calls == [
        (
            "direct_threads",
            (),
            {
                "amount": 3,
                "selected_filter": "",
                "box": "",
                "thread_message_limit": 1,
            },
        )
    ]


async def test_direct_messages_calls_read_only_sdk_method(fake_aiograpi: type[_FakeClient]) -> None:
    backend = _backend()
    client = backend._client

    messages = [message async for message in backend.iter_direct_messages("123", limit=5)]

    assert messages[0].pk == "m1"
    assert messages[0].thread_id == "123"
    assert client.calls == [("direct_messages", (123,), {"amount": 5})]


async def test_direct_messages_rejects_non_numeric_thread_id(
    fake_aiograpi: type[_FakeClient],
) -> None:
    backend = _backend()

    with pytest.raises(BackendError, match="invalid direct thread id"):
        [message async for message in backend.iter_direct_messages("not-a-number", limit=5)]
