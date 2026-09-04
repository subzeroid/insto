"""POSIX subprocess coverage for the persistent foreground watcher."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from insto.models import Profile
from insto.service.history import HistoryStore

PYTHON = sys.executable

pytestmark = pytest.mark.skipif(os.name != "posix", reason="daemon uses POSIX locks/signals")


@dataclass(frozen=True, slots=True)
class _CapturedRequest:
    method: str
    path: str
    content_type: str | None
    raw_body: bytes
    parsed_body: Any


class _LoopbackWebhookReceiver:
    def __init__(self, endpoint: str, response_body: str) -> None:
        self._endpoint = endpoint
        self._response_body = response_body.encode()
        self._requests: list[_CapturedRequest] = []
        self._requests_lock = threading.Lock()
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                raw_body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                try:
                    parsed_body = json.loads(raw_body)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    parsed_body = None
                receiver._record(
                    _CapturedRequest(
                        method=self.command,
                        path=self.path,
                        content_type=self.headers.get("Content-Type"),
                        raw_body=raw_body,
                        parsed_body=parsed_body,
                    )
                )
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(receiver._response_body)))
                self.end_headers()
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    self.wfile.write(receiver._response_body)

            def log_message(self, _format: str, *_args: object) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.01},
            name="insto-e2e-webhook-receiver",
            daemon=True,
        )
        self._started = False

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}{self._endpoint}"

    def start(self) -> None:
        self._thread.start()
        self._started = True

    def stop(self) -> None:
        if self._started:
            self._server.shutdown()
        self._server.server_close()
        if self._started:
            self._thread.join(timeout=5)

    def requests(self) -> list[_CapturedRequest]:
        with self._requests_lock:
            return list(self._requests)

    def _record(self, request: _CapturedRequest) -> None:
        with self._requests_lock:
            self._requests.append(request)


def _run_insto(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [PYTHON, "-m", "insto", *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def _start_daemon(env: dict[str, str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [PYTHON, "-m", "insto", "watch-daemon"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _stop_daemon(process: subprocess.Popen[str]) -> tuple[str, str]:
    if process.poll() is None:
        process.send_signal(signal.SIGTERM)
    try:
        return process.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        return process.communicate(timeout=5)


def _wait_until(predicate: Callable[[], bool], *, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _remains_true(predicate: Callable[[], bool], *, duration: float = 0.5) -> bool:
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        if not predicate():
            return False
        time.sleep(0.05)
    return predicate()


def _watch_last_ok(db_path: Path) -> int | None:
    with sqlite3.connect(db_path) as connection:
        row = connection.execute("SELECT last_ok FROM watches WHERE user = 'alice'").fetchone()
    return None if row is None else row[0]


def _snapshot_count(db_path: Path) -> int:
    with sqlite3.connect(db_path) as connection:
        return int(connection.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0])


def _make_watch_due(db_path: Path) -> None:
    store = HistoryStore(db_path)
    try:
        spec = store.get_watch("alice")
        assert spec is not None
        assert store.update_watch_state(spec, last_ok=0)
    finally:
        store.close()


def _seed_changed_snapshot(db_path: Path) -> None:
    store = HistoryStore(db_path)
    try:
        previous = Profile(
            pk="1001",
            username="alice",
            access="public",
            full_name="Alice Example",
            biography="private prior biography for daemon webhook e2e",
            follower_count=2048,
            following_count=512,
            media_count=2,
            is_verified=True,
        )
        store.add_snapshot(store.snapshot_from_profile(previous, post_pks=["p1", "p2"]))
    finally:
        store.close()


def _daemon_artifacts(insto_home: Path, stdout: str, stderr: str) -> str:
    parts = [stdout, stderr]
    for log_path in sorted(insto_home.rglob("insto.log*")):
        if log_path.is_file():
            parts.append(log_path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts)


def test_daemon_ticks_rejects_second_owner_and_restarts(
    insto_env: dict[str, str],
) -> None:
    db_path = Path(insto_env["INSTO_DB_PATH"])
    registration = _run_insto(["@alice", "-c", "watch"], insto_env)
    assert registration.returncode == 0, registration.stderr
    assert "insto watch-daemon" in registration.stdout
    _make_watch_due(db_path)

    first = _start_daemon(insto_env)
    try:
        assert _wait_until(lambda: (_watch_last_ok(db_path) or 0) > 0), (
            _stop_daemon(first),
            "first daemon did not persist a successful tick",
        )
        assert _snapshot_count(db_path) >= 1

        contender = _run_insto(["watch-daemon"], insto_env)
        assert contender.returncode != 0
        assert "watch executor already active" in contender.stderr

        time.sleep(0.1)
        first.send_signal(signal.SIGTERM)
        stdout, stderr = first.communicate(timeout=10)
        assert first.returncode == 0, f"stdout={stdout!r}; stderr={stderr!r}"
        settled_count = _snapshot_count(db_path)
        time.sleep(0.1)
        assert _snapshot_count(db_path) == settled_count
    finally:
        if first.poll() is None:
            _stop_daemon(first)

    _make_watch_due(db_path)
    second = _start_daemon(insto_env)
    try:
        assert _wait_until(lambda: _snapshot_count(db_path) > settled_count), (
            _stop_daemon(second),
            "restarted daemon did not perform another tick",
        )
        time.sleep(0.1)
        second.send_signal(signal.SIGTERM)
        stdout, stderr = second.communicate(timeout=10)
        assert second.returncode == 0, f"stdout={stdout!r}; stderr={stderr!r}"
    finally:
        if second.poll() is None:
            _stop_daemon(second)


def test_daemon_delivers_changed_watch_webhook_without_leaking_secrets(
    insto_env: dict[str, str],
) -> None:
    db_path = Path(insto_env["INSTO_DB_PATH"])
    registration = _run_insto(["@alice", "-c", "watch"], insto_env)
    assert registration.returncode == 0, registration.stderr
    _seed_changed_snapshot(db_path)
    _make_watch_due(db_path)

    endpoint_sentinel = f"private-endpoint-{uuid4().hex}"
    response_sentinel = f"private-response-{uuid4().hex}"
    receiver = _LoopbackWebhookReceiver(f"/{endpoint_sentinel}", response_sentinel)
    process: subprocess.Popen[str] | None = None
    try:
        receiver.start()
        daemon_env = dict(insto_env)
        daemon_env["INSTO_WATCH_WEBHOOK_URL"] = receiver.url
        process = _start_daemon(daemon_env)

        def delivery_was_persisted() -> bool:
            return (
                len(receiver.requests()) == 1
                and (_watch_last_ok(db_path) or 0) > 0
                and _snapshot_count(db_path) == 2
            )

        assert _wait_until(delivery_was_persisted), (
            f"requests={receiver.requests()!r}; "
            f"last_ok={_watch_last_ok(db_path)!r}; snapshots={_snapshot_count(db_path)}"
        )

        requests = receiver.requests()
        assert len(requests) == 1
        request = requests[0]
        assert request.method == "POST"
        assert request.path == f"/{endpoint_sentinel}"
        assert request.content_type == "application/json"
        assert request.raw_body

        payload = request.parsed_body
        assert isinstance(payload, dict)
        assert set(payload) == {
            "schema_version",
            "event",
            "event_id",
            "username",
            "observed_at",
            "changes",
            "previous_usernames",
        }
        assert payload["schema_version"] == 1
        assert payload["event"] == "watch.changed"
        assert payload["username"] == "alice"

        event_id = payload["event_id"]
        assert isinstance(event_id, str)
        assert str(UUID(event_id)) == event_id

        observed_at = payload["observed_at"]
        assert isinstance(observed_at, str)
        assert observed_at.endswith("Z")
        observed = datetime.fromisoformat(observed_at.removesuffix("Z") + "+00:00")
        assert observed.tzinfo is UTC

        assert payload["changes"] == {
            "biography": {
                "old": "private prior biography for daemon webhook e2e",
                "new": "fake bio for e2e tests",
            }
        }
        assert payload["previous_usernames"] == []

        stdout, stderr = _stop_daemon(process)
        assert process.returncode == 0, f"stdout={stdout!r}; stderr={stderr!r}"
        assert _remains_true(lambda: len(receiver.requests()) == 1)

        artifacts = _daemon_artifacts(Path(insto_env["INSTO_HOME"]), stdout, stderr)
        for private_value in (endpoint_sentinel, receiver.url, response_sentinel):
            assert private_value not in artifacts
    finally:
        if process is not None and process.poll() is None:
            _stop_daemon(process)
        receiver.stop()
