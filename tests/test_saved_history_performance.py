import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

from insto.service.history import _PROFILE_TRACKED_FIELDS
from insto.service.history_readonly import scan_sql


def test_large_saved_history_is_streamed_and_measured(monitoring_profile):
    profile = monitoring_profile
    db_path = profile.home / "store.db"
    values = dict.fromkeys(_PROFILE_TRACKED_FIELDS, None)
    values.update(username="alice", biography="界" * 21600)
    body = json.dumps(values, ensure_ascii=False)
    assert 64000 < len(body.encode("utf-8")) + 2 <= 65536
    with closing(sqlite3.connect(db_path)) as db, db:
        db.executemany("INSERT INTO snapshots(target_pk,captured_at,profile_fields_json,last_post_pks_json) "
                       "VALUES (?,?,?,'[]')",
                       ((str(1 + i // 100), i + 1, body) for i in range(2000)))
        global_sql, global_args = scan_sql(2000, None, None, 2000)
        target_sql, target_args = scan_sql(2000, (1000, 999), "7", 50)
        for sql in (global_sql, target_sql):
            assert "profile_fields_json" not in sql and "last_post_pks_json" not in sql
        global_plan = [row[3] for row in db.execute("EXPLAIN QUERY PLAN " + global_sql, global_args)]
        target_plan = [row[3] for row in db.execute("EXPLAIN QUERY PLAN " + target_sql, target_args)]
        assert any("USE TEMP B-TREE FOR ORDER BY" in line for line in global_plan)
        assert any("idx_snapshots_target_ts" in line and "captured_at<" in line for line in target_plan)
        assert [row[1] for row in db.execute("PRAGMA index_list(snapshots)")] == ["idx_snapshots_target_ts"]
        assert db.execute("SELECT value FROM _meta WHERE key='schema_version'").fetchone()[0] == "2"
    script = '''
import json
import resource
import sqlite3
import sys
import time
import tracemalloc
from pathlib import Path
from insto.desktop import database
from insto.desktop.history import run
from insto.desktop.history_params import validate_params
from insto.desktop.profile import Profile
from insto.desktop.protocol import encode

original_connect = sqlite3.connect

def memory_backed_connect(*args, **kwargs):
    connection = original_connect(*args, **kwargs)
    connection.execute('PRAGMA temp_store=MEMORY')
    return connection

if sys.argv[2] == 'memory':
    database.sqlite3.connect = memory_backed_connect
profile = Profile(Path(sys.argv[1]))
operation = sys.argv[3]
params = {'username': 'missing'} if operation == 'snapshots.targets' else {}
scale = 1 if sys.platform == 'darwin' else 1024
rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale
tracemalloc.start()
started = time.monotonic()
page = run(profile, operation, validate_params(operation, params), deadline=started+10)
elapsed = time.monotonic()-started
current, peak = tracemalloc.get_traced_memory()
tracemalloc.stop()
rss_peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale
wire = encode({'protocol_version': 1, 'request_id': 'x'*64, 'result': page})
print(json.dumps({'elapsed_seconds': elapsed, 'python_peak_bytes': peak,
    'rss_peak_bytes': rss_peak, 'rss_growth_bytes': max(0,rss_peak-rss_before),
    'encoded_bytes': len(wire), 'page': page}))
'''
    measurements = {}
    for temp_store in ("file", "memory"):
        for operation in ("snapshots.targets", "changes.list"):
            result = subprocess.run(
                [sys.executable, "-B", "-c", script, str(profile.root), temp_store, operation],
                cwd=Path(__file__).resolve().parents[1],
                env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C.UTF-8",
                     "PYTHONDONTWRITEBYTECODE": "1"},
                capture_output=True, text=True, timeout=30)
            assert result.returncode == 0, result.stderr
            measured = json.loads(result.stdout)
            page = measured["page"]
            if operation == "snapshots.targets":
                assert page["items"] == [] and page["scanned"] == 2000
            else:
                # The 200 newest candidates span PKs 20 and 19: 198 unchanged pairs
                # resolved through predecessor lookups plus one baseline per PK.
                assert page["scanned"] == 200
                assert [item["kind"] for item in page["items"]] == ["baseline", "baseline"]
            assert page["next_cursor"] and not page["scan_complete"]
            assert measured["elapsed_seconds"] < 10
            assert measured["python_peak_bytes"] < 16 * 1024 * 1024
            assert measured["rss_growth_bytes"] < 64 * 1024 * 1024
            assert measured["encoded_bytes"] < 2 * 1024 * 1024
            measurements[f"{temp_store}:{operation}"] = measured
    print(json.dumps({"measurements": measurements, "global_plan": global_plan,
                      "target_plan": target_plan}, sort_keys=True))


def test_oversized_multibyte_row_is_projected_out_before_large_scan(monitoring_profile):
    import time
    from insto.desktop.history import run
    from insto.desktop.history_params import validate_params
    from insto.service.history_readonly import PROJECTION

    profile = monitoring_profile
    payload = json.dumps({"username": "alice", "biography": "界" * 24000}, ensure_ascii=False)
    assert len(payload) < 65536 < len(payload.encode("utf-8"))
    with closing(sqlite3.connect(profile.home / "store.db")) as db, db:
        db.row_factory = sqlite3.Row
        db.execute(
            "INSERT INTO snapshots(target_pk,captured_at,profile_fields_json,last_post_pks_json) "
            "VALUES ('7',1,?,'[]')", (payload,),
        )
        row = db.execute("SELECT " + PROJECTION + " FROM snapshots").fetchone()
        assert row["profile_json"] is None and row["posts_json"] is None
    page = run(profile, "snapshots.targets", validate_params("snapshots.targets", {"username": "alice"}),
               deadline=time.monotonic() + 10)
    assert page["items"][0]["code"] == "history_oversized"
    assert page["scanned"] == 1
