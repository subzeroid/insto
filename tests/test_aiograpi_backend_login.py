"""AiograpiBackend fresh-login wiring tests.

These tests install a tiny fake ``aiograpi`` module into ``sys.modules`` so
the optional dependency is not required in CI. They verify that
``_ensure_logged_in`` submits a *generated* 6-digit TOTP code — not the raw
base32 seed — to ``client.login`` when a ``totp_seed`` is configured. See
issue #45.
"""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any

import pytest

from insto.backends.aiograpi import AiograpiBackend


class _FakeClient:
    """Records ``login`` kwargs and turns a seed into a fixed 6-digit code."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def set_proxy(self, proxy: str) -> None:  # pragma: no cover — unused here
        self.calls.append(("set_proxy", (proxy,), {}))

    @staticmethod
    def totp_generate_code(seed: str) -> str:
        # Real aiograpi returns a time-based 6-digit code; the fake returns a
        # deterministic 6-digit code keyed off the seed so the test is stable
        # while still proving the seed went through the generator.
        return "654321" if seed == "JBSWY3DPEHPK3PXP" else "000000"

    async def login(
        self,
        username: str | None = None,
        password: str | None = None,
        relogin: bool = False,
        verification_code: str = "",
    ) -> bool:
        self.calls.append(
            (
                "login",
                (username, password),
                {"relogin": relogin, "verification_code": verification_code},
            )
        )
        return True


@pytest.fixture
def fake_aiograpi(monkeypatch: pytest.MonkeyPatch) -> type[_FakeClient]:
    module = ModuleType("aiograpi")
    module.Client = _FakeClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "aiograpi", module)
    return _FakeClient


async def test_login_submits_generated_code_not_raw_seed(
    fake_aiograpi: type[_FakeClient],
) -> None:
    seed = "JBSWY3DPEHPK3PXP"
    backend = AiograpiBackend(username="user", password="pass", totp_seed=seed)
    client = backend._client

    await backend._ensure_logged_in()

    assert backend._logged_in is True
    assert client.calls == [
        (
            "login",
            ("user", "pass"),
            {"relogin": False, "verification_code": "654321"},
        )
    ]
    # The raw seed must never reach Instagram as the verification code.
    assert client.calls[0][2]["verification_code"] != seed


async def test_login_without_seed_sends_empty_code(
    fake_aiograpi: type[_FakeClient],
) -> None:
    backend = AiograpiBackend(username="user", password="pass")
    client = backend._client

    await backend._ensure_logged_in()

    assert client.calls == [
        (
            "login",
            ("user", "pass"),
            {"relogin": False, "verification_code": ""},
        )
    ]
