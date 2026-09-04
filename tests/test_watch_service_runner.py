from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import pytest

from insto._redact import clear_registered_secrets
from insto.exceptions import BackendError
from insto.service import watch_service_runner as runner


def _private(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path


@pytest.fixture(autouse=True)
def _secrets() -> None:
    clear_registered_secrets()
    yield
    clear_registered_secrets()


def test_read_service_env_accepts_only_documented_string_keys(tmp_path: Path) -> None:
    path = _private(
        tmp_path / "service.toml",
        '[env]\nHIKERAPI_TOKEN = "fresh-token"\nINSTO_WATCH_WEBHOOK_URL = ""\n',
    )

    assert runner.read_service_env(path) == {
        "HIKERAPI_TOKEN": "fresh-token",
        "INSTO_WATCH_WEBHOOK_URL": "",
    }
    assert runner.read_service_env(None) == {}


@pytest.mark.parametrize(
    "payload",
    [
        '[other]\nHIKERAPI_TOKEN = "secret-value"\n',
        '[env]\nUNKNOWN = "secret-value"\n',
        "[env]\nHIKERAPI_TOKEN = 7\n",
        '[env]\nHIKERAPI_TOKEN = "bad\\u0000value"\n',
    ],
)
def test_read_service_env_rejects_invalid_schema_without_echoing_values(
    tmp_path: Path, payload: str
) -> None:
    path = _private(tmp_path / "service.toml", payload)

    with pytest.raises(BackendError) as raised:
        runner.read_service_env(path)

    assert "secret-value" not in str(raised.value)
    assert "bad" not in str(raised.value)


def test_resolve_service_config_ignores_inherited_app_and_proxy_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    _private(home / "config.toml", '[hikerapi]\ntoken = "config-token"\n')
    monkeypatch.setenv("HIKERAPI_TOKEN", "inherited-token")
    monkeypatch.setenv("INSTO_BACKEND", "aiograpi")
    monkeypatch.setenv("HTTPS_PROXY", "http://inherited.proxy")

    config = runner.resolve_service_config(home, None)

    assert config.hiker_token == "config-token"
    assert config.backend == "hikerapi"
    assert "HTTPS_PROXY" in os.environ  # startup-only resolution restores the caller


def test_resolve_service_config_env_file_precedence_and_empty_values(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _private(
        home / "config.toml",
        '[hikerapi]\ntoken = "config-token"\nproxy = "http://config.proxy"\n',
    )
    env_file = _private(
        tmp_path / "service.toml",
        '[env]\nHIKERAPI_TOKEN = "fresh-token"\nHIKERAPI_PROXY = ""\n',
    )

    config = runner.resolve_service_config(home, env_file)

    assert config.hiker_token == "fresh-token"
    assert config.hiker_proxy == "http://config.proxy"


def test_resolve_service_config_keeps_install_time_pins(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _private(
        home / "config.toml",
        'backend = "hikerapi"\noutput_dir = "/changed/out"\ndb_path = "/changed/db"\n'
        '[hikerapi]\ntoken = "rotated-token"\n[aiograpi]\nsession_path = "/changed/session"\n',
    )

    config = runner.resolve_service_config(
        home,
        None,
        pinned={
            "backend": "fake",
            "db_path": str(tmp_path / "store.db"),
            "output_dir": str(tmp_path / "out"),
            "aiograpi_session_path": str(tmp_path / "session.json"),
        },
    )

    assert config.backend == "fake"
    assert config.db_path == tmp_path / "store.db"
    assert config.output_dir == tmp_path / "out"
    assert config.aiograpi_session_path == tmp_path / "session.json"
    assert config.hiker_token == "rotated-token"


def test_resolve_service_config_validates_required_credentials(tmp_path: Path) -> None:
    home = tmp_path / "home"

    with pytest.raises(BackendError, match="credential"):
        runner.resolve_service_config(home, None, pinned={"backend": "hikerapi"})


@pytest.mark.parametrize(
    "payload",
    [
        "[hikerapi]\ntoken = 7\n",
        '[hikerapi]\ntoken = "bad\\u0000token"\n',
        '[aiograpi]\nusername = []\npassword = "valid-password"\n',
    ],
)
def test_resolve_service_config_rejects_unsafe_credential_types(
    tmp_path: Path, payload: str
) -> None:
    home = tmp_path / "home"
    _private(home / "config.toml", payload)

    with pytest.raises(BackendError, match="credential") as raised:
        runner.resolve_service_config(home, None, pinned={"backend": "fake"})

    assert "bad" not in str(raised.value)


def test_secure_service_logging_rotates_and_redacts(tmp_path: Path) -> None:
    from insto._redact import register_secret

    secret = "highly-sensitive-token"
    register_secret(secret)
    log = runner.setup_service_logging(tmp_path, max_bytes=80, backup_count=2)
    for _ in range(8):
        log.info("tick %s padding-padding-padding", secret)
    for handler in log.handlers:
        handler.flush()

    files = list(tmp_path.glob("insto.log*"))
    assert len(files) > 1
    assert all((item.stat().st_mode & 0o077) == 0 for item in files)
    assert secret not in "".join(item.read_text() for item in files)
    runner.close_service_logging(log)


def test_secure_service_logging_rejects_symlink_and_fifo(tmp_path: Path) -> None:
    tmp_path.mkdir(exist_ok=True)
    target = _private(tmp_path / "target", "old")
    (tmp_path / "insto.log").symlink_to(target)
    with pytest.raises(BackendError):
        runner.setup_service_logging(tmp_path)

    (tmp_path / "insto.log").unlink()
    os.mkfifo(tmp_path / "insto.log")
    with pytest.raises(BackendError):
        runner.setup_service_logging(tmp_path)


def test_secure_service_logging_rejects_unsafe_rotated_sibling(tmp_path: Path) -> None:
    target = _private(tmp_path / "target", "old")
    (tmp_path / "insto.log.1").symlink_to(target)

    with pytest.raises(BackendError):
        runner.setup_service_logging(tmp_path)


def test_secure_log_open_closes_descriptor_when_wrapping_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_open = os.open
    opened: list[int] = []
    closed: list[int] = []

    def tracked_open(*args: Any, **kwargs: Any) -> int:
        fd = real_open(*args, **kwargs)
        opened.append(fd)
        return fd

    def tracked_close(fd: int) -> None:
        closed.append(fd)
        os_close(fd)

    os_close = os.close
    monkeypatch.setattr(runner.os, "open", tracked_open)
    monkeypatch.setattr(runner.os, "close", tracked_close)
    monkeypatch.setattr(runner.os, "fdopen", lambda *_a, **_kw: (_ for _ in ()).throw(OSError()))

    with pytest.raises(BackendError):
        runner.setup_service_logging(tmp_path)

    assert closed == opened


def test_main_keeps_clean_environment_while_daemon_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = tmp_path / "services" / "watch" / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}")
    manifest.chmod(0o600)
    pinned: dict[str, Any] = {
        "schema_version": 1,
        "managed_by": "insto-watch-service",
        "uid": os.getuid(),
        "label": "test",
        "config_home": str(tmp_path),
        "python": os.path.abspath(os.sys.executable),
        "backend": "fake",
        "db_path": str(tmp_path / "store.db"),
        "output_dir": str(tmp_path / "out"),
        "aiograpi_session_path": str(tmp_path / "session.json"),
        "env_file": None,
    }
    monkeypatch.setenv("INSTO_BACKEND", "hikerapi")
    monkeypatch.setenv("HTTPS_PROXY", "http://inherited.proxy")
    monkeypatch.setattr(runner, "_load_runner_manifest", lambda _path: (pinned, tmp_path))
    seen: dict[str, str | None] = {}

    async def fake_daemon(_config: Any, _log: logging.Logger, *, output: Any = None) -> int:
        seen["backend"] = os.environ.get("INSTO_BACKEND")
        seen["proxy"] = os.environ.get("HTTPS_PROXY")
        assert output is not None
        output("daemon tick")
        return 0

    monkeypatch.setattr(runner, "_run_daemon", fake_daemon)

    assert runner.main([str(manifest)]) == 0
    assert seen == {"backend": "fake", "proxy": None}
    assert os.environ["INSTO_BACKEND"] == "hikerapi"
    assert os.environ["HTTPS_PROXY"] == "http://inherited.proxy"


def test_main_returns_one_for_startup_failure_without_secret_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "startup-secret-value"
    monkeypatch.setattr(
        runner,
        "_load_runner_manifest",
        lambda _path: (_ for _ in ()).throw(BackendError(f"invalid {secret}")),
    )

    assert runner.main([str(tmp_path / "manifest.json")]) == 1
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err


def test_main_logs_configuration_failure_after_manifest_is_trusted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = tmp_path / "services" / "watch" / "manifest.json"
    pinned = {"env_file": None, "backend": "hikerapi"}
    monkeypatch.setattr(runner, "_load_runner_manifest", lambda _path: (pinned, tmp_path))
    monkeypatch.setattr(
        runner,
        "resolve_service_config",
        lambda *_a, **_kw: (_ for _ in ()).throw(BackendError("credential unavailable")),
    )

    assert runner.main([str(manifest)]) == 1

    log_text = (tmp_path / "services" / "watch" / "logs" / "insto.log").read_text()
    assert "credential unavailable" in log_text


def test_main_reads_service_env_only_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = tmp_path / "services" / "watch" / "manifest.json"
    pinned = {"env_file": str(tmp_path / "env.toml"), "backend": "fake"}
    config = runner.Config(backend="fake")
    calls = 0

    def resolve(*_args: Any, **_kwargs: Any) -> runner.Config:
        nonlocal calls
        calls += 1
        return config

    monkeypatch.setattr(runner, "_load_runner_manifest", lambda _path: (pinned, tmp_path))
    monkeypatch.setattr(runner, "resolve_service_config", resolve)
    monkeypatch.setattr(runner, "read_service_env", lambda _path: pytest.fail("duplicate read"))
    monkeypatch.setattr(runner, "_run_daemon", lambda *_a, **_kw: _return_zero())

    assert runner.main([str(manifest)]) == 0
    assert calls == 1


async def _return_zero() -> int:
    return 0


def test_main_reports_safe_diagnostic_when_manifest_cannot_be_trusted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        runner,
        "_load_runner_manifest",
        lambda _path: (_ for _ in ()).throw(BackendError("unsafe /secret/private/path")),
    )

    assert runner.main([str(tmp_path / "manifest.json")]) == 1

    captured = capsys.readouterr()
    assert "watch service startup failed" in captured.err
    assert "/secret/private/path" not in captured.err
