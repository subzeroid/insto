# Contributing to insto

Thanks for considering a contribution. This document covers the dev workflow.

## Setup

```bash
git clone git@github.com:subzeroid/insto.git
cd insto
uv sync --extra dev
```

Python 3.11+ required (we use `dataclass(slots=True)`, `X | Y` unions, `datetime.fromisoformat` with `Z`).

## Running tests

```bash
uv run pytest                                    # all tests
uv run pytest -k hiker                           # subset
uv run pytest --cov=insto --cov-report=term-missing
```

The suite is fully offline — no real HikerAPI or Instagram calls. The single live smoke flow is documented in `CLAUDE.md` and is gated by an explicit `HIKERAPI_TOKEN` env var.

Coverage targets: keep pure-logic modules at 100% (`models`, `_redact`, `exceptions`, mappers). Everything else: 90%+ on touched code.

## Lint + types

```bash
uv run ruff check insto tests
uv run ruff format --check insto tests
uv run mypy insto
```

CI runs the same three; PRs blocked on the strict mypy gate.

## Commit conventions

We use [Conventional Commits](https://www.conventionalcommits.org/). Allowed types:

- `feat:` — new user-visible behavior (changelog "Added")
- `fix:` — bug fix (changelog "Fixed")
- `perf:` — performance only (changelog "Performance")
- `refactor:` — restructure without behavior change (changelog "Changed")
- `docs:` — documentation only
- `build:` / `ci:` / `chore:` / `test:` / `style:` — hidden from changelog

PR titles follow the same shape and are CI-checked. release-please assembles `CHANGELOG.md` and bumps the version automatically when commits land on `main`.

## Project layout

```
insto/
├── _redact.py              # secret redaction (used everywhere we output)
├── _version.py             # single source of version truth
├── cli.py                  # one-shot CLI + setup wizard + _format_error
├── repl.py                 # prompt_toolkit REPL + slash completer
├── config.py               # config precedence: flag > env > toml
├── exceptions.py           # backend error taxonomy
├── models.py               # DTOs (Profile, Post, Story, Quota, ...)
├── ui/                     # banner, theme, render helpers
├── backends/               # OSINTBackend ABC, HikerBackend, _retry, _cdn
├── service/                # facade, history, analytics, exporter, watch
└── commands/               # one file per group (target/profile/media/...)
tests/
├── fakes.py                # FakeBackend with per-method error injection
├── fixtures/hiker/         # frozen HikerAPI dict responses per access state
├── e2e/                    # subprocess + prompt_toolkit pty tests
└── test_*.py               # one per module
```

## Adding a command

1. Pick the right module under `insto/commands/`.
2. Use `@command("name", "help text", csv=..., add_args=...)` from `_base.py`.
3. Decorate with `@with_target` (gives you `username: str`) or `@with_pk` (gives you `pk: str`).
4. To accept inline target on the command line (`/info instagram`), pass `add_args=add_target_arg` (or compose with your own).
5. Return the value the REPL should echo; the dispatcher handles JSON / CSV / Maltego export.

Add tests in the matching `tests/test_commands_*.py`.

## Adding a backend

1. New file under `insto/backends/`.
2. Subclass `OSINTBackend` from `_base.py`. Implement every abstract async iterator + `resolve_target` + `get_quota`.
3. Wrap SDK calls in `@self._apply_retry` so `RateLimited` / `Transient` are retried; surface domain errors from the taxonomy in `exceptions.py` (no naked SDK exceptions above the backend layer).
4. Register in `backends/__init__.py:make_backend()` with a lazy import — the SDK gets imported only when the user picks that backend.

## Releasing

Maintainer-only.

1. Land Conventional Commits on `main`.
2. release-please opens a release PR with a `CHANGELOG.md` bump + version bump.
3. Merge the release PR. release-please tags `vX.Y.Z`.
4. The `release.yml` workflow picks up the tag, builds, and publishes to PyPI via trusted-publishing.
