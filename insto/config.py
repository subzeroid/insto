"""Config loader: env + ~/.insto/config.toml with cli precedence.

Precedence (highest first): explicit flag (cli_overrides) > env var > toml file >
hard-coded default. The home directory `~/.insto` is created with mode `0700`
and `config.toml` is written with mode `0600`. Loading a world- or
group-readable `config.toml` is refused with a clear `BackendError` so a token
never silently leaks via permissions drift.

Tests can isolate the on-disk state by setting `$INSTO_HOME` to a tmp_path.
"""

from __future__ import annotations

import os
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import tomli_w

from insto.exceptions import BackendError

CONFIG_HOME_ENV = "INSTO_HOME"
DEFAULT_CONFIG_DIR_NAME = ".insto"

ENV_TOKEN = "HIKERAPI_TOKEN"
ENV_PROXY = "HIKERAPI_PROXY"
ENV_OUTPUT_DIR = "INSTO_OUTPUT_DIR"
ENV_DB_PATH = "INSTO_DB_PATH"

Origin = Literal["flag", "env", "toml", "default"]


def config_dir() -> Path:
    """Return path to ~/.insto (or $INSTO_HOME for test isolation)."""
    override = os.environ.get(CONFIG_HOME_ENV)
    if override:
        return Path(override)
    return Path.home() / DEFAULT_CONFIG_DIR_NAME


def config_file_path() -> Path:
    """Return path to ~/.insto/config.toml."""
    return config_dir() / "config.toml"


def db_path() -> Path:
    """Default sqlite store path (~/.insto/store.db)."""
    return config_dir() / "store.db"


def output_dir() -> Path:
    """Default output directory for exports and downloaded media."""
    return Path("./output")


def cli_history_path() -> Path:
    """Path to the prompt_toolkit history file (~/.insto/cli_history)."""
    return config_dir() / "cli_history"


def ensure_config_dir() -> Path:
    """Create ~/.insto if missing; ensure mode 0700; return its Path."""
    p = config_dir()
    if p.exists():
        if not p.is_dir():
            raise BackendError(f"config dir is not a directory: {p}")
        p.chmod(0o700)
    else:
        p.mkdir(mode=0o700, parents=True, exist_ok=True)
        p.chmod(0o700)
    return p


@dataclass(slots=True)
class Config:
    """Resolved configuration. `sources` maps each key to where its value came from."""

    hiker_token: str | None = None
    hiker_proxy: str | None = None
    output_dir: Path = field(default_factory=lambda: Path("./output"))
    db_path: Path = field(default_factory=db_path)
    cli_history_path: Path = field(default_factory=cli_history_path)
    sources: dict[str, Origin] = field(default_factory=dict)


def _check_not_world_readable(path: Path) -> None:
    """Raise BackendError if `path` has any group/other permission bits set."""
    st = path.stat()
    leaked = st.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
    if leaked:
        raise BackendError(
            f"refusing to read group/world-accessible config: {path} "
            f"(mode={oct(st.st_mode & 0o777)}); chmod 600 required"
        )


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    _check_not_world_readable(path)
    with path.open("rb") as f:
        return tomllib.load(f)


def _pick(
    cli: dict[str, Any],
    cli_key: str,
    env_var: str | None,
    toml_value: Any,
    default: Any,
) -> tuple[Any, Origin]:
    """Apply precedence: cli > env > toml > default."""
    if cli_key in cli and cli[cli_key] is not None:
        return cli[cli_key], "flag"
    if env_var:
        env_value = os.environ.get(env_var)
        if env_value is not None and env_value != "":
            return env_value, "env"
    if toml_value is not None:
        return toml_value, "toml"
    return default, "default"


def load_config(cli_overrides: dict[str, Any] | None = None) -> Config:
    """Build a Config with precedence: cli_overrides > env > toml > defaults.

    Recognised `cli_overrides` keys:
        hiker_token, hiker_proxy, output_dir, db_path
    """
    cli = cli_overrides or {}
    toml_data = _read_toml(config_file_path())
    raw_hiker = toml_data.get("hiker")
    hiker_toml: dict[str, Any] = raw_hiker if isinstance(raw_hiker, dict) else {}

    sources: dict[str, Origin] = {}

    token, sources["hiker.token"] = _pick(
        cli, "hiker_token", ENV_TOKEN, hiker_toml.get("token"), None
    )
    proxy, sources["hiker.proxy"] = _pick(
        cli, "hiker_proxy", ENV_PROXY, hiker_toml.get("proxy"), None
    )
    out_value, sources["output_dir"] = _pick(
        cli, "output_dir", ENV_OUTPUT_DIR, toml_data.get("output_dir"), "./output"
    )
    db_value, sources["db_path"] = _pick(
        cli, "db_path", ENV_DB_PATH, toml_data.get("db_path"), str(db_path())
    )
    sources["cli_history_path"] = "default"

    return Config(
        hiker_token=token,
        hiker_proxy=proxy,
        output_dir=Path(out_value),
        db_path=Path(db_value),
        cli_history_path=cli_history_path(),
        sources=sources,
    )


def write_config(values: dict[str, Any]) -> Path:
    """Write `values` to ~/.insto/config.toml as 0600. Refuse world-readable result.

    `values` mirrors what `load_config` reads, e.g.
    `{"hiker": {"token": "...", "proxy": "..."}, "output_dir": "./out"}`.
    """
    ensure_config_dir()
    path = config_file_path()
    payload = tomli_w.dumps(values).encode("utf-8")
    tmp = path.parent / f"{path.name}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        if tmp.exists():
            tmp.unlink()
        raise
    os.replace(tmp, path)
    path.chmod(0o600)
    leaked = path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO)
    if leaked:
        path.unlink()
        raise BackendError(
            f"refusing to leave group/world-accessible config: {path} "
            f"(mode={oct(path.stat().st_mode & 0o777)})"
        )
    return path


def _redact(value: str) -> str:
    """Mask all but the last 4 chars of a secret."""
    if len(value) <= 4:
        return "***"
    return f"***{value[-4:]}"


def effective_config_report(config: Config) -> list[dict[str, Any]]:
    """Return rows of `{key, value, origin}` for the `/config` command."""
    redacted_keys = {"hiker.token"}
    snapshot: dict[str, Any] = {
        "hiker.token": config.hiker_token,
        "hiker.proxy": config.hiker_proxy,
        "output_dir": str(config.output_dir),
        "db_path": str(config.db_path),
        "cli_history_path": str(config.cli_history_path),
    }
    rows: list[dict[str, Any]] = []
    for key, value in snapshot.items():
        display: Any
        if value is None:
            display = None
        elif key in redacted_keys:
            display = _redact(str(value))
        else:
            display = value
        rows.append(
            {
                "key": key,
                "value": display,
                "origin": config.sources.get(key, "default"),
            }
        )
    return rows
