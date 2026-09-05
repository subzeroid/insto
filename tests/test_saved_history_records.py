import json
import sqlite3

import pytest

from insto.service.history import _PROFILE_TRACKED_FIELDS
from insto.service.history_readonly import (
    PROJECTION, HistoryReadError, comparison, metadata, snapshot,
)


@pytest.fixture
def connection():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("""CREATE TABLE snapshots (
        id INTEGER PRIMARY KEY, target_pk TEXT, captured_at INTEGER,
        profile_fields_json TEXT, last_post_pks_json TEXT,
        avatar_url_hash TEXT, banner_url_hash TEXT)""")
    yield db
    db.close()


def row(db, payload, posts="[]", *, identifier=1, stamp=1, pk="7", avatar=None):
    db.execute("INSERT INTO snapshots VALUES (?, ?, ?, ?, ?, ?, NULL)",
               (identifier, pk, stamp, payload, posts, avatar))
    return db.execute("SELECT " + PROJECTION + " FROM snapshots WHERE id=?", (identifier,)).fetchone()


def test_sql_does_not_materialize_combined_oversize_multibyte_payload(connection):
    payload = json.dumps({"biography": "界" * 21840}, ensure_ascii=False)
    selected = row(connection, payload, '["12345678901234567890"]')
    assert len(payload) < 65536 < len(payload.encode()) + 24
    assert selected["payload_bytes"] > 65536
    assert selected["profile_json"] is None
    assert selected["posts_json"] is None
    with pytest.raises(HistoryReadError, match="history_oversized"):
        snapshot(selected, lambda: None)


@pytest.mark.parametrize("payload,posts", [
    ('{"biography":"secret","biography":"again"}', '[]'),
    ('{"follower_count":NaN}', '[]'),
    ('{"follower_count":1e999}', '[]'),
    ('{"follower_count":true}', '[]'),
    ('{"follower_count":1.0}', '[]'),
    ('{"follower_count":-1}', '[]'),
    ('{"follower_count":9007199254740992}', '[]'),
    ('{"is_private":1}', '[]'),
    ('{"biography":[]}', '[]'),
    ('{"biography":"\\ud800"}', '[]'),
    ('{"username":"@@alice"}', '[]'),
    ('[]', '[]'), ('{', '[]'), ('{}', '{}'), ('{}', '[1]'),
    ('{}', '[{}]'), ('{}', '[NaN]'),
    (b'{"biography":"secret"}', '[]'), ('{}', b'[]'), (5, '[]'), (None, '[]'), ('{}', None),
])
def test_corruption_is_explicit_and_never_reflects_raw_text(connection, payload, posts):
    selected = row(connection, payload, posts)
    with pytest.raises(HistoryReadError, match="history_corrupt") as caught:
        snapshot(selected, lambda: None)
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize("pk,stamp,avatar", [
    ("x" * 100000, 1, None), ("01", 1, None), ("7", -1, None),
    ("7", "x" * 100000, None), ("7", 1, "x" * 100000), ("7", 1, "q" * 64),
])
def test_scalar_guards_prevent_large_or_invalid_dtos(connection, pk, stamp, avatar):
    selected = row(connection, "{}", pk=pk, stamp=stamp, avatar=avatar)
    assert all(not isinstance(value, str) or len(value) < 1000 for value in selected)
    with pytest.raises(HistoryReadError, match="history_corrupt"):
        snapshot(selected, lambda: None)


def test_invalid_utf8_text_is_fetched_as_bytes_and_reported_as_corrupt(connection):
    # Review gate G5: a TEXT cell holding invalid UTF-8 must be fetchable (BLOB
    # projection) and rejected by the record validator, never by the sqlite3 driver.
    connection.execute(
        "INSERT INTO snapshots VALUES "
        "(1, '7', 1, CAST(X'7B22FF223A317D' AS TEXT), '[]', NULL, NULL)"
    )
    stored = connection.execute("SELECT typeof(profile_fields_json) FROM snapshots").fetchone()
    assert stored[0] == "text"
    selected = connection.execute("SELECT " + PROJECTION + " FROM snapshots WHERE id=1").fetchone()
    assert type(selected["profile_json"]) is bytes and selected["payload_types_valid"] == 1
    with pytest.raises(HistoryReadError, match="history_corrupt"):
        snapshot(selected, lambda: None)


def test_absent_old_field_is_unknown_but_null_is_known(connection):
    old = snapshot(row(connection, '{"biography":null}', identifier=1), lambda: None)
    new = snapshot(row(connection, '{"biography":"new","full_name":"Alice"}',
                       identifier=2, stamp=2), lambda: None)
    result = comparison(old, new, lambda: None)
    assert result["changes"] == [{"field": "biography", "old": None, "new": "new"}]
    assert "full_name" in result["unknown_fields"]
    assert "biography" not in result["unknown_fields"]


def test_identity_precision_ties_and_hash_semantics(connection):
    fields = json.dumps(dict.fromkeys(_PROFILE_TRACKED_FIELDS, None))
    older = snapshot(row(connection, fields, identifier=9007199254740993, stamp=3,
                         avatar="a" * 64), lambda: None)
    newer = snapshot(row(connection, fields, identifier=9007199254740994, stamp=3,
                         avatar="b" * 64), lambda: None)
    result = comparison(older, newer, lambda: None)
    assert result["older"]["id"] == "9007199254740993"
    assert result["newer"]["captured_at"] == 3
    assert result["changes"] == [{"field": "avatar", "old": "a" * 64, "new": "b" * 64}]
    assert result["unknown_fields"] == []
    assert metadata(connection.execute("SELECT " + PROJECTION + " FROM snapshots LIMIT 1").fetchone()).target_pk == "7"
