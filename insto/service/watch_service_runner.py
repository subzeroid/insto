"""Private launchd entry point for the persistent watch executor."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import stat
import sys
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from insto._redact import redact_secrets, register_secret
from insto.config import BACKEND_AIOGRAPI, BACKEND_FAKE, BACKEND_HIKERAPI, Config, load_config
from insto.exceptions import BackendError

_ENV_KEYS = frozenset(
    {
        "HIKERAPI_TOKEN",
        "HIKERAPI_PROXY",
        "AIOGRAPI_USERNAME",
        "AIOGRAPI_PASSWORD",
        "AIOGRAPI_TOTP_SEED",
        "INSTO_WATCH_WEBHOOK_URL",
    }
)
_PROXY_ENV_KEYS = frozenset(
    {
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
    }
)
_SECRET_ENV_KEYS = frozenset(_ENV_KEYS)
_LOG_NAME = "insto.log"


def _controller() -> Any:
    from insto.service import watch_service

    return watch_service


def _secure_toml(path: Path, *, missing_ok: bool = False) -> dict[str, Any]:
    if missing_ok:
        try:
            path.lstat()
        except FileNotFoundError:
            return {}
    try:
        payload = _controller().read_private_file(path, max_bytes=65536)
    except FileNotFoundError:
        if missing_ok:
            return {}
        raise BackendError("private configuration file does not exist") from None
    try:
        value = tomllib.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        raise BackendError("private configuration file is not valid TOML") from None
    if not isinstance(value, dict):
        raise BackendError("private configuration file has invalid structure")
    return value


def _validate_config_credentials(data: dict[str, Any]) -> None:
    credential_fields = {
        "hikerapi": ("token", "proxy"),
        "hiker": ("token", "proxy"),
        "aiograpi": ("username", "password", "totp_seed"),
    }
    for section_name, fields in credential_fields.items():
        section = data.get(section_name)
        if section is None:
            continue
        if not isinstance(section, dict):
            raise BackendError("service configuration credential section is invalid")
        for field in fields:
            if field not in section:
                continue
            value = section[field]
            if not isinstance(value, str) or "\x00" in value:
                raise BackendError("service configuration credential is invalid")


def read_service_env(path: Path | None) -> dict[str, str]:
    """Read the deliberately small, private ``[env]`` service schema."""
    if path is None:
        return {}
    data = _secure_toml(path)
    if set(data) != {"env"} or not isinstance(data.get("env"), dict):
        raise BackendError("service environment file must contain only an [env] table")
    raw = data["env"]
    if any(key not in _ENV_KEYS for key in raw):
        raise BackendError("service environment file contains an unsupported key")
    if any(not isinstance(value, str) or "\x00" in value for value in raw.values()):
        raise BackendError("service environment values must be NUL-free strings")
    result = dict(raw)
    for key, value in result.items():
        if key in _SECRET_ENV_KEYS:
            register_secret(value)
    return result


def _is_service_setting(key: str) -> bool:
    upper = key.upper()
    return (
        upper.startswith("INSTO_")
        or upper.startswith("HIKERAPI_")
        or upper.startswith("AIOGRAPI_")
        or upper in _PROXY_ENV_KEYS
    )


def _clean_environment(*, home: Path, explicit: dict[str, str]) -> None:
    for key in tuple(os.environ):
        if _is_service_setting(key):
            os.environ.pop(key, None)
    os.environ["INSTO_HOME"] = str(home)
    os.environ.update(explicit)


@contextmanager
def _temporary_service_environment(home: Path, explicit: dict[str, str]) -> Iterator[None]:
    original = dict(os.environ)
    try:
        _clean_environment(home=home, explicit=explicit)
        yield
    finally:
        os.environ.clear()
        os.environ.update(original)


def resolve_service_config(
    home: Path,
    env_file: Path | None,
    *,
    pinned: dict[str, Any] | None = None,
) -> Config:
    """Resolve credentials afresh while preserving install-time operational paths."""
    explicit = read_service_env(env_file)
    with _temporary_service_environment(home, explicit):
        toml_data = _secure_toml(home / "config.toml", missing_ok=True)
        _validate_config_credentials(toml_data)
        config = load_config(toml_data=toml_data)

    pins = pinned or {}
    if "backend" in pins:
        config.backend = str(pins["backend"])
        config.sources["backend"] = "flag"
    for key in ("db_path", "output_dir", "aiograpi_session_path"):
        if key in pins:
            setattr(config, key, Path(str(pins[key])))

    _validate_resolved_config(config)
    return config


def _validate_resolved_config(config: Config) -> None:
    if config.backend == BACKEND_HIKERAPI and not config.hiker_token:
        raise BackendError("required HikerAPI credential is not configured")
    if config.backend == BACKEND_AIOGRAPI and (
        not config.aiograpi_username or not config.aiograpi_password
    ):
        raise BackendError("required aiograpi credentials are not configured")
    if config.backend not in {BACKEND_HIKERAPI, BACKEND_AIOGRAPI, BACKEND_FAKE}:
        raise BackendError("service manifest selects an unsupported backend")
    if config.watch_webhook_url:
        from insto.service.watch_webhook import validate_webhook_url

        validate_webhook_url(config.watch_webhook_url)


def load_home_config(home: Path, toml_data: dict[str, Any] | None = None) -> Config:
    """Resolve a home's configuration like the service runner, without env-file secrets.

    Relative paths resolve against the home, which is the service's WorkingDirectory;
    the bridge's own working directory is never a base for a CLI home's paths. The
    one exception is an explicit relative ``aiograpi.session_path``: ``load_config``
    already resolves it against the process cwd before this function sees it
    (pre-existing runner behaviour, irrelevant for hikerapi homes).
    """
    with _temporary_service_environment(home, {}):
        data = (
            _secure_toml(home / "config.toml", missing_ok=False) if toml_data is None else toml_data
        )
        _validate_config_credentials(data)
        config = load_config(toml_data=data)
    _validate_resolved_config(config)
    for key in ("db_path", "output_dir", "aiograpi_session_path"):
        value = getattr(config, key)
        if isinstance(value, Path):
            expanded = value.expanduser()
            setattr(config, key, expanded if expanded.is_absolute() else home / expanded)
    return config


def _validate_log_file(fd: int, path: Path) -> None:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise BackendError(f"service log is not a regular file: {path.name}")
    if info.st_uid != os.getuid():
        raise BackendError(f"service log is not owned by the current user: {path.name}")
    if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise BackendError(f"service log permissions are not private: {path.name}")


class _PrivateRotatingHandler(RotatingFileHandler):
    def _open(self) -> Any:
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NONBLOCK
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd: int | None = None
        try:
            fd = os.open(self.baseFilename, flags, 0o600)
            _validate_log_file(fd, Path(self.baseFilename))
            return os.fdopen(fd, "a", encoding=self.encoding or "utf-8")
        except OSError as exc:
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)
            raise BackendError("refusing unsafe service log file") from exc
        except BaseException:
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)
            raise

    def doRollover(self) -> None:  # noqa: N802 - stdlib override
        base = Path(self.baseFilename)
        _validate_rotated_logs(base.parent, self.backupCount)
        super().doRollover()

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802 - stdlib override
        error = sys.exception()
        if error is not None:
            raise error
        super().handleError(record)


def _validate_log_dir(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        path.mkdir(mode=0o700, parents=True)
        info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
    ):
        raise BackendError("refusing unsafe service log directory")


def _validate_rotated_logs(log_dir: Path, backup_count: int) -> None:
    for index in range(1, backup_count + 1):
        candidate = log_dir / f"{_LOG_NAME}.{index}"
        if not os.path.lexists(candidate):
            continue
        info = candidate.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
        ):
            raise BackendError("refusing unsafe rotated service log")


def setup_service_logging(
    log_dir: Path,
    *,
    max_bytes: int | None = None,
    backup_count: int | None = None,
) -> logging.Logger:
    from insto.cli import LOG_BACKUP_COUNT, LOG_MAX_BYTES, RedactingFormatter

    _validate_log_dir(log_dir)
    selected_backup_count = LOG_BACKUP_COUNT if backup_count is None else backup_count
    _validate_rotated_logs(log_dir, selected_backup_count)
    logger = logging.getLogger("insto.service.runner")
    close_service_logging(logger)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = _PrivateRotatingHandler(
        log_dir / _LOG_NAME,
        maxBytes=LOG_MAX_BYTES if max_bytes is None else max_bytes,
        backupCount=selected_backup_count,
        encoding="utf-8",
    )
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(handler)
    return logger


def close_service_logging(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


def _load_runner_manifest(path: Path) -> tuple[dict[str, Any], Path]:
    if not path.is_absolute():
        raise BackendError("service manifest path must be absolute")
    home = path.parent.parent.parent
    paths = _controller().service_paths(home)
    if os.path.abspath(path) != os.path.abspath(paths.manifest):
        raise BackendError("service manifest path is not canonical")
    manifest = _controller().read_manifest(paths)
    if Path(manifest["config_home"]) != home:
        raise BackendError("service manifest config home does not match its location")
    return manifest, home


async def _run_daemon(config: Config, log: logging.Logger, *, output: Any = None) -> int:
    from insto.cli import _run_watch_daemon

    # The CLI function owns the scheduler; keeping the dynamic call here also
    # avoids coupling this private runner to its callback's precise type alias.
    return int(await _run_watch_daemon(config, log, output=output))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m insto.service.watch_service_runner")
    parser.add_argument("manifest", type=Path)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1

    try:
        manifest, home = _load_runner_manifest(args.manifest)
    except Exception:
        print("watch service startup failed: unsafe service metadata", file=sys.stderr)
        return 1

    original_environment = dict(os.environ)
    logger: logging.Logger | None = None
    try:
        paths = _controller().service_paths(home)
        logger = setup_service_logging(paths.log_dir)
        raw_env_file = manifest["env_file"]
        env_file = Path(raw_env_file) if raw_env_file is not None else None
        config = resolve_service_config(home, env_file, pinned=manifest)
        _clean_environment(home=home, explicit={"INSTO_BACKEND": config.backend})
        logger.info("watch service starting")
        rc = asyncio.run(_run_daemon(config, logger, output=logger.info))
        logger.info("watch service stopped with status %d", rc)
        return rc
    except Exception as exc:
        if logger is not None:
            logger.error("watch service failed: %s", redact_secrets(str(exc)))
        else:
            print("watch service startup failed: unsafe logging destination", file=sys.stderr)
        return 1
    finally:
        if logger is not None:
            close_service_logging(logger)
        os.environ.clear()
        os.environ.update(original_environment)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "close_service_logging",
    "load_home_config",
    "main",
    "read_service_env",
    "resolve_service_config",
    "setup_service_logging",
]
