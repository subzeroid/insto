import json
import sqlite3
import time
from contextlib import closing

import pytest

from insto.desktop.errors import DesktopError
from insto.desktop.history import run
from insto.desktop.history_params import validate_params
from insto.desktop.protocol import MAX_OUTPUT_BYTES, encode
from insto.service.history import _PROFILE_TRACKED_FIELDS


def fields(**updates):
    value = dict.fromkeys(_PROFILE_TRACKED_FIELDS, None)
    value.update(username="alice", biography="", follower_count=1)
    value.update(updates)
    return value


def insert(profile, *, pk="7", stamp=1, payload=None, posts="[]", identifier=None):
    body = json.dumps(fields() if payload is None else payload, ensure_ascii=False) if not isinstance(payload, str) else payload
    with closing(sqlite3.connect(profile.home / "store.db")) as db, db:
        cursor = db.execute("INSERT INTO snapshots(id,target_pk,captured_at,"
                            "profile_fields_json,last_post_pks_json) "
                            "VALUES (?,?,?,?,?)",
                            (identifier, pk, stamp, body, posts))
        return str(cursor.lastrowid)


def request(profile, operation, params):
    return run(profile, operation, validate_params(operation, params),
               deadline=time.monotonic() + 10)


def test_empty_one_snapshot_and_metadata_only_list(monitoring_profile):
    p = monitoring_profile
    assert request(p, "snapshots.list", {"target_pk": "7"}) == {
        "items": [], "next_cursor": None, "scan_complete": True, "scanned": 0,
    }
    identifier = insert(p, payload=fields(biography="private-observed-biography"))
    page = request(p, "snapshots.list", {"target_pk": "7"})
    assert page["items"] == [{"kind": "snapshot", "snapshot": {
        "id": identifier, "target_pk": "7", "captured_at": 1,
    }}]
    assert "private-observed-biography" not in json.dumps(page)
    feed = request(p, "changes.list", {})
    assert feed["items"][0]["kind"] == "baseline"
    assert "changes" not in feed["items"][0]


def test_compare_same_pk_retention_and_chronological_order(monitoring_profile):
    p = monitoring_profile
    first = insert(p, stamp=3, payload=fields(follower_count=1))
    second = insert(p, stamp=3, payload=fields(follower_count=2))
    foreign = insert(p, pk="8", stamp=4)
    params = {"target_pk": "7", "older_id": first, "newer_id": second}
    assert request(p, "snapshots.compare", params)["changes"] == [
        {"field": "follower_count", "old": 1, "new": 2},
    ]
    with pytest.raises(DesktopError, match="snapshot_identity_mismatch"):
        request(p, "snapshots.compare", {**params, "newer_id": foreign})
    with pytest.raises(DesktopError, match="invalid_params"):
        request(p, "snapshots.compare", {**params, "older_id": second, "newer_id": first})
    with closing(sqlite3.connect(p.home / "store.db")) as db, db:
        db.execute("DELETE FROM snapshots WHERE id=?", (first,))
    with pytest.raises(DesktopError, match="snapshot_unavailable"):
        request(p, "snapshots.compare", params)


def test_unknown_fields_are_incomplete_not_fabricated_changes(monitoring_profile):
    p = monitoring_profile
    old = insert(p, payload={"biography": None})
    new = insert(p, stamp=2, payload={"biography": None, "full_name": "Alice"})
    result = request(p, "snapshots.compare", {"target_pk": "7", "older_id": old, "newer_id": new})
    assert result["changes"] == []
    assert "full_name" in result["unknown_fields"]
    assert "biography" not in result["unknown_fields"]
    feed = request(p, "changes.list", {"target_pk": "7"})
    assert feed["items"][0]["kind"] == "incomplete"


def test_username_rename_and_reuse_are_historical_evidence(monitoring_profile):
    p = monitoring_profile
    insert(p, pk="7", stamp=1, payload=fields(username="alice"))
    insert(p, pk="7", stamp=2, payload=fields(username="renamed"))
    insert(p, pk="8", stamp=3, payload=fields(username="Alice"))
    page = request(p, "snapshots.targets", {"username": "@ALICE"})
    assert {item["target_pk"] for item in page["items"]} == {"7", "8"}
    assert page["scan_complete"] is True
    assert all(item["kind"] == "target" for item in page["items"])
    assert request(p, "snapshots.targets", {"username": "missing"})["items"] == []


def test_target_dedup_is_page_local_and_search_is_incomplete_at_2000(monitoring_profile):
    p = monitoring_profile
    insert(p, pk="8", stamp=0, payload=fields(username="alice"))
    body = json.dumps(fields(username="alice"))
    with closing(sqlite3.connect(p.home / "store.db")) as db, db:
        db.executemany("INSERT INTO snapshots(target_pk,captured_at,profile_fields_json,last_post_pks_json) "
                       "VALUES ('7',? ,?,'[]')",
                       ((i, body) for i in range(1, 2001)))
    first = request(p, "snapshots.targets", {"username": "alice"})
    assert first["scanned"] == 2000
    assert first["scan_complete"] is False and first["next_cursor"]
    assert {item["target_pk"] for item in first["items"]} == {"7"}
    second = request(p, "snapshots.targets", {"username": "alice", "cursor": first["next_cursor"]})
    assert {item["target_pk"] for item in second["items"]} == {"8"}
    assert second["scan_complete"] is True
    # A separate page boundary is allowed to repeat a PK; callers union the evidence.
    tiny = request(p, "snapshots.targets", {"username": "alice", "limit": 1})
    again = request(p, "snapshots.targets", {"username": "alice", "limit": 1, "cursor": tiny["next_cursor"]})
    assert tiny["items"][0]["target_pk"] == again["items"][0]["target_pk"] == "7"


def test_unchanged_feed_can_continue_without_visible_items(monitoring_profile):
    p = monitoring_profile
    body = json.dumps(fields())
    with closing(sqlite3.connect(p.home / "store.db")) as db, db:
        db.executemany("INSERT INTO snapshots(target_pk,captured_at,profile_fields_json,last_post_pks_json) "
                       "VALUES ('7',?,?,'[]')",
                       ((i, body) for i in range(1, 202)))
    first = request(p, "changes.list", {})
    assert first["items"] == [] and first["scanned"] == 200
    assert first["next_cursor"] and first["scan_complete"] is False
    second = request(p, "changes.list", {"cursor": first["next_cursor"]})
    assert [item["kind"] for item in second["items"]] == ["baseline"]
    assert second["scan_complete"] is True


def test_feed_uses_same_pk_predecessor_across_interleaved_targets(monitoring_profile):
    p = monitoring_profile
    old = insert(p, pk="7", stamp=1, payload=fields(follower_count=1))
    insert(p, pk="8", stamp=2, payload=fields(follower_count=999))
    new = insert(p, pk="7", stamp=3, payload=fields(follower_count=2))
    item = request(p, "changes.list", {})["items"][0]
    assert item["older"]["id"] == old and item["newer"]["id"] == new
    assert item["changes"] == [{"field": "follower_count", "old": 1, "new": 2}]


def test_all_page_types_honor_ceiling_and_equal_timestamp_order(monitoring_profile):
    p = monitoring_profile
    for i in range(1, 5):
        insert(p, stamp=3, payload=fields(follower_count=i))
    for operation, params in [
        ("snapshots.list", {"target_pk": "7"}),
        ("snapshots.targets", {"username": "alice"}),
        ("changes.list", {}),
    ]:
        first = request(p, operation, {**params, "limit": 1})
        late = insert(p, stamp=0, payload=fields(follower_count=100))
        seen = list(first["items"])
        cursor = first["next_cursor"]
        while cursor:
            page = request(p, operation, {**params, "limit": 1, "cursor": cursor})
            seen.extend(page["items"])
            cursor = page["next_cursor"]
        identifiers = [item.get("newer", item.get("snapshot"))["id"] for item in seen]
        assert late not in identifiers
        assert len(identifiers) == len(set(identifiers))


@pytest.mark.parametrize("payload,code", [('{"biography":"private-token",', "history_corrupt"),
                                          (json.dumps({"biography": "界" * 24000}, ensure_ascii=False), "history_oversized")])
@pytest.mark.parametrize("operation,params", [
    ("snapshots.list", {"target_pk": "7"}),
    ("snapshots.targets", {"username": "alice"}),
    ("changes.list", {}),
])
def test_bad_json_is_a_diagnostic_with_progress(monitoring_profile, payload, code, operation, params):
    p = monitoring_profile
    bad = insert(p, stamp=2, payload=payload)
    insert(p, stamp=1)
    page = request(p, operation, {**params, "limit": 1})
    assert page["items"] == [{"kind": "diagnostic", "snapshot": {
        "id": bad, "target_pk": "7", "captured_at": 2,
    }, "code": code}]
    assert page["next_cursor"] and not page["scan_complete"]
    assert "private-token" not in json.dumps(page)


def test_encoded_byte_limit_shortens_feed_without_losing_deferred_item(monitoring_profile):
    p = monitoring_profile
    identifiers = []
    for i in range(1, 52):
        identifiers.append(insert(p, stamp=i, payload=fields(biography=("界" if i % 2 else "語") * 21000)))
    page = request(p, "changes.list", {"target_pk": "7"})
    assert 0 < len(page["items"]) < 50
    all_items = []
    while True:
        wire = encode({"protocol_version": 1, "request_id": "x" * 64, "result": page})
        assert len(wire) < MAX_OUTPUT_BYTES
        assert page["scanned"] <= 200
        all_items.extend(page["items"])
        if page["next_cursor"] is None:
            break
        page = request(p, "changes.list", {"target_pk": "7", "cursor": page["next_cursor"]})
    seen = [item.get("newer", item.get("snapshot"))["id"] for item in all_items]
    assert seen == identifiers[::-1]
