"""Real isolated Python child processes exercise the one-request transport."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HELLO = b'{"protocol_version":1,"request_id":"child","operation":"hello","params":{}}\n'


@pytest.mark.parametrize(
    "raw,code",
    [(HELLO, None), (b"not-json\n", "invalid_request"), (b"x" * 65537, "invalid_request")],
    ids=["hello", "malformed", "oversized"],
)
def test_isolated_process(raw: bytes, code: str | None, tmp_path: Path) -> None:
    state = tmp_path / "must-not-exist"
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-m", "insto.desktop"],
        input=raw,
        capture_output=True,
        timeout=10,
        cwd=tmp_path,
        env={**os.environ, "INSTO_HOME": str(state)},
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == b""
    assert result.stdout.count(b"\n") == 1
    response = json.loads(result.stdout)
    if code:
        assert response["error"]["code"] == code
        assert response["request_id"] is None
    else:
        assert response["result"]["capabilities"] == ["hello"]
    assert not state.exists()


def test_hello_never_imports_sdks(tmp_path: Path) -> None:
    script = (
        "import runpy, sys; runpy.run_module('insto.desktop', run_name='__main__'); "
        "assert not any(n.split('.')[0] in {'hikerapi', 'aiograpi'} for n in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script],
        input=HELLO,
        capture_output=True,
        cwd=tmp_path,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == b""
    assert json.loads(result.stdout)["result"]["capabilities"] == ["hello"]


def test_hello_never_opens_config_runtime_or_database(tmp_path: Path) -> None:
    # Patch the real boundaries before dispatch is imported, covering future
    # direct imports as well as calls through these modules.
    script = """
import asyncio
import sqlite3
import sys

import insto.config as config
import insto.service.history as history
import insto.service.runtime as runtime

calls = []

def forbidden(*args, **kwargs):
    calls.append(True)
    raise AssertionError("hello must not initialize application state")

sqlite3.connect = forbidden
history.HistoryStore.__init__ = forbidden
config.load_config = forbidden
runtime.open_runtime = forbidden

from insto.desktop.dispatch import handle

sys.stdout.buffer.write(asyncio.run(handle(sys.stdin.buffer.read())))
assert not calls, "a forbidden call was attempted, even if its exception was caught"
"""
    state = tmp_path / "must-not-exist"
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script],
        input=HELLO,
        capture_output=True,
        cwd=tmp_path,
        env={**os.environ, "INSTO_HOME": str(state)},
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == b""
    assert json.loads(result.stdout)["result"]["capabilities"] == ["hello"]
    assert not state.exists()


def test_parent_must_close_stdin_and_can_cancel(tmp_path: Path) -> None:
    with subprocess.Popen(
        [sys.executable, "-I", "-B", "-m", "insto.desktop"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=tmp_path,
    ) as child:
        assert child.stdin is not None
        child.stdin.write(HELLO)
        child.stdin.flush()
        try:
            with pytest.raises(subprocess.TimeoutExpired):
                child.wait(timeout=0.5)
        finally:
            child.kill()
        stdout, stderr = child.communicate(timeout=10)
        assert stdout == b"" and stderr == b""
