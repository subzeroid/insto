import json
import sqlite3
from contextlib import closing

import pytest

from insto.service.history import _PROFILE_TRACKED_FIELDS
from insto.service.history_readonly import Reader, metadata


class ObservedCursor:
    def __init__(self, cursor, calls):
        self.cursor, self.calls = cursor, calls

    def fetchmany(self, size):
        rows = self.cursor.fetchmany(size)
        self.calls.append((size, len(rows)))
        return rows

    def fetchall(self):
        return self.cursor.fetchall()

    def fetchone(self):
        return self.cursor.fetchone()

    def close(self):
        self.cursor.close()


class ObservedConnection:
    def __init__(self, db):
        self.db, self.calls, self.queries = db, [], []

    def execute(self, sql, params=()):
        self.queries.append((sql, params))
        return ObservedCursor(self.db.execute(sql, params), self.calls)


@pytest.fixture
def query_db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("""CREATE TABLE snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT, target_pk TEXT, captured_at INTEGER,
        profile_fields_json TEXT, last_post_pks_json TEXT,
        avatar_url_hash TEXT, banner_url_hash TEXT)""")
    db.execute("CREATE INDEX idx_snapshots_target_ts ON snapshots(target_pk,captured_at)")
    fields = json.dumps(dict.fromkeys(_PROFILE_TRACKED_FIELDS, None))
    db.executemany(
        "INSERT INTO snapshots VALUES (?, ?, ?, ?, '[]', NULL, NULL)",
        ((i, "7" if i % 2 else "8", i // 2, fields) for i in range(1, 301)),
    )
    yield db
    db.close()


def test_streaming_batches_and_candidate_ceiling(query_db):
    observed = ObservedConnection(query_db)
    reader = Reader(observed, lambda: None)
    with closing(reader.rows(300, None, None, 200)) as rows:
        result = [metadata(row) for row in rows]
    assert len(result) == 200
    assert all(size <= 16 for size, returned in observed.calls)
    assert sum(returned for size, returned in observed.calls) == 200
    assert [item.key for item in result] == sorted((item.key for item in result), reverse=True)
    sorted_sql = [sql for sql, _ in observed.queries if "ORDER BY" in sql]
    assert sorted_sql and all("profile_fields_json" not in sql for sql in sorted_sql)
    assert sum(1 for sql, _ in observed.queries if " IN (" in sql) == 13


def test_key_scan_plans_use_index_ranges_and_never_sort_payloads(query_db):
    from insto.service.history_readonly import batch_sql, scan_sql

    global_sql, global_args = scan_sql(300, None, None, 2000)
    target_sql, target_args = scan_sql(300, (100, 199), "7", 200)
    for sql in (global_sql, target_sql):
        assert "profile_fields_json" not in sql and "last_post_pks_json" not in sql
    global_plan = [
        row[3] for row in query_db.execute("EXPLAIN QUERY PLAN " + global_sql, global_args)
    ]
    target_plan = [
        row[3] for row in query_db.execute("EXPLAIN QUERY PLAN " + target_sql, target_args)
    ]
    assert any("USE TEMP B-TREE FOR ORDER BY" in step for step in global_plan), global_plan
    # SQLite renders the row-value range as "captured_at<?" (3.49) or as
    # "(captured_at,rowid)<(?,?)" (other releases); both prove the index range.
    assert any(
        "idx_snapshots_target_ts" in step and "captured_at" in step for step in target_plan
    ), target_plan
    batch_plan = [
        row[3] for row in query_db.execute("EXPLAIN QUERY PLAN " + batch_sql(3), (1, 2, 3))
    ]
    assert any("INTEGER PRIMARY KEY" in step for step in batch_plan), batch_plan
    reader = Reader(query_db, lambda: None)
    with closing(reader.rows(300, (100, 199), "7", 200)) as rows:
        keys = [metadata(row).key for row in rows]
    assert keys and all(key < (100, 199) for key in keys)
    assert keys == sorted(keys, reverse=True)


def test_backdated_later_insert_is_excluded_and_ties_do_not_repeat(query_db):
    reader = Reader(query_db, lambda: None)
    ceiling = reader.ceiling()
    with closing(reader.rows(ceiling, None, None, 16)) as rows:
        first = [metadata(row) for row in rows]
    query_db.execute(
        "INSERT INTO snapshots(target_pk,captured_at,profile_fields_json,last_post_pks_json) "
        "VALUES ('7',1,'{}','[]')"
    )
    with closing(reader.rows(ceiling, first[-1].key, None, 2000)) as rows:
        rest = [metadata(row) for row in rows]
    assert len(first + rest) == 300
    assert len({item.identifier for item in first + rest}) == 300
    assert max(item.identifier for item in rest) <= ceiling
    assert all(item.key < first[-1].key for item in rest)


def test_predecessor_is_adjacent_within_pk_and_uses_id_on_equal_time(query_db):
    reader = Reader(query_db, lambda: None)
    current = metadata(reader.selected(299))
    previous = metadata(reader.predecessor(current, 300))
    assert previous.target_pk == current.target_pk == "7"
    assert previous.identifier == 297
    query_db.execute("UPDATE snapshots SET captured_at=148 WHERE id=299")
    current = metadata(reader.selected(299))
    assert metadata(reader.predecessor(current, 300)).identifier == 297
    assert reader.predecessor(metadata(reader.selected(1)), 300) is None


def test_closing_iteration_releases_cursor_and_cpu_deadline(query_db):
    from insto.desktop.errors import DesktopError

    calls = 0

    def check():
        nonlocal calls
        calls += 1
        if calls >= 5:
            raise DesktopError("operation_timeout")

    reader = Reader(query_db, check)
    with (
        pytest.raises(DesktopError, match="operation_timeout"),
        closing(reader.rows(300, None, None, 200)) as rows,
    ):
        list(rows)
    assert query_db.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 300
