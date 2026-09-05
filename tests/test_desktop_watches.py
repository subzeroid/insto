import asyncio
import contextlib
import dataclasses
import json
import sqlite3
import time
from unittest.mock import AsyncMock

import pytest

from insto.desktop.errors import DesktopError
from insto.exceptions import BackendError
from insto.models import WatchSpec
from insto.service.history import HistoryStore
from insto.service.watch import WatchManager
from insto.service.watch_daemon import WatchDaemon
from insto.service.watch_lock import WatchProcessLock


def command(profile, operation, params):
    from insto.desktop.watch_params import validate_params
    from insto.desktop.watches import run

    return run(
        profile, operation, validate_params(operation, params), deadline=time.monotonic() + 10
    )


def store(profile):
    # Review gate G7: a sqlite3 connection context manager only commits; closing()
    # releases the file before lock/checkpoint assertions instead of waiting for GC.
    return contextlib.closing(sqlite3.connect(profile.home / "store.db"))


@pytest.mark.parametrize(
    "operation,params",
    [
        ("overview", {"home": "/foreign"}),
        ("watches.list", {"limit": True}),
        ("watches.list", {"limit": 51}),
        ("watches.list", {"cursor": "not-a-cursor"}),
        ("watches.add", {"user": ".."}),
        ("watches.add", {"user": "a", "interval_seconds": 299}),
        ("watches.add", {"user": "a", "interval_seconds": True}),
        ("watches.add", {"user": "a", "interval_seconds": 2**31}),
        ("watches.add", {"user": "a", "token": "offline-secret"}),
        ("watches.pause", {"user": "a"}),
        ("watches.update", {"user": "a", "revision": "f" * 64}),
        ("watches.remove", {"user": "a", "revision": "g" * 64}),
    ],
)
def test_invalid_params_have_static_error(operation, params):
    from insto.desktop.watch_params import validate_params

    with pytest.raises(DesktopError, match="invalid_params"):
        validate_params(operation, params)


def test_username_canonicalization_matches_the_cli():
    # Review gate G12: watches.add, snapshots.targets and the CLI share one rule.
    from insto.desktop.watch_params import validate_params
    from insto.service.history import _canonical_watch_user

    for raw in ("@@Alice ", "@alice", "ALICE", "  alice  "):
        assert validate_params("watches.add", {"user": raw})["user"] == "alice"
        assert _canonical_watch_user(raw) == "alice"
    # The CLI strips "@" before whitespace, so a space before "@" is invalid there too.
    with pytest.raises(DesktopError, match="invalid_params"):
        validate_params("watches.add", {"user": " @alice"})
    with pytest.raises(ValueError):
        _canonical_watch_user(" @alice")


def test_revision_error_and_noop(monitoring_profile):
    watch = command(monitoring_profile, "watches.add", {"user": "@Alice"})["watch"]
    same = command(
        monitoring_profile,
        "watches.resume",
        {
            "user": "alice",
            "revision": watch["revision"],
        },
    )["watch"]
    assert same["revision"] == watch["revision"]
    paused = command(
        monitoring_profile,
        "watches.pause",
        {
            "user": "alice",
            "revision": watch["revision"],
        },
    )["watch"]
    assert paused["revision"] != watch["revision"]
    with pytest.raises(DesktopError, match="watch_conflict"):
        command(
            monitoring_profile,
            "watches.remove",
            {
                "user": "alice",
                "revision": watch["revision"],
            },
        )


def test_paused_rows_paginate_without_error_text(monitoring_profile):
    with store(monitoring_profile) as connection, connection:
        connection.executemany(
            "INSERT INTO watches VALUES (?,?,300,NULL,?,1,'paused')",
            [(f"user{i:03}", f"generation{i}", "old-secret") for i in range(55)],
        )
    first = command(monitoring_profile, "watches.list", {})
    assert len(first["items"]) == 50
    assert first["next_cursor"]
    second = command(monitoring_profile, "watches.list", {"cursor": first["next_cursor"]})
    assert len(second["items"]) == 5
    assert second["next_cursor"] is None
    raw = json.dumps([first, second])
    assert "old-secret" not in raw and "generation" not in raw
    assert all(row["has_error"] for row in first["items"])
    assert len({r["user"] for r in first["items"] + second["items"]}) == 55


def test_add_and_resume_active_cap(monitoring_profile):
    a = command(monitoring_profile, "watches.add", {"user": "a"})["watch"]
    paused = command(monitoring_profile, "watches.pause", {"user": "a", "revision": a["revision"]})[
        "watch"
    ]
    for user in ("b", "c", "d"):
        command(monitoring_profile, "watches.add", {"user": user})
    with pytest.raises(DesktopError, match="watch_limit"):
        command(monitoring_profile, "watches.add", {"user": "e"})
    with pytest.raises(DesktopError, match="watch_limit"):
        command(monitoring_profile, "watches.resume", {"user": "a", "revision": paused["revision"]})


async def test_late_real_daemon_persistence_cannot_undo_pause(monitoring_profile):
    watch = command(monitoring_profile, "watches.add", {"user": "alice"})["watch"]
    store = HistoryStore(monitoring_profile.home / "store.db")
    gate = asyncio.Event()

    async def no_network():
        return None

    manager = WatchManager(WatchProcessLock(store.path), release_when_empty=False)
    daemon = WatchDaemon(
        history=store, manager=manager, tick_factory=lambda user: no_network, role="daemon"
    )
    spec = store.get_watch("alice")
    assert isinstance(spec, WatchSpec)

    async def late_tick_completion():
        await gate.wait()
        return await daemon._persist_state(dataclasses.replace(spec, last_ok=123))

    task = asyncio.create_task(late_tick_completion())
    try:
        paused = command(
            monitoring_profile, "watches.pause", {"user": "alice", "revision": watch["revision"]}
        )["watch"]
        gate.set()
        assert await asyncio.wait_for(task, 2) is False
        assert store.get_watch("alice").status == "paused"
        assert paused["status"] == "paused"
    finally:
        gate.set()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        store.close()


@pytest.mark.parametrize("remaining", [0, 8])
async def test_overview_does_not_scan_history_or_call_provider(
    monitoring_profile, monkeypatch, remaining
):
    from insto.desktop import access, configuration, operations, watches

    command(monitoring_profile, "watches.add", {"user": "alice"})
    with monitoring_profile.locked():
        state = monitoring_profile.read_state()
        assert state is not None
        state.update(quota_remaining=remaining, quota_checked_at=123)
        monitoring_profile.write_state(state)
    original_state = monitoring_profile.state.read_bytes()
    monkeypatch.setattr(access, "make_backend", lambda *a: pytest.fail("provider constructed"))
    monkeypatch.setattr(
        configuration, "check_database", lambda *a, **k: pytest.fail("C1 copy used")
    )
    service = type("Service", (), {})()
    service.inspect_owned = AsyncMock(
        return_value={
            "registration": "unloaded",
            "process": {"state": None, "pid": None},
            "executor": {"state": "idle", "pid": None},
        }
    )
    monkeypatch.setattr(operations, "read_service", lambda *a: service)
    for _ in range(2):
        result = await watches.overview(monitoring_profile, deadline=time.monotonic() + 10)
        assert result["configured"] is True and result["service_state"] == "stopped"
        assert result["watches"][0]["user"] == "alice"
        assert result["quota_remaining"] == remaining
        assert result["quota_checked_at"] == 123
        assert monitoring_profile.state.read_bytes() == original_state
        assert "offline-desktop-token" not in json.dumps(result)
    assert service.inspect_owned.await_count == 2


async def test_unconfigured_overview_has_no_quota_or_check_time(tmp_path, monkeypatch):
    from insto.desktop import operations, watches
    from insto.desktop.profile import Profile

    profile = Profile(tmp_path / "absent")
    monkeypatch.setattr(operations, "read_service", lambda *a: pytest.fail("service inspected"))
    result = await watches.overview(profile, deadline=time.monotonic() + 10)
    assert result == {
        "configured": False,
        "desired_service": None,
        "service_state": "unknown",
        "quota_remaining": None,
        "quota_checked_at": None,
        "watches": [],
        "next_cursor": None,
    }
    assert not profile.root.exists()


async def test_unknown_service_never_becomes_stopped(monitoring_profile, monkeypatch):
    from insto.desktop import operations, watches

    service = type("Service", (), {})()
    service.inspect_owned = AsyncMock(
        return_value={
            "registration": "unknown",
            "process": {"state": "unknown", "pid": None},
            "executor": {"state": "idle", "pid": None},
        }
    )
    monkeypatch.setattr(operations, "read_service", lambda *a: service)
    result = await watches.overview(monitoring_profile, deadline=time.monotonic() + 10)
    assert result["service_state"] == "unknown"


@pytest.mark.parametrize(
    "operation",
    [
        "overview",
        "watches.list",
        "watches.add",
        "watches.update",
        "watches.pause",
        "watches.resume",
        "watches.remove",
    ],
)
async def test_bad_watch_request_never_imports_operation(monkeypatch, operation):
    import sys

    from insto import desktop
    from insto.desktop.dispatch import handle

    # Review gate G6: `from insto.desktop import watches` resolves the package
    # attribute first, so poisoning sys.modules alone cannot fail once an earlier
    # test imported the module. Drop the attributes and poison both modules.
    for name in ("watches", "profile"):
        monkeypatch.delattr(desktop, name, raising=False)
        monkeypatch.setitem(sys.modules, "insto.desktop." + name, None)
    raw = await handle(
        (
            json.dumps(
                {
                    "protocol_version": 1,
                    "request_id": "c2",
                    "operation": operation,
                    "params": {"home": "/foreign", "token": "offline-secret"},
                }
            )
            + "\n"
        ).encode()
    )
    assert json.loads(raw)["error"]["code"] == "invalid_params"
    assert b"offline-secret" not in raw and b"/foreign" not in raw


async def test_dispatch_real_watch_roundtrip(monitoring_profile, monkeypatch):
    from insto.desktop.dispatch import handle

    monkeypatch.setenv("INSTO_DESKTOP_ROOT", str(monitoring_profile.root))

    async def send(operation, params):
        raw = await handle(
            (
                json.dumps(
                    {
                        "protocol_version": 1,
                        "request_id": "c2",
                        "operation": operation,
                        "params": params,
                    }
                )
                + "\n"
            ).encode()
        )
        assert raw.count(b"\n") == 1 and len(raw) < 2 * 1024 * 1024
        assert b"offline-desktop-token" not in raw
        return json.loads(raw)

    watch = (await send("watches.add", {"user": "alice"}))["result"]["watch"]
    listed = (await send("watches.list", {}))["result"]["items"]
    assert listed == [watch]
    paused = (
        await send(
            "watches.pause",
            {
                "user": "alice",
                "revision": watch["revision"],
            },
        )
    )["result"]["watch"]
    stale = await send("watches.remove", {"user": "alice", "revision": watch["revision"]})
    assert stale["error"]["code"] == "watch_conflict"
    removed = await send("watches.remove", {"user": "alice", "revision": paused["revision"]})
    assert removed["result"] == {"removed_user": "alice"}


async def test_dispatch_real_resume_clears_errors_and_fences_old_generation(
    monitoring_profile, monkeypatch
):
    from insto.desktop import access, operations
    from insto.desktop.dispatch import handle

    monkeypatch.setenv("INSTO_DESKTOP_ROOT", str(monitoring_profile.root))
    monkeypatch.setattr(access, "make_backend", lambda *a: pytest.fail("provider constructed"))
    monkeypatch.setattr(operations, "read_service", lambda *a: pytest.fail("service inspected"))

    async def send(operation, params):
        raw = await handle(
            (
                json.dumps(
                    {
                        "protocol_version": 1,
                        "request_id": "resume-contract",
                        "operation": operation,
                        "params": params,
                    }
                )
                + "\n"
            ).encode()
        )
        assert raw.count(b"\n") == 1 and len(raw) < 2 * 1024 * 1024
        for secret in (b"offline-desktop-token", b"stored-resume-secret", b"late-resume-secret"):
            assert secret not in raw
        assert b"registration_id" not in raw
        response = json.loads(raw)
        assert response["protocol_version"] == 1
        assert response["request_id"] == "resume-contract"
        return response

    added = (await send("watches.add", {"user": "alice", "interval_seconds": 600}))["result"][
        "watch"
    ]
    store = HistoryStore(monitoring_profile.home / "store.db")
    try:
        original_spec = store.get_watch("alice")
        assert original_spec is not None
        assert store.update_watch_state(
            original_spec, last_ok=123, last_error="stored-resume-secret", consecutive_errors=2
        )
        paused = (await send("watches.pause", {"user": "alice", "revision": added["revision"]}))[
            "result"
        ]["watch"]
        assert paused["status"] == "paused"
        assert paused["last_ok"] == 123
        assert paused["has_error"] is True and paused["consecutive_errors"] == 2
        paused_spec = store.get_watch("alice")
        assert paused_spec is not None

        resumed = (await send("watches.resume", {"user": "alice", "revision": paused["revision"]}))[
            "result"
        ]["watch"]
        assert resumed == {
            **paused,
            "status": "active",
            "has_error": False,
            "consecutive_errors": 0,
            "revision": resumed["revision"],
        }
        assert resumed["interval_seconds"] == 600
        assert resumed["waiting_first_check"] is False
        assert resumed["revision"] not in (added["revision"], paused["revision"])
        resumed_spec = store.get_watch("alice")
        assert resumed_spec is not None
        assert resumed_spec.status == "active" and resumed_spec.last_ok == 123
        assert resumed_spec.interval_seconds == 600
        assert resumed_spec.last_error is None and resumed_spec.consecutive_errors == 0
        assert len({s.registration_id for s in (original_spec, paused_spec, resumed_spec)}) == 3

        repeated = (
            await send("watches.resume", {"user": "alice", "revision": resumed["revision"]})
        )["result"]["watch"]
        assert repeated == resumed
        assert store.get_watch("alice") == resumed_spec
        for old_revision in (added["revision"], paused["revision"]):
            stale = await send("watches.pause", {"user": "alice", "revision": old_revision})
            assert "result" not in stale
            assert stale["error"]["code"] == "watch_conflict"
            assert stale["error"]["retryable"] is False
        for old_spec in (original_spec, paused_spec):
            assert not store.update_watch_state(
                old_spec, last_ok=999, last_error="late-resume-secret", consecutive_errors=7
            )
        assert store.get_watch("alice") == resumed_spec

        assert store.update_watch_state(resumed_spec, last_ok=124)
        listed = (await send("watches.list", {}))["result"]["items"]
        assert listed == [{**resumed, "last_ok": 124}]
        # `store` is the HistoryStore here; open a separate closed-on-exit reader.
        with contextlib.closing(
            sqlite3.connect(monitoring_profile.home / "store.db")
        ) as connection:
            assert (
                connection.execute("SELECT COUNT(*) FROM watches WHERE status='active'").fetchone()[
                    0
                ]
                == 1
            )
    finally:
        store.close()


def test_huge_stored_error_is_not_loaded_into_dto(monitoring_profile):
    import tracemalloc

    from insto.desktop.protocol import encode

    command(monitoring_profile, "watches.add", {"user": "alice"})
    with store(monitoring_profile) as connection, connection:
        connection.execute("UPDATE watches SET last_error=CAST(zeroblob(10485760) AS TEXT)")
    tracemalloc.start()
    try:
        result = command(monitoring_profile, "watches.list", {})
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    raw = encode({"protocol_version": 1, "request_id": "x" * 64, "result": result})
    assert result["items"][0]["has_error"] is True
    assert len(raw) < 65536 and peak < 2 * 1024 * 1024


@pytest.mark.parametrize("column", ["interval_seconds", "last_ok", "consecutive_errors", "status"])
def test_huge_numeric_or_status_cell_is_rejected_before_materialization(monitoring_profile, column):
    # Review gate G3: SQLite stores TEXT in INTEGER columns and the CHECK constraints
    # do not stop it. Three 10 MiB cells must be rejected in SQL, never fetched.
    import tracemalloc

    for user in ("alice", "bob", "carol"):
        command(monitoring_profile, "watches.add", {"user": user})
    with store(monitoring_profile) as connection, connection:
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute(f"UPDATE watches SET {column}=CAST(zeroblob(10485760) AS TEXT)")
    tracemalloc.start()
    try:
        with pytest.raises(DesktopError, match="storage_error"):
            command(monitoring_profile, "watches.list", {})
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 2 * 1024 * 1024


def test_watch_pagination_walks_the_primary_key_index_without_a_temp_sort(monitoring_profile):
    # Review gate G3: `ORDER BY user` would resolve to the CASE alias and sort a
    # temporary B-tree; the qualified column must use sqlite_autoindex_watches_1.
    from insto.service.watch_registry import _LIST_SQL

    with store(monitoring_profile) as connection, connection:
        connection.executemany(
            "INSERT INTO watches VALUES (?,?,300,NULL,NULL,0,'paused')",
            [(f"user{i:04}", f"generation{i}") for i in range(500)],
        )
        plan = [row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + _LIST_SQL, ("", 51))]
    assert not any("TEMP B-TREE" in step for step in plan), plan
    assert any("sqlite_autoindex_watches_1" in step for step in plan), plan
    seen = []
    cursor = None
    while True:
        page = command(monitoring_profile, "watches.list", {"cursor": cursor} if cursor else {})
        seen.extend(item["user"] for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == 500 and seen == sorted(seen) and len(set(seen)) == 500


def test_malformed_watch_is_explicit_not_coerced_active(monitoring_profile):
    with store(monitoring_profile) as connection, connection:
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute(
            "INSERT INTO watches VALUES ('alice','generation',300,NULL,NULL,0,'broken')"
        )
    with pytest.raises(DesktopError, match="storage_error"):
        command(monitoring_profile, "watches.list", {})


async def test_overview_queries_no_snapshot_content(monitoring_profile, monkeypatch):
    from insto.desktop import database, operations, watches

    statements = []
    original = sqlite3.connect

    def observed(*args, **kwargs):
        connection = original(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(database.sqlite3, "connect", observed)
    service = type("Service", (), {})()
    service.inspect_owned = AsyncMock(
        return_value={
            "registration": "unloaded",
            "process": {"state": None, "pid": None},
            "executor": {"state": "idle", "pid": None},
        }
    )
    monkeypatch.setattr(operations, "read_service", lambda *a: service)
    await watches.overview(monitoring_profile, deadline=time.monotonic() + 10)
    snapshot_sql = [s for s in statements if "snapshots" in s.lower()]
    assert snapshot_sql
    assert all(s.rstrip().endswith("LIMIT 0") for s in snapshot_sql)


@pytest.mark.parametrize(
    "report,expected",
    [
        (
            {
                "registration": "loaded",
                "process": {"state": "running", "pid": 42},
                "executor": {"state": "busy", "pid": 42},
            },
            "running",
        ),
        (
            {
                "registration": "loaded",
                "process": {"state": "running", "pid": 42},
                "executor": {"state": "idle", "pid": None},
            },
            "unknown",
        ),
        (BackendError("executor ownership is not confirmed"), "unknown"),
        ({"registration": "unloaded"}, "unknown"),
    ],
    ids=["running", "process-without-busy-executor", "backend-error", "incomplete-report"],
)
async def test_overview_service_states_are_explicit(
    monitoring_profile, monkeypatch, report, expected
):
    # Review gate G11: the only green state and every inspection failure resolve
    # explicitly, and the service handle is always released.
    from insto.desktop import operations, watches

    service = type("Service", (), {})()
    if isinstance(report, Exception):
        service.inspect_owned = AsyncMock(side_effect=report)
    else:
        service.inspect_owned = AsyncMock(return_value=report)
    monkeypatch.setattr(operations, "read_service", lambda *a: service)
    result = await watches.overview(monitoring_profile, deadline=time.monotonic() + 10)
    assert result["service_state"] == expected
    assert service._active is False


async def test_overview_deadline_after_watch_page_skips_service_inspection(
    monitoring_profile, monkeypatch
):
    from insto.desktop import operations, watches

    monkeypatch.setattr(operations, "read_service", lambda *a: pytest.fail("service inspected"))
    real_run = watches.run
    finished = []

    def run_then_arm(*args, **kwargs):
        page = real_run(*args, **kwargs)
        finished.append(page)
        return page

    def expiring(deadline):
        if finished:
            raise DesktopError("operation_timeout")

    monkeypatch.setattr(watches, "run", run_then_arm)
    monkeypatch.setattr(watches, "check_deadline", expiring)
    with pytest.raises(DesktopError, match="operation_timeout"):
        await watches.overview(monitoring_profile, deadline=time.monotonic() + 10)
    assert finished and finished[0]["items"] == []


async def test_overview_through_real_handle(monitoring_profile, monkeypatch):
    from insto.desktop import operations
    from insto.desktop.dispatch import handle

    command(monitoring_profile, "watches.add", {"user": "alice"})
    service = type("Service", (), {})()
    service.inspect_owned = AsyncMock(
        return_value={
            "registration": "unloaded",
            "process": {"state": None, "pid": None},
            "executor": {"state": "idle", "pid": None},
        }
    )
    monkeypatch.setattr(operations, "read_service", lambda *a: service)
    monkeypatch.setenv("INSTO_DESKTOP_ROOT", str(monitoring_profile.root))
    request = json.dumps(
        {"protocol_version": 1, "request_id": "overview-1", "operation": "overview", "params": {}}
    )
    raw = await handle((request + "\n").encode())
    assert raw.count(b"\n") == 1 and b"offline-desktop-token" not in raw
    response = json.loads(raw)
    assert response["request_id"] == "overview-1"
    assert response["result"]["configured"] is True
    assert response["result"]["service_state"] == "stopped"
    assert [item["user"] for item in response["result"]["watches"]] == ["alice"]


def test_mutating_a_missing_watch_is_not_found(monitoring_profile):
    revision = "f" * 64
    for action in ("pause", "resume", "remove"):
        with pytest.raises(DesktopError, match="watch_not_found"):
            command(
                monitoring_profile, f"watches.{action}", {"user": "ghost", "revision": revision}
            )
    with pytest.raises(DesktopError, match="watch_not_found"):
        command(
            monitoring_profile,
            "watches.update",
            {"user": "ghost", "revision": revision, "interval_seconds": 600},
        )


def test_real_bridge_watch_list_offline(monitoring_profile):
    import os
    import subprocess
    import sys

    command(monitoring_profile, "watches.add", {"user": "alice"})
    request = (
        json.dumps(
            {
                "protocol_version": 1,
                "request_id": "subprocess-c2",
                "operation": "watches.list",
                "params": {},
            }
        )
        + "\n"
    ).encode()
    result = subprocess.run(
        [sys.executable, "-B", "-m", "insto.desktop"],
        input=request,
        capture_output=True,
        timeout=15,
        env={
            "PATH": os.defpath,
            "INSTO_DESKTOP_ROOT": str(monitoring_profile.root),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )
    assert result.returncode == 0 and result.stderr == b""
    assert result.stdout.count(b"\n") == 1
    assert json.loads(result.stdout)["result"]["items"][0]["user"] == "alice"
    assert b"offline-desktop-token" not in result.stdout
