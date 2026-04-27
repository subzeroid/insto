"""E2E test wiring: every test in `tests/e2e/` runs against the fake backend.

The fixtures here do two things:

1. Mark each test with `pytest.mark.e2e` so the suite can be filtered with
   `-m e2e` / `-m "not e2e"`.
2. Build the env block (`INSTO_BACKEND=fake`, isolated `INSTO_HOME`,
   `HIKERAPI_TOKEN`, etc.) that both subprocess and in-process tests use to
   talk to the fake backend.

In-process tests (`test_repl_session.py`, `test_watch_tick.py`) call
`monkeypatch.setenv` from the fixture so the same env reaches `make_backend`.
The subprocess test (`test_oneshot.py`) reuses the same dict via `env=`.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Auto-mark every test in `tests/e2e/` with `@pytest.mark.e2e`."""
    e2e_root = Path(__file__).parent.resolve()
    for item in items:
        if Path(str(item.fspath)).resolve().is_relative_to(e2e_root):
            item.add_marker(pytest.mark.e2e)


@pytest.fixture
def insto_env(tmp_path: Path) -> dict[str, str]:
    """Build the env block that selects the fake backend and isolates state."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    output = tmp_path / "output"
    output.mkdir(parents=True, exist_ok=True)
    db = tmp_path / "store.db"
    env = dict(os.environ)
    env.update(
        {
            "INSTO_BACKEND": "fake",
            "INSTO_HOME": str(home),
            "INSTO_OUTPUT_DIR": str(output),
            "INSTO_DB_PATH": str(db),
            "HIKERAPI_TOKEN": "fake-token-for-tests",
            "PYTHONUNBUFFERED": "1",
            "NO_COLOR": "1",
        }
    )
    return env


@pytest.fixture
def in_process_env(
    insto_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> Iterator[dict[str, str]]:
    """Apply `insto_env` to the current process via monkeypatch."""
    for key, value in insto_env.items():
        monkeypatch.setenv(key, value)
    yield insto_env
