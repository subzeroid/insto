"""AiograpiBackend read-only private-surface wiring tests.

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

    async def collections(self) -> list[Any]:
        self.calls.append(("collections", (), {}))
        return [SimpleNamespace(id="c1", name="Research", type="MEDIA", media_count=1)]

    async def collection_pk_by_name(self, name: str) -> int:
        self.calls.append(("collection_pk_by_name", (name,), {}))
        return 123

    async def collection_medias(
        self,
        collection_pk: str,
        amount: int = 21,
        last_media_pk: int = 0,
    ) -> list[Any]:
        self.calls.append(
            (
                "collection_medias",
                (collection_pk,),
                {"amount": amount, "last_media_pk": last_media_pk},
            )
        )
        return [
            SimpleNamespace(
                pk="m1",
                code="ABC123",
                taken_at=datetime(2026, 4, 17, 17, 45, 12, tzinfo=UTC),
                media_type=1,
                caption_text="saved",
                like_count=1,
                comment_count=0,
                thumbnail_url="https://cdn.example/saved.jpg",
                user=SimpleNamespace(pk="25025320", username="instagram"),
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


async def test_saved_collections_calls_read_only_sdk_method(
    fake_aiograpi: type[_FakeClient],
) -> None:
    backend = _backend()
    client = backend._client

    collections = [collection async for collection in backend.iter_saved_collections(limit=1)]

    assert collections[0].pk == "c1"
    assert collections[0].name == "Research"
    assert client.calls == [("collections", (), {})]


async def test_saved_posts_calls_generic_saved_surface(
    fake_aiograpi: type[_FakeClient],
) -> None:
    backend = _backend()
    client = backend._client

    posts = [post async for post in backend.iter_saved_posts(limit=2)]

    assert posts[0].pk == "m1"
    assert posts[0].owner_username == "instagram"
    assert client.calls == [("collection_medias", ("saved",), {"amount": 2, "last_media_pk": 0})]


async def test_saved_posts_resolves_named_collection(
    fake_aiograpi: type[_FakeClient],
) -> None:
    backend = _backend()
    client = backend._client

    posts = [post async for post in backend.iter_saved_posts(collection="Research", limit=2)]

    assert posts[0].pk == "m1"
    assert client.calls == [
        ("collection_pk_by_name", ("Research",), {}),
        ("collection_medias", ("123",), {"amount": 2, "last_media_pk": 0}),
    ]
