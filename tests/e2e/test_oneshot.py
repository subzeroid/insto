"""E2E #1: one-shot `insto @alice -c info` via subprocess.

A full subprocess invocation gives us proof that argparse, lazy backend
selection (`INSTO_BACKEND=fake`), config precedence, and the `_format_error`
path all line up the way the user sees them. The fake backend keeps the
test offline.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

PYTHON = sys.executable


def _run_insto(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [PYTHON, "-m", "insto", *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def test_oneshot_info_renders_profile_to_stdout(insto_env: dict[str, str]) -> None:
    """`insto @alice -c info` exits 0 and the profile is written to stdout."""
    result = _run_insto(["@alice", "-c", "info"], env=insto_env)

    assert result.returncode == 0, (
        f"insto failed (rc={result.returncode}); stderr={result.stderr!r}"
    )
    out = result.stdout
    # Key fields the rich panel renders (label or value):
    assert "alice" in out
    assert "Alice Example" in out
    assert "fake bio for e2e tests" in out


def test_oneshot_info_with_json_writes_versioned_envelope(
    insto_env: dict[str, str], tmp_path: Path
) -> None:
    """`--json` writes `output/<user>/info.json` with `_schema: insto.v1`."""
    result = _run_insto(["@alice", "-c", "info", "--json"], env=insto_env)
    assert result.returncode == 0, (
        f"insto failed (rc={result.returncode}); stderr={result.stderr!r}"
    )

    output_dir = Path(insto_env["INSTO_OUTPUT_DIR"])
    info_file = output_dir / "alice" / "info.json"
    assert info_file.exists(), f"expected {info_file} to be written"

    payload = json.loads(info_file.read_text(encoding="utf-8"))
    assert payload["_schema"] == "insto.v1"
    assert payload["command"] == "info"
    assert payload["target"] in ("alice", "@alice")
    profile = payload["data"]["profile"]
    assert profile["username"] == "alice"
    assert profile["full_name"] == "Alice Example"
