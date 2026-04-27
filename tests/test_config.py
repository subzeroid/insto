"""Tests for insto.config."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from insto import config as cfgmod
from insto.config import (
    Config,
    config_dir,
    config_file_path,
    effective_config_report,
    ensure_config_dir,
    load_config,
    write_config,
)
from insto.exceptions import BackendError


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point $INSTO_HOME at tmp_path and clear known env vars per test."""
    monkeypatch.setenv(cfgmod.CONFIG_HOME_ENV, str(tmp_path / ".insto"))
    for var in (cfgmod.ENV_TOKEN, cfgmod.ENV_PROXY, cfgmod.ENV_OUTPUT_DIR, cfgmod.ENV_DB_PATH):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def _mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_config_dir_honors_env(_isolated_home: Path) -> None:
    expected = _isolated_home / ".insto"
    assert config_dir() == expected


def test_ensure_config_dir_creates_with_0700() -> None:
    p = ensure_config_dir()
    assert p.exists() and p.is_dir()
    assert _mode(p) == 0o700


def test_ensure_config_dir_hardens_existing_dir() -> None:
    p = config_dir()
    p.mkdir(parents=True)
    p.chmod(0o755)
    ensure_config_dir()
    assert _mode(p) == 0o700


def test_load_config_defaults_when_no_inputs() -> None:
    cfg = load_config()
    assert cfg.hiker_token is None
    assert cfg.hiker_proxy is None
    assert cfg.output_dir == Path("./output")
    assert cfg.db_path == config_dir() / "store.db"
    assert cfg.cli_history_path == config_dir() / "cli_history"
    assert cfg.sources["hiker.token"] == "default"
    assert cfg.sources["output_dir"] == "default"


def test_load_config_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(cfgmod.ENV_TOKEN, "tok-from-env")
    monkeypatch.setenv(cfgmod.ENV_PROXY, "socks5h://127.0.0.1:9050")
    monkeypatch.setenv(cfgmod.ENV_OUTPUT_DIR, "/tmp/insto-out")
    cfg = load_config()
    assert cfg.hiker_token == "tok-from-env"
    assert cfg.hiker_proxy == "socks5h://127.0.0.1:9050"
    assert cfg.output_dir == Path("/tmp/insto-out")
    assert cfg.sources["hiker.token"] == "env"
    assert cfg.sources["hiker.proxy"] == "env"
    assert cfg.sources["output_dir"] == "env"


def test_load_config_reads_toml() -> None:
    write_config(
        {
            "hiker": {"token": "tok-toml", "proxy": "http://proxy:3128"},
            "output_dir": "./out-toml",
            "db_path": "/var/insto.db",
        }
    )
    cfg = load_config()
    assert cfg.hiker_token == "tok-toml"
    assert cfg.hiker_proxy == "http://proxy:3128"
    assert cfg.output_dir == Path("./out-toml")
    assert cfg.db_path == Path("/var/insto.db")
    assert cfg.sources["hiker.token"] == "toml"
    assert cfg.sources["hiker.proxy"] == "toml"
    assert cfg.sources["output_dir"] == "toml"
    assert cfg.sources["db_path"] == "toml"


def test_precedence_flag_beats_env_beats_toml(monkeypatch: pytest.MonkeyPatch) -> None:
    write_config({"hiker": {"token": "from-toml"}})
    monkeypatch.setenv(cfgmod.ENV_TOKEN, "from-env")

    cfg_env = load_config()
    assert cfg_env.hiker_token == "from-env"
    assert cfg_env.sources["hiker.token"] == "env"

    cfg_flag = load_config({"hiker_token": "from-flag"})
    assert cfg_flag.hiker_token == "from-flag"
    assert cfg_flag.sources["hiker.token"] == "flag"

    monkeypatch.delenv(cfgmod.ENV_TOKEN)
    cfg_toml = load_config()
    assert cfg_toml.hiker_token == "from-toml"
    assert cfg_toml.sources["hiker.token"] == "toml"


def test_write_config_creates_0600_file() -> None:
    path = write_config({"hiker": {"token": "abc1234567890"}})
    assert path == config_file_path()
    assert _mode(path) == 0o600
    parent_mode = _mode(path.parent)
    assert parent_mode == 0o700


def test_load_refuses_world_readable_toml() -> None:
    write_config({"hiker": {"token": "abc"}})
    config_file_path().chmod(0o644)
    with pytest.raises(BackendError, match="group/world-accessible"):
        load_config()


def test_load_refuses_group_readable_toml() -> None:
    write_config({"hiker": {"token": "abc"}})
    config_file_path().chmod(0o640)
    with pytest.raises(BackendError, match="group/world-accessible"):
        load_config()


def test_write_config_overwrites_existing_securely() -> None:
    p1 = write_config({"hiker": {"token": "first"}})
    assert _mode(p1) == 0o600
    p2 = write_config({"hiker": {"token": "second"}})
    assert p1 == p2
    assert _mode(p2) == 0o600
    cfg = load_config()
    assert cfg.hiker_token == "second"


def test_effective_config_report_origins(monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(
        {
            "hiker": {"token": "tok-toml-1234567890"},
            "output_dir": "./from-toml",
        }
    )
    monkeypatch.setenv(cfgmod.ENV_PROXY, "http://proxy:1")
    cfg = load_config({"db_path": "/flag/db.sqlite"})

    rows = {row["key"]: row for row in effective_config_report(cfg)}

    assert rows["hiker.token"]["origin"] == "toml"
    assert rows["hiker.token"]["value"].startswith("***")
    assert rows["hiker.token"]["value"].endswith("7890")
    assert "tok-toml" not in rows["hiker.token"]["value"]

    assert rows["hiker.proxy"]["origin"] == "env"
    assert rows["hiker.proxy"]["value"] == "http://proxy:1"

    assert rows["output_dir"]["origin"] == "toml"
    assert rows["output_dir"]["value"] == "from-toml"

    assert rows["db_path"]["origin"] == "flag"
    assert rows["db_path"]["value"] == "/flag/db.sqlite"

    assert rows["cli_history_path"]["origin"] == "default"


def test_effective_config_report_defaults_when_unset() -> None:
    cfg = load_config()
    rows = {row["key"]: row for row in effective_config_report(cfg)}
    assert rows["hiker.token"]["value"] is None
    assert rows["hiker.token"]["origin"] == "default"
    assert rows["hiker.proxy"]["value"] is None
    assert rows["output_dir"]["value"] == "output"
    assert rows["output_dir"]["origin"] == "default"


def test_config_dataclass_has_slots() -> None:
    cfg = Config()
    with pytest.raises(AttributeError):
        cfg.unknown_attr = 1  # type: ignore[attr-defined]


def test_world_accessible_check_uses_group_and_other_bits(tmp_path: Path) -> None:
    """Sanity: the helper should reject any non-owner perms set on the file."""
    p = tmp_path / "secret.toml"
    p.write_bytes(b"")
    p.chmod(0o600)
    cfgmod._check_not_world_readable(p)
    for bad in (0o604, 0o620, 0o644, 0o666, 0o660, 0o602):
        p.chmod(bad)
        with pytest.raises(BackendError):
            cfgmod._check_not_world_readable(p)


def test_empty_env_var_treated_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty string in env should not override toml."""
    write_config({"hiker": {"token": "from-toml"}})
    monkeypatch.setenv(cfgmod.ENV_TOKEN, "")
    cfg = load_config()
    assert cfg.hiker_token == "from-toml"
    assert cfg.sources["hiker.token"] == "toml"


def test_umask_does_not_leak_perms(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even if umask is permissive, written file must be 0600."""
    old_umask = os.umask(0o000)
    try:
        write_config({"hiker": {"token": "x"}})
        mode = config_file_path().stat().st_mode & 0o777
        assert mode == 0o600
        leaked = config_file_path().stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO)
        assert leaked == 0
    finally:
        os.umask(old_umask)
