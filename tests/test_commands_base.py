"""Tests for `insto.commands._base`: registry, parsing, global flags, helpers.

Each test exercises one slice of the foundation laid in Task 15:

- The `@command` decorator registers in `COMMANDS`.
- The shared `--json/--csv/--limit/--no-download/--yes/--maltego/--output-format`
  parent parser is inherited by every command parser.
- Mutual-exclusion / flat-only rules are enforced by `validate_global_flags`.
- `parse_command_line` strips leading `/`, dispatches to the right spec,
  raises `CommandUsageError` (never `SystemExit`) for unknown / malformed
  input, and emits did-you-mean suggestions.
- `resolve_export_dest` maps `-` to `sys.stdout.buffer` and other strings
  to `Path`.
- `download_or_print_url` honours the `no_download` flag.
- `with_target` / `with_pk` resolve from positional args or session state.
"""

from __future__ import annotations

import argparse
import io
import sys
from collections.abc import Generator
from pathlib import Path

import httpx
import pytest

from insto.commands._base import (
    COMMANDS,
    CommandContext,
    CommandSpec,
    CommandUsageError,
    Session,
    _NonExitingParser,
    build_parser_for,
    command,
    dispatch,
    download_or_print_url,
    parse_command_line,
    resolve_export_dest,
    validate_global_flags,
    with_pk,
    with_target,
)
from insto.config import Config
from insto.models import Profile
from insto.service.facade import OsintFacade
from insto.service.history import HistoryStore
from tests.fakes import FakeBackend


@pytest.fixture
def history(tmp_path: Path) -> Generator[HistoryStore, None, None]:
    s = HistoryStore(tmp_path / "store.db")
    yield s
    s.close()


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(output_dir=tmp_path / "output", db_path=tmp_path / "store.db")


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend(
        profiles={"42": Profile(pk="42", username="alice", access="public")},
    )


@pytest.fixture
def facade(backend: FakeBackend, history: HistoryStore, config: Config) -> OsintFacade:
    return OsintFacade(backend=backend, history=history, config=config)


@pytest.fixture
def session() -> Session:
    return Session()


# ---------------------------------------------------------------------------
# Registry / decorator
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_registry() -> Generator[dict[str, CommandSpec], None, None]:
    """Snapshot/restore COMMANDS so test registrations don't leak."""
    snapshot = COMMANDS.copy()
    yield COMMANDS
    COMMANDS.clear()
    COMMANDS.update(snapshot)


def test_command_decorator_registers(
    isolated_registry: dict[str, CommandSpec],
) -> None:
    @command("ping", "ping help", csv=False)
    async def ping_cmd(ctx: CommandContext) -> str:
        return "pong"

    assert "ping" in isolated_registry
    spec = isolated_registry["ping"]
    assert spec.name == "ping"
    assert spec.help == "ping help"
    assert spec.fn is ping_cmd
    assert spec.csv is False


def test_command_decorator_carries_add_args(
    isolated_registry: dict[str, CommandSpec],
) -> None:
    def add_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("foo")

    @command("with-args", "h", add_args=add_args)
    async def fn(ctx: CommandContext) -> None:
        return None

    parser = build_parser_for(isolated_registry["with-args"])
    args = parser.parse_args(["bar"])
    assert args.foo == "bar"
    # Global flags inherited by parent parser:
    assert args.json is None
    assert args.csv is None
    assert args.limit is None
    assert args.no_download is False
    assert args.yes is False


def test_target_group_registered() -> None:
    # /target /current /clear come from importing insto.commands.target.
    import insto.commands  # noqa: F401  (triggers module imports)

    assert "target" in COMMANDS
    assert "current" in COMMANDS
    assert "clear" in COMMANDS


# ---------------------------------------------------------------------------
# Global flags inherited via parents=
# ---------------------------------------------------------------------------


@pytest.fixture
def trivial_spec(isolated_registry: dict[str, CommandSpec]) -> CommandSpec:
    @command("ping", "help", csv=False)
    async def fn(ctx: CommandContext) -> None:
        return None

    return isolated_registry["ping"]


def test_global_parser_provides_all_flags(trivial_spec: CommandSpec) -> None:
    parser = build_parser_for(trivial_spec)
    args = parser.parse_args(
        [
            "--json",
            "out.json",
            "--limit",
            "25",
            "--no-download",
            "--yes",
        ]
    )
    assert args.json == "out.json"
    assert args.limit == 25
    assert args.no_download is True
    assert args.yes is True


def test_json_flag_without_value_uses_default_marker(trivial_spec: CommandSpec) -> None:
    parser = build_parser_for(trivial_spec)
    args = parser.parse_args(["--json"])
    # nargs='?' with const='' — empty string signals "use default location"
    assert args.json == ""
    assert args.csv is None


def test_csv_flag_dash_signals_stdout(trivial_spec: CommandSpec) -> None:
    parser = build_parser_for(trivial_spec)
    args = parser.parse_args(["--csv", "-"])
    assert args.csv == "-"


def test_maltego_flag_sets_bool(trivial_spec: CommandSpec) -> None:
    parser = build_parser_for(trivial_spec)
    args = parser.parse_args(["--maltego"])
    assert args.maltego is True


def test_output_format_choice(trivial_spec: CommandSpec) -> None:
    parser = build_parser_for(trivial_spec)
    args = parser.parse_args(["--output-format", "json"])
    assert args.output_format == "json"


def test_output_format_rejects_unknown(trivial_spec: CommandSpec) -> None:
    parser = build_parser_for(trivial_spec)
    with pytest.raises(CommandUsageError):
        parser.parse_args(["--output-format", "xml"])


# ---------------------------------------------------------------------------
# Validation rules (mutual exclusion + flat-only CSV)
# ---------------------------------------------------------------------------


def _ns(**kw: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "json": None,
        "csv": None,
        "maltego": False,
        "output_format": None,
        "limit": None,
        "no_download": False,
        "yes": False,
    }
    base.update(kw)
    return argparse.Namespace(**base)


def test_validate_rejects_json_and_csv_together() -> None:
    with pytest.raises(CommandUsageError, match="mutually exclusive"):
        validate_global_flags("followers", _ns(json="a", csv="b"))


def test_validate_rejects_maltego_with_other_format() -> None:
    with pytest.raises(CommandUsageError, match="--maltego conflicts"):
        validate_global_flags("followers", _ns(maltego=True, output_format="json"))


def test_validate_allows_csv_on_flat_command() -> None:
    validate_global_flags("followers", _ns(csv=""))  # no raise


def test_validate_rejects_csv_on_non_flat_command() -> None:
    with pytest.raises(CommandUsageError, match="cannot be exported as CSV"):
        validate_global_flags("info", _ns(csv=""))


def test_validate_rejects_csv_on_non_flat_via_output_format() -> None:
    with pytest.raises(CommandUsageError, match="cannot be exported as CSV"):
        validate_global_flags("info", _ns(output_format="csv"))


def test_validate_allows_maltego_on_any_command() -> None:
    validate_global_flags("info", _ns(maltego=True))  # no raise
    validate_global_flags("info", _ns(output_format="maltego"))  # no raise


# ---------------------------------------------------------------------------
# parse_command_line
# ---------------------------------------------------------------------------


def test_parse_command_line_strips_leading_slash(
    isolated_registry: dict[str, CommandSpec],
) -> None:
    @command("hello", "h")
    async def fn(ctx: CommandContext) -> None:
        return None

    spec, args = parse_command_line("/hello --limit 5")
    assert spec.name == "hello"
    assert args.limit == 5


def test_parse_command_line_works_without_slash(
    isolated_registry: dict[str, CommandSpec],
) -> None:
    @command("hello", "h")
    async def fn(ctx: CommandContext) -> None:
        return None

    spec, _ = parse_command_line("hello")
    assert spec.name == "hello"


def test_parse_command_line_unknown_command_suggests(
    isolated_registry: dict[str, CommandSpec],
) -> None:
    @command("followers", "list followers")
    async def fn(ctx: CommandContext) -> None:
        return None

    with pytest.raises(CommandUsageError) as excinfo:
        parse_command_line("/folowers")
    assert "did you mean /followers" in str(excinfo.value)


def test_parse_command_line_empty_raises(
    isolated_registry: dict[str, CommandSpec],
) -> None:
    with pytest.raises(CommandUsageError, match="empty command"):
        parse_command_line("   ")


def test_parse_command_line_handles_quoted_args(
    isolated_registry: dict[str, CommandSpec],
) -> None:
    def add_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("text")

    @command("echo", "h", add_args=add_args)
    async def fn(ctx: CommandContext) -> None:
        return None

    _, args = parse_command_line('/echo "hello world"')
    assert args.text == "hello world"


def test_parse_command_line_unbalanced_quotes_raise(
    isolated_registry: dict[str, CommandSpec],
) -> None:
    with pytest.raises(CommandUsageError, match="failed to parse command line"):
        parse_command_line('/anything "broken')


def test_parse_command_line_rejects_csv_on_non_flat(
    isolated_registry: dict[str, CommandSpec],
) -> None:
    @command("info", "h")
    async def fn(ctx: CommandContext) -> None:
        return None

    with pytest.raises(CommandUsageError, match="cannot be exported as CSV"):
        parse_command_line("/info --csv out.csv")


def test_parse_command_line_parser_does_not_exit(
    isolated_registry: dict[str, CommandSpec],
) -> None:
    """argparse error must surface as CommandUsageError, never SystemExit."""

    def add_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--mode", choices=("a", "b"))

    @command("strict", "h", add_args=add_args)
    async def fn(ctx: CommandContext) -> None:
        return None

    with pytest.raises(CommandUsageError):
        parse_command_line("/strict --mode c")


def test_non_exiting_parser_help_does_not_exit(
    isolated_registry: dict[str, CommandSpec],
) -> None:
    parser = _NonExitingParser(prog="x")
    parser.add_argument("--foo")
    with pytest.raises(CommandUsageError):
        parser.parse_args(["--bogus"])


# ---------------------------------------------------------------------------
# dispatch (end-to-end through registry → fn)
# ---------------------------------------------------------------------------


async def test_dispatch_calls_registered_function(
    isolated_registry: dict[str, CommandSpec],
    facade: OsintFacade,
    session: Session,
) -> None:
    seen: dict[str, object] = {}

    @command("noop", "h")
    async def fn(ctx: CommandContext) -> str:
        seen["limit"] = ctx.limit
        seen["yes"] = ctx.yes
        return "ok"

    result = await dispatch("/noop --limit 10 --yes", facade=facade, session=session)
    assert result == "ok"
    assert seen == {"limit": 10, "yes": True}


# ---------------------------------------------------------------------------
# resolve_export_dest
# ---------------------------------------------------------------------------


def test_resolve_export_dest_none() -> None:
    assert resolve_export_dest(None) is None


def test_resolve_export_dest_empty_string_means_default() -> None:
    assert resolve_export_dest("") is None


def test_resolve_export_dest_dash_means_stdout() -> None:
    assert resolve_export_dest("-") is sys.stdout.buffer


def test_resolve_export_dest_path() -> None:
    out = resolve_export_dest("foo/bar.json")
    assert out == Path("foo/bar.json")


# ---------------------------------------------------------------------------
# download_or_print_url
# ---------------------------------------------------------------------------


async def test_download_or_print_url_no_download_prints(
    facade: OsintFacade,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    result = await download_or_print_url(
        facade,
        "https://example.com/x.jpg",
        tmp_path / "x.jpg",
        no_download=True,
    )
    assert result is None
    out = capsys.readouterr().out.strip()
    assert out == "https://example.com/x.jpg"
    assert not (tmp_path / "x.jpg").exists()


async def test_download_or_print_url_streams_when_enabled(
    backend: FakeBackend,
    history: HistoryStore,
    config: Config,
    tmp_path: Path,
) -> None:
    body = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 1024

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=body,
            headers={"content-type": "image/jpeg", "content-length": str(len(body))},
        )

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    facade = OsintFacade(
        backend=backend, history=history, config=config, cdn_client=client
    )
    try:
        out = await download_or_print_url(
            facade,
            "https://scontent.cdninstagram.com/x.jpg",
            tmp_path / "x.jpg",
            no_download=False,
        )
        assert out is not None
        assert out.exists()
        assert out.read_bytes().startswith(b"\xff\xd8")
    finally:
        await facade.aclose()


# ---------------------------------------------------------------------------
# with_target / with_pk decorators
# ---------------------------------------------------------------------------


def _ctx(facade: OsintFacade, session: Session, **flags: object) -> CommandContext:
    args = _ns(**flags)
    return CommandContext(facade=facade, args=args, session=session)


async def test_with_target_uses_session(
    facade: OsintFacade, session: Session
) -> None:
    session.set_target("alice")

    @with_target
    async def fn(ctx: CommandContext, username: str) -> str:
        return username

    result = await fn(_ctx(facade, session))
    assert result == "alice"


async def test_with_target_uses_positional(
    facade: OsintFacade, session: Session
) -> None:
    @with_target
    async def fn(ctx: CommandContext, username: str) -> str:
        return username

    result = await fn(_ctx(facade, session, target="@bob"))
    assert result == "bob"


async def test_with_target_positional_overrides_session(
    facade: OsintFacade, session: Session
) -> None:
    session.set_target("alice")

    @with_target
    async def fn(ctx: CommandContext, username: str) -> str:
        return username

    result = await fn(_ctx(facade, session, target="bob"))
    assert result == "bob"


async def test_with_target_raises_when_unset(
    facade: OsintFacade, session: Session
) -> None:
    @with_target
    async def fn(ctx: CommandContext, username: str) -> str:
        return username

    with pytest.raises(CommandUsageError, match="no target set"):
        await fn(_ctx(facade, session))


async def test_with_pk_resolves_via_facade_cache(
    facade: OsintFacade,
    backend: FakeBackend,
    session: Session,
) -> None:
    session.set_target("alice")

    @with_pk
    async def fn(ctx: CommandContext, pk: str) -> str:
        return pk

    pk = await fn(_ctx(facade, session))
    assert pk == "42"
    # Second call should hit the cache: only one resolve_target in the log.
    pk2 = await fn(_ctx(facade, session))
    assert pk2 == "42"
    resolves = [c for c in backend.request_log if c[0] == "resolve_target"]
    assert len(resolves) == 1


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


def test_session_strips_at_sign() -> None:
    s = Session()
    s.set_target("@alice")
    assert s.target == "alice"


def test_session_clear_drops_target() -> None:
    s = Session()
    s.set_target("alice")
    s.clear()
    assert s.target is None


def test_session_rejects_blank_target() -> None:
    s = Session()
    with pytest.raises(CommandUsageError):
        s.set_target("@")


# ---------------------------------------------------------------------------
# CommandContext properties
# ---------------------------------------------------------------------------


def test_context_output_format_priority(
    facade: OsintFacade, session: Session
) -> None:
    ctx = _ctx(facade, session, output_format="json")
    assert ctx.output_format() == "json"
    ctx = _ctx(facade, session, maltego=True)
    assert ctx.output_format() == "maltego"
    ctx = _ctx(facade, session, json="-")
    assert ctx.output_format() == "json"
    ctx = _ctx(facade, session, csv="x.csv")
    assert ctx.output_format() == "csv"
    assert _ctx(facade, session).output_format() is None


# ---------------------------------------------------------------------------
# Exporter accepts BytesIO destinations (Task 15 requirement)
# ---------------------------------------------------------------------------


def test_exporter_accepts_binary_stream() -> None:
    from insto.service.exporter import to_csv, to_json

    buf = io.BytesIO()
    out = to_json({"a": 1}, command="info", target="alice", dest=buf)
    assert out is None
    assert b'"data"' in buf.getvalue()

    buf2 = io.BytesIO()
    out2 = to_csv(
        [{"username": "bob", "pk": "1"}],
        command="followers",
        target="alice",
        dest=buf2,
    )
    assert out2 is None
    assert buf2.getvalue().startswith(b"username,pk")
