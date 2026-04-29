"""Tests for insto.cli: parser, setup wizard, completion, logging, _format_error."""

from __future__ import annotations

import logging
import stat
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from insto import cli as cli_mod
from insto import config as cfgmod
from insto.cli import (
    LOG_FILENAME,
    SETUP_HINT,
    RedactingFormatter,
    _format_error,
    _print_completion,
    _run_setup,
    build_parser,
    setup_logging,
)
from insto.commands import COMMANDS, CommandUsageError
from insto.config import config_file_path, load_config
from insto.exceptions import (
    AuthInvalid,
    BackendError,
    Banned,
    PostNotFound,
    PostPrivate,
    ProfileBlocked,
    ProfileDeleted,
    ProfileNotFound,
    ProfilePrivate,
    QuotaExhausted,
    RateLimited,
    SchemaDrift,
    Transient,
)


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    monkeypatch.setenv(cfgmod.CONFIG_HOME_ENV, str(tmp_path / ".insto"))
    for var in (cfgmod.ENV_TOKEN, cfgmod.ENV_PROXY, cfgmod.ENV_OUTPUT_DIR, cfgmod.ENV_DB_PATH):
        monkeypatch.delenv(var, raising=False)
    yield tmp_path
    # Detach our log handlers to not bleed across tests.
    insto_logger = logging.getLogger("insto")
    for handler in list(insto_logger.handlers):
        insto_logger.removeHandler(handler)
        handler.close()


def _mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def test_parser_defaults_no_args() -> None:
    args = build_parser().parse_args([])
    assert args.target is None
    assert args.cmd_argv is None
    assert args.print_completion is None
    assert args.interactive is False
    assert args.verbose is False
    assert args.debug is False


def test_parser_setup_positional() -> None:
    args = build_parser().parse_args(["setup"])
    assert args.target == "setup"
    assert args.cmd_argv is None


def test_parser_oneshot_target_and_cmd() -> None:
    args = build_parser().parse_args(["@ferrari", "-c", "info", "--json"])
    assert args.target == "@ferrari"
    assert args.cmd_argv == ["info", "--json"]


def test_parser_oneshot_remainder_preserves_flags() -> None:
    args = build_parser().parse_args(["@x", "-c", "posts", "--limit", "5"])
    assert args.cmd_argv == ["posts", "--limit", "5"]


def test_parser_print_completion_choice() -> None:
    args = build_parser().parse_args(["--print-completion", "bash"])
    assert args.print_completion == "bash"


def test_parser_verbose_and_debug() -> None:
    args = build_parser().parse_args(["--debug", "@x", "-c", "info"])
    assert args.debug is True
    args = build_parser().parse_args(["-v"])
    assert args.verbose is True


def test_parser_proxy_flag() -> None:
    args = build_parser().parse_args(["--proxy", "socks5h://127.0.0.1:9050"])
    assert args.proxy == "socks5h://127.0.0.1:9050"


def test_parser_hiker_token_flag() -> None:
    """`--hiker-token` must override env/toml per spec §8."""
    args = build_parser().parse_args(["--hiker-token", "abc123"])
    assert args.hiker_token == "abc123"
    args = build_parser().parse_args([])
    assert args.hiker_token is None


def test_parsed_cmd_resolves_to_command_spec() -> None:
    """`-c <name> ...` must round-trip through the command parser to a CommandSpec."""
    from insto.commands._base import parse_command_line

    args = build_parser().parse_args(["@ferrari", "-c", "info", "--json"])
    assert args.cmd_argv is not None
    line = " ".join(args.cmd_argv)
    spec, parsed = parse_command_line(line)
    assert spec.name == "info"
    assert spec is COMMANDS["info"]
    assert parsed.json == ""


# ---------------------------------------------------------------------------
# _format_error
# ---------------------------------------------------------------------------


def test_format_error_profile_not_found() -> None:
    assert _format_error(ProfileNotFound("ferrari")) == "profile not found: @ferrari"


def test_format_error_profile_private() -> None:
    assert _format_error(ProfilePrivate("ferrari")) == "profile is private: @ferrari"


def test_format_error_profile_blocked() -> None:
    assert _format_error(ProfileBlocked("x")) == "profile has blocked us: @x"


def test_format_error_profile_deleted() -> None:
    assert _format_error(ProfileDeleted("x")) == "profile is deleted: @x"


def test_format_error_post_not_found() -> None:
    assert _format_error(PostNotFound("MEDIA1")) == "post not found: MEDIA1"


def test_format_error_post_private() -> None:
    assert _format_error(PostPrivate("MEDIA1")) == "post is private: MEDIA1"


def test_format_error_auth_invalid_mentions_setup() -> None:
    msg = _format_error(AuthInvalid())
    assert "auth invalid" in msg
    assert "insto setup" in msg


def test_format_error_quota_exhausted() -> None:
    assert "quota exhausted" in _format_error(QuotaExhausted())


def test_format_error_rate_limited() -> None:
    assert _format_error(RateLimited(7.5)) == "rate limited — retry after 7.5s"


def test_format_error_schema_drift() -> None:
    msg = _format_error(SchemaDrift("/v1/profile", "username"))
    assert "/v1/profile" in msg
    assert "username" in msg


def test_format_error_transient() -> None:
    assert _format_error(Transient("network blip")) == "transient backend error: network blip"


def test_format_error_banned() -> None:
    assert "banned" in _format_error(Banned())


def test_format_error_generic_backend() -> None:
    assert _format_error(BackendError("weird")) == "backend error: weird"


def test_format_error_command_usage() -> None:
    assert _format_error(CommandUsageError("missing target")) == "usage: missing target"


def test_format_error_unknown_exception_type() -> None:
    assert "RuntimeError" in _format_error(RuntimeError("boom"))


def test_format_error_redacts_env_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HIKERAPI_TOKEN", "supersecrettoken1234")
    err = BackendError("upstream said: token=supersecrettoken1234 expired")
    out = _format_error(err)
    assert "supersecrettoken1234" not in out
    assert "***" in out


def test_format_error_redacts_query_string() -> None:
    err = BackendError("fetch failed: https://cdn.example.com/x.jpg?signature=abcDEFsig123&size=1")
    out = _format_error(err)
    assert "abcDEFsig123" not in out
    assert "signature=***" in out


# ---------------------------------------------------------------------------
# Setup wizard
# ---------------------------------------------------------------------------


def _scripted_prompt(answers: list[str]) -> Callable[[str], str]:
    iterator = iter(answers)

    def prompt(_text: str) -> str:
        return next(iterator)

    return prompt


def test_setup_writes_0600_with_token_and_proxy(capsys: pytest.CaptureFixture[str]) -> None:
    rc = _run_setup(
        prompt=_scripted_prompt(
            [
                "",  # backend (Enter = default 'hiker')
                "tok-1234567890",  # token
                "./out-x",  # output_dir
                "/tmp/insto-store.db",  # db_path
                "socks5h://127.0.0.1:9050",  # proxy
            ]
        )
    )
    assert rc == 0
    path = config_file_path()
    assert path.exists()
    assert _mode(path) == 0o600
    leaked = path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO)
    assert leaked == 0
    cfg = load_config()
    assert cfg.hiker_token == "tok-1234567890"
    assert cfg.hiker_proxy == "socks5h://127.0.0.1:9050"
    # Setup wizard resolves user input to absolute paths so behaviour does
    # not depend on the CWD where `insto` is later invoked from.
    assert cfg.output_dir.is_absolute()
    assert cfg.output_dir.name == "out-x"
    # Path("/tmp/...").resolve() expands the macOS /tmp -> /private/tmp symlink.
    assert cfg.db_path == Path("/tmp/insto-store.db").resolve()


def test_setup_keeps_existing_token_when_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    cfgmod.write_config({"hiker": {"token": "existing-token-9999"}})
    # Five Enters: backend / token / output_dir / db_path / proxy — all default.
    rc = _run_setup(prompt=_scripted_prompt(["", "", "", "", ""]))
    assert rc == 0
    cfg = load_config()
    assert cfg.hiker_token == "existing-token-9999"


def test_setup_clear_proxy_with_dash() -> None:
    cfgmod.write_config(
        {"hiker": {"token": "tok-abcd1234", "proxy": "http://prev:1"}, "output_dir": "./o"}
    )
    rc = _run_setup(prompt=_scripted_prompt(["", "", "", "", "-"]))
    assert rc == 0
    cfg = load_config()
    assert cfg.hiker_proxy is None


def test_setup_without_token_emits_hint(capsys: pytest.CaptureFixture[str]) -> None:
    rc = _run_setup(prompt=_scripted_prompt(["", "", "", "", ""]))
    assert rc == 0
    out = capsys.readouterr().out
    assert SETUP_HINT in out


# ---------------------------------------------------------------------------
# Shell completion
# ---------------------------------------------------------------------------


def test_print_completion_without_shtab(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(sys.modules, "shtab", None)
    rc = _print_completion(build_parser(), "bash")
    err = capsys.readouterr().err
    assert rc == 1
    assert "shell completion requires" in err
    assert "insto[completion]" in err


def test_print_completion_with_shtab(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import types

    fake = types.ModuleType("shtab")

    def fake_complete(parser: object, shell: str = "bash") -> str:
        assert shell in ("bash", "zsh")
        return f"# completion script for {shell}"

    fake.complete = fake_complete  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "shtab", fake)
    rc = _print_completion(build_parser(), "zsh")
    out = capsys.readouterr().out
    assert rc == 0
    assert "completion script for zsh" in out


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def test_setup_logging_creates_0600_file(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    log_path = setup_logging(logging.INFO, log_dir=log_dir)
    assert log_path == log_dir / LOG_FILENAME
    log = logging.getLogger("insto.test")
    log.info("hello world")
    for handler in logging.getLogger("insto").handlers:
        handler.flush()
    assert log_path.exists()
    assert _mode(log_path) == 0o600
    assert _mode(log_dir) == 0o700


def test_setup_logging_debug_records_debug_lines(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    log_path = setup_logging(logging.DEBUG, log_dir=log_dir)
    log = logging.getLogger("insto.test")
    log.debug("debug line one")
    log.info("info line one")
    for handler in logging.getLogger("insto").handlers:
        handler.flush()
    contents = log_path.read_text()
    assert "debug line one" in contents
    assert "info line one" in contents


def test_setup_logging_info_skips_debug(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    log_path = setup_logging(logging.INFO, log_dir=log_dir)
    log = logging.getLogger("insto.test")
    log.debug("hidden debug")
    log.info("visible info")
    for handler in logging.getLogger("insto").handlers:
        handler.flush()
    contents = log_path.read_text()
    assert "hidden debug" not in contents
    assert "visible info" in contents


def test_log_redaction_strips_secrets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HIKERAPI_TOKEN", "log-token-1234567890")
    log_dir = tmp_path / "logs"
    log_path = setup_logging(logging.DEBUG, log_dir=log_dir)
    log = logging.getLogger("insto.test")
    log.warning("dumping token=log-token-1234567890 for debugging")
    log.warning("cdn url: https://cdn.x/?signature=abcDEFsig987")
    for handler in logging.getLogger("insto").handlers:
        handler.flush()
    contents = log_path.read_text()
    assert "log-token-1234567890" not in contents
    assert "abcDEFsig987" not in contents
    assert "***" in contents


def test_log_rotation_5mb_threshold(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    log_path = setup_logging(logging.INFO, log_dir=log_dir)
    log = logging.getLogger("insto.test")
    big = "x" * 1024
    # Write >5MB so the rotating handler triggers a rollover.
    for _ in range(6 * 1024):
        log.info(big)
    for handler in logging.getLogger("insto").handlers:
        handler.flush()
    rotated = log_dir / f"{LOG_FILENAME}.1"
    assert rotated.exists()
    assert log_path.exists()
    assert log_path.stat().st_size <= cli_mod.LOG_MAX_BYTES * 1.1


def test_redacting_formatter_directly() -> None:
    fmt = RedactingFormatter("%(message)s")
    record = logging.LogRecord(
        name="insto.test",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="cdn https://cdn.x/p?signature=abcDEF1234sig and more",
        args=None,
        exc_info=None,
    )
    out = fmt.format(record)
    assert "abcDEF1234sig" not in out
    assert "signature=***" in out


# ---------------------------------------------------------------------------
# main() integration: hint when no token
# ---------------------------------------------------------------------------


def test_main_oneshot_no_token_prints_hint(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = cli_mod.main(["@ferrari", "-c", "info"])
    err = capsys.readouterr().err
    assert rc == 1
    assert SETUP_HINT in err


def test_main_no_args_no_token_prints_hint_and_exits(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = cli_mod.main([])
    err = capsys.readouterr().err
    assert SETUP_HINT in err
    assert rc == 1


def test_main_setup_invokes_wizard(monkeypatch: pytest.MonkeyPatch) -> None:
    called: dict[str, Any] = {}

    def fake_run_setup(*, non_interactive: bool = False) -> int:
        called["yes"] = True
        called["non_interactive"] = non_interactive
        return 0

    monkeypatch.setattr(cli_mod, "_run_setup", fake_run_setup)
    rc = cli_mod.main(["setup"])
    assert rc == 0
    assert called.get("yes") is True
    assert called.get("non_interactive") is False


def test_main_setup_non_interactive_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run_setup(*, non_interactive: bool = False) -> int:
        captured["non_interactive"] = non_interactive
        return 0

    monkeypatch.setattr(cli_mod, "_run_setup", fake_run_setup)
    rc = cli_mod.main(["--non-interactive", "setup"])
    assert rc == 0
    assert captured["non_interactive"] is True


def test_main_print_completion_dispatches(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str] = {}

    def fake_print_completion(parser: object, shell: str) -> int:
        captured["shell"] = shell
        return 0

    monkeypatch.setattr(cli_mod, "_print_completion", fake_print_completion)
    rc = cli_mod.main(["--print-completion", "zsh"])
    assert rc == 0
    assert captured["shell"] == "zsh"
