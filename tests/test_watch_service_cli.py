"""Service routing must stay credential-free and leave legacy CLI intact."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from insto import cli
from insto.config import Config


@pytest.fixture
def service_stub(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[Any]:
    calls: list[Any] = []

    async def status(**kwargs: Any) -> dict[str, Any]:
        calls.append(("status", kwargs))
        return {"schema_version": 1, "installation": "absent", "watches": []}

    async def install(**kwargs: Any) -> dict[str, Any]:
        calls.append(("install", kwargs))
        return {"schema_version": 1, "installation": "installed"}

    async def uninstall(**kwargs: Any) -> dict[str, Any]:
        calls.append(("uninstall", kwargs))
        return {"schema_version": 1, "installation": "absent"}

    monkeypatch.setitem(
        sys.modules,
        "insto.service.watch_service",
        SimpleNamespace(
            service_status=status,
            install_service=install,
            uninstall_service=uninstall,
        ),
    )
    monkeypatch.setenv("INSTO_HOME", str(tmp_path / "missing-home"))
    monkeypatch.setattr(cli, "setup_logging", lambda *a, **kw: pytest.fail("status logged"))
    monkeypatch.setattr(cli, "_safe_load_config", lambda *a: pytest.fail("status loaded config"))
    return calls


def test_status_json_needs_no_config_or_logging(service_stub: list[Any], capsys: Any) -> None:
    assert cli.main(["watch-service", "status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["installation"] == "absent"
    assert service_stub == [("status", {})]


def test_install_passes_explicit_env_file(service_stub: list[Any]) -> None:
    assert cli.main(["watch-service", "install", "--env-file", "/private/service.toml"]) == 0
    assert service_stub == [("install", {"env_file": Path("/private/service.toml")})]


def test_uninstall_routes_without_config(service_stub: list[Any]) -> None:
    assert cli.main(["watch-service", "uninstall"]) == 0
    assert service_stub == [("uninstall", {})]


def test_service_help_is_specific(service_stub: list[Any], capsys: Any) -> None:
    with pytest.raises(SystemExit) as caught:
        cli.main(["watch-service", "--help"])
    assert caught.value.code == 0
    assert "uninstall" in capsys.readouterr().out
    assert service_stub == []


@pytest.mark.parametrize("flag", ["--hiker-token", "--proxy", "--backend"])
def test_service_rejects_global_overrides_without_echoing_value(
    service_stub: list[Any],
    capsys: Any,
    flag: str,
) -> None:
    with pytest.raises(SystemExit) as caught:
        cli.main(["watch-service", "install", flag, "private-sensitive-value"])
    assert caught.value.code == 2
    assert "private-sensitive-value" not in capsys.readouterr().err
    assert not service_stub


def test_at_service_name_remains_a_target(monkeypatch: pytest.MonkeyPatch) -> None:
    async def oneshot(*args: Any) -> int:
        assert args[1] == "@watch-service"
        return 17

    monkeypatch.setattr(cli, "setup_logging", lambda *a, **kw: None)
    monkeypatch.setattr(cli, "_run_oneshot", oneshot)
    assert cli.main(["@watch-service", "-c", "info"]) == 17


def test_daemon_accepts_service_output_sink(monkeypatch: pytest.MonkeyPatch) -> None:
    from insto.service import runtime

    async def run(stop: asyncio.Event) -> None:
        return None

    async def list_watches() -> list[Any]:
        return []

    @asynccontextmanager
    async def fake_runtime(*args: Any, **kwargs: Any):
        kwargs["watch_output"]("tick-event")
        yield SimpleNamespace(
            coordinator=SimpleNamespace(run=run),
            manager=[],
            history=SimpleNamespace(list_watches_async=list_watches),
        )

    monkeypatch.setattr(runtime, "open_runtime", fake_runtime)
    messages: list[str] = []
    assert (
        asyncio.run(
            cli._run_watch_daemon(
                Config(),
                logging.getLogger("test"),
                output=messages.append,
            )
        )
        == 0
    )
    assert "tick-event" in messages
    assert any("watch daemon started" in message for message in messages)


def test_text_status_displays_unavailable_database_and_interpreter(capsys: Any) -> None:
    cli._print_service_result(
        {
            "installation": "installed",
            "registration": "loaded",
            "process": {"state": None, "pid": None, "last_exit_code": 1},
            "paths": {"python": "/missing/python"},
            "interpreter_available": False,
            "database_state": "unavailable",
            "database_error": "watch database could not be read safely",
            "watches": [],
        }
    )
    output = capsys.readouterr().out
    assert "database: unavailable" in output
    assert "watch database could not be read safely" in output
    assert "interpreter unavailable" in output
    assert "reinstall" in output
    assert "process: unknown" in output
