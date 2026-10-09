import json
import sqlite3
import time
from contextlib import closing, contextmanager

import pytest

from insto.desktop.errors import DesktopError
from insto.desktop.history import run
from insto.desktop.history_params import validate_params
from insto.desktop.protocol import MAX_OUTPUT_BYTES, encode
from insto.service.history import (
    _PROFILE_TRACKED_FIELDS,
    MEDIA_HASH_ALGO,
    MEDIA_HASH_ALGO_FIELD,
)


def hashes(profile, identifier, avatar=None, banner=None):
    with closing(sqlite3.connect(profile.home / "store.db")) as db, db:
        db.execute(
            "UPDATE snapshots SET avatar_url_hash=?, banner_url_hash=? WHERE id=?",
            (avatar, banner, identifier),
        )


def stable(**updates):
    """Tracked fields plus the per-row sentinel a current writer stamps."""
    return fields(**{**updates, MEDIA_HASH_ALGO_FIELD: MEDIA_HASH_ALGO})


def fields(**updates):
    value = dict.fromkeys(_PROFILE_TRACKED_FIELDS, None)
    value.update(username="alice", biography="", follower_count=1)
    value.update(updates)
    return value


def insert(profile, *, pk="7", stamp=1, payload=None, posts="[]", identifier=None):
    body = (
        json.dumps(fields() if payload is None else payload, ensure_ascii=False)
        if not isinstance(payload, str)
        else payload
    )
    with closing(sqlite3.connect(profile.home / "store.db")) as db, db:
        cursor = db.execute(
            "INSERT INTO snapshots(id,target_pk,captured_at,"
            "profile_fields_json,last_post_pks_json) "
            "VALUES (?,?,?,?,?)",
            (identifier, pk, stamp, body, posts),
        )
        return str(cursor.lastrowid)


def request(profile, operation, params):
    return run(
        profile, operation, validate_params(operation, params), deadline=time.monotonic() + 10
    )


def test_empty_one_snapshot_and_metadata_only_list(monitoring_profile):
    p = monitoring_profile
    assert request(p, "snapshots.list", {"target_pk": "7"}) == {
        "items": [],
        "next_cursor": None,
        "scan_complete": True,
        "scanned": 0,
    }
    identifier = insert(p, payload=fields(biography="private-observed-biography"))
    page = request(p, "snapshots.list", {"target_pk": "7"})
    assert page["items"] == [
        {
            "kind": "snapshot",
            "snapshot": {
                "id": identifier,
                "target_pk": "7",
                "captured_at": 1,
            },
        }
    ]
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


def test_read_reports_the_same_fields_and_typing_as_compare(monitoring_profile):
    p = monitoring_profile
    avatar, banner = "a" * 64, "b" * 64
    # Both rows carry the media-hash sentinel, so the hashes are comparable
    # and `read` and `compare` report them identically.
    payload = stable(
        full_name="Alice",
        external_url=None,
        is_verified=True,
        is_business=False,
        is_private=False,
        follower_count=3,
        following_count=4,
        media_count=5,
    )
    identifier = insert(p, stamp=2, payload=payload)
    hashes(p, identifier, avatar, banner)
    result = request(p, "snapshots.read", {"target_pk": "7", "snapshot_id": identifier})
    assert result == {
        "kind": "snapshot_fields",
        "snapshot": {"id": identifier, "target_pk": "7", "captured_at": 2},
        "fields": {
            "username": "alice",
            "full_name": "Alice",
            "biography": "",
            "external_url": None,
            "is_verified": True,
            "is_business": False,
            "is_private": False,
            "follower_count": 3,
            "following_count": 4,
            "media_count": 5,
            "public_email": None,
            "public_phone": None,
            "business_category": None,
            "avatar": avatar,
            "banner": banner,
        },
        "unknown_fields": [],
    }
    # The same names, the same values and the same hash treatment `compare` uses,
    # so the app can reuse one formatter for both.
    empty = insert(
        p,
        stamp=1,
        payload={
            **dict.fromkeys(_PROFILE_TRACKED_FIELDS, None),
            MEDIA_HASH_ALGO_FIELD: MEDIA_HASH_ALGO,
        },
    )
    difference = request(
        p, "snapshots.compare", {"target_pk": "7", "older_id": empty, "newer_id": identifier}
    )
    reported = {change["field"]: change["new"] for change in difference["changes"]}
    assert reported == {
        name: value for name, value in result["fields"].items() if value is not None
    }


def test_read_lists_unknown_fields_of_an_old_snapshot_in_compare_order(monitoring_profile):
    p = monitoring_profile
    identifier = insert(p, payload={"biography": None, "full_name": "Alice"})
    result = request(p, "snapshots.read", {"target_pk": "7", "snapshot_id": identifier})
    assert result["fields"] == {
        "full_name": "Alice",
        "biography": None,
        "avatar": None,
        "banner": None,
    }
    assert result["unknown_fields"] == [
        "username",
        "external_url",
        "is_verified",
        "is_business",
        "is_private",
        "follower_count",
        "following_count",
        "media_count",
        "public_email",
        "public_phone",
        "business_category",
    ]
    # `compare` sorts the same way: the tracked declaration order.
    newer = insert(p, stamp=2, payload={"biography": None, "full_name": "Alice"})
    difference = request(
        p, "snapshots.compare", {"target_pk": "7", "older_id": identifier, "newer_id": newer}
    )
    assert difference["unknown_fields"] == result["unknown_fields"]


def test_read_rejects_a_missing_foreign_or_unreadable_snapshot(monitoring_profile):
    p = monitoring_profile
    identifier = insert(p, stamp=1)
    foreign = insert(p, pk="8", stamp=2)
    corrupt = insert(p, stamp=3, payload="{private-old-token")
    with pytest.raises(DesktopError, match="snapshot_identity_mismatch"):
        request(p, "snapshots.read", {"target_pk": "7", "snapshot_id": foreign})
    with pytest.raises(DesktopError, match="history_corrupt") as caught:
        request(p, "snapshots.read", {"target_pk": "7", "snapshot_id": corrupt})
    assert "private-old-token" not in str(caught.value)
    with closing(sqlite3.connect(p.home / "store.db")) as db, db:
        db.execute("DELETE FROM snapshots WHERE id=?", (identifier,))
    with pytest.raises(DesktopError, match="snapshot_unavailable"):
        request(p, "snapshots.read", {"target_pk": "7", "snapshot_id": identifier})
    with pytest.raises(DesktopError, match="snapshot_unavailable"):
        request(p, "snapshots.read", {"target_pk": "7", "snapshot_id": "9223372036854775807"})


def test_read_refuses_an_oversized_snapshot(monitoring_profile):
    p = monitoring_profile
    identifier = insert(p, payload=json.dumps({"biography": "界" * 24000}, ensure_ascii=False))
    with pytest.raises(DesktopError, match="history_oversized"):
        request(p, "snapshots.read", {"target_pk": "7", "snapshot_id": identifier})


def test_read_never_opens_the_store_for_writing(monitoring_profile, monkeypatch):
    p = monitoring_profile
    identifier = insert(p)
    from insto.desktop import database
    from insto.desktop import history as desktop_history

    def forbidden(*args, **kwargs):
        raise AssertionError("snapshots.read must not open the store for writing")

    monkeypatch.setattr(database, "write_database", forbidden)
    monkeypatch.setattr(desktop_history, "write_database", forbidden, raising=False)
    modes: list[int] = []
    real = desktop_history.read_database

    @contextmanager
    def spy(profile, *, deadline):
        with real(profile, deadline=deadline) as connection:
            modes.append(connection.execute("PRAGMA query_only").fetchone()[0])
            yield connection

    monkeypatch.setattr(desktop_history, "read_database", spy)
    assert request(p, "snapshots.read", {"target_pk": "7", "snapshot_id": identifier})["kind"] == (
        "snapshot_fields"
    )
    assert modes == [1]


def test_read_honours_the_cooperative_deadline(monitoring_profile):
    p = monitoring_profile
    identifier = insert(p)
    with pytest.raises(DesktopError, match="operation_timeout"):
        run(
            p,
            "snapshots.read",
            validate_params("snapshots.read", {"target_pk": "7", "snapshot_id": identifier}),
            deadline=time.monotonic() - 1,
        )


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
        db.executemany(
            "INSERT INTO snapshots(target_pk,captured_at,profile_fields_json,last_post_pks_json) "
            "VALUES ('7',? ,?,'[]')",
            ((i, body) for i in range(1, 2001)),
        )
    first = request(p, "snapshots.targets", {"username": "alice"})
    assert first["scanned"] == 2000
    assert first["scan_complete"] is False and first["next_cursor"]
    assert {item["target_pk"] for item in first["items"]} == {"7"}
    second = request(p, "snapshots.targets", {"username": "alice", "cursor": first["next_cursor"]})
    assert {item["target_pk"] for item in second["items"]} == {"8"}
    assert second["scan_complete"] is True
    # A separate page boundary is allowed to repeat a PK; callers union the evidence.
    tiny = request(p, "snapshots.targets", {"username": "alice", "limit": 1})
    again = request(
        p, "snapshots.targets", {"username": "alice", "limit": 1, "cursor": tiny["next_cursor"]}
    )
    assert tiny["items"][0]["target_pk"] == again["items"][0]["target_pk"] == "7"


def test_unchanged_feed_can_continue_without_visible_items(monitoring_profile):
    p = monitoring_profile
    body = json.dumps(fields())
    with closing(sqlite3.connect(p.home / "store.db")) as db, db:
        db.executemany(
            "INSERT INTO snapshots(target_pk,captured_at,profile_fields_json,last_post_pks_json) "
            "VALUES ('7',?,?,'[]')",
            ((i, body) for i in range(1, 202)),
        )
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


@pytest.mark.parametrize(
    "payload,code",
    [
        ('{"biography":"private-token",', "history_corrupt"),
        (json.dumps({"biography": "界" * 24000}, ensure_ascii=False), "history_oversized"),
    ],
)
@pytest.mark.parametrize(
    "operation,params",
    [
        ("snapshots.list", {"target_pk": "7"}),
        ("snapshots.targets", {"username": "alice"}),
        ("changes.list", {}),
    ],
)
def test_bad_json_is_a_diagnostic_with_progress(
    monitoring_profile, payload, code, operation, params
):
    p = monitoring_profile
    bad = insert(p, stamp=2, payload=payload)
    insert(p, stamp=1)
    page = request(p, operation, {**params, "limit": 1})
    assert page["items"] == [
        {
            "kind": "diagnostic",
            "snapshot": {
                "id": bad,
                "target_pk": "7",
                "captured_at": 2,
            },
            "code": code,
        }
    ]
    assert page["next_cursor"] and not page["scan_complete"]
    assert "private-token" not in json.dumps(page)


def test_encoded_byte_limit_shortens_feed_without_losing_deferred_item(monitoring_profile):
    p = monitoring_profile
    identifiers = []
    for i in range(1, 52):
        identifiers.append(
            insert(p, stamp=i, payload=fields(biography=("界" if i % 2 else "語") * 21000))
        )
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


def test_unknown_username_is_visible_and_prevents_exhaustive_identity_claim(monitoring_profile):
    insert(monitoring_profile, payload={"biography": "legacy"})
    page = request(monitoring_profile, "snapshots.targets", {"username": "alice"})
    assert page["items"][0]["code"] == "history_identity_unknown"
    assert page["items"][0]["kind"] == "diagnostic"
    assert page["scan_complete"] is True


def test_visible_cap_is_fifty_for_every_page_operation(monitoring_profile):
    p = monitoring_profile
    for i in range(1, 62):
        insert(p, pk=str(i), stamp=i)
    for operation, params in [("changes.list", {}), ("snapshots.targets", {"username": "alice"})]:
        page = request(p, operation, params)
        assert len(page["items"]) == 50
        assert page["next_cursor"]
    for i in range(62, 123):
        insert(p, stamp=i)
    page = request(p, "snapshots.list", {"target_pk": "7"})
    assert len(page["items"]) == 50 and page["scanned"] == 50


def test_corrupt_selected_or_predecessor_is_never_empty_success(monitoring_profile):
    p = monitoring_profile
    first = insert(p, stamp=1, payload="{private-old-token")
    second = insert(p, stamp=2)
    with pytest.raises(DesktopError, match="history_corrupt"):
        request(p, "snapshots.compare", {"target_pk": "7", "older_id": first, "newer_id": second})
    feed = request(p, "changes.list", {})
    assert feed["items"][0]["kind"] == "diagnostic"
    assert feed["items"][0]["snapshot"]["id"] == second
    assert feed["items"][0]["code"] == "history_corrupt"
    assert "private-old-token" not in json.dumps(feed)


def test_invalid_ordering_metadata_fails_closed(monitoring_profile):
    p = monitoring_profile
    insert(p)
    with closing(sqlite3.connect(p.home / "store.db")) as db, db:
        db.execute("UPDATE snapshots SET captured_at='private-malformed-ordering'")
    with pytest.raises(DesktopError, match="history_corrupt") as caught:
        request(p, "snapshots.list", {"target_pk": "7"})
    assert "private-malformed-ordering" not in str(caught.value)


def test_invalid_utf8_rows_are_diagnostics_not_storage_errors(monitoring_profile):
    # Review gate G5: byte-level corruption in a current row, in a predecessor and
    # in an explicitly selected row stays on the per-record path; the request as a
    # whole never degrades to storage_error and the cursor keeps advancing.
    p = monitoring_profile
    bad_older = insert(p, stamp=1)
    good = insert(p, stamp=2)
    bad_newer = insert(p, stamp=3)
    with closing(sqlite3.connect(p.home / "store.db")) as db, db:
        db.execute(
            "UPDATE snapshots SET profile_fields_json=CAST(X'7B22FF223A317D' AS TEXT) "
            "WHERE id IN (?, ?)",
            (bad_older, bad_newer),
        )
    for operation, params in [
        ("snapshots.list", {"target_pk": "7"}),
        ("snapshots.targets", {"username": "alice"}),
        ("changes.list", {}),
    ]:
        page = request(p, operation, {**params, "limit": 1})
        assert page["items"] == [
            {
                "kind": "diagnostic",
                "snapshot": {
                    "id": bad_newer,
                    "target_pk": "7",
                    "captured_at": 3,
                },
                "code": "history_corrupt",
            }
        ]
        assert page["next_cursor"] and not page["scan_complete"]
    feed = request(p, "changes.list", {})
    assert [(item["kind"], item["snapshot"]["id"]) for item in feed["items"]] == [
        ("diagnostic", bad_newer),
        ("diagnostic", good),
        ("diagnostic", bad_older),
    ]
    with pytest.raises(DesktopError, match="history_corrupt"):
        request(p, "snapshots.compare", {"target_pk": "7", "older_id": bad_older, "newer_id": good})


def test_continuation_survives_rows_deleted_between_pages(monitoring_profile):
    # Review gate G13: retention or manual pruning between two page requests must
    # neither error nor repeat items; the keyset simply skips what is gone.
    p = monitoring_profile
    first = insert(p, stamp=1, payload=fields(follower_count=1))
    second = insert(p, stamp=2, payload=fields(follower_count=2))
    third = insert(p, stamp=3, payload=fields(follower_count=3))
    page = request(p, "snapshots.list", {"target_pk": "7", "limit": 1})
    assert [item["snapshot"]["id"] for item in page["items"]] == [third]
    feed = request(p, "changes.list", {"limit": 1})
    assert feed["items"][0]["older"]["id"] == second and feed["items"][0]["newer"]["id"] == third
    with closing(sqlite3.connect(p.home / "store.db")) as db, db:
        db.execute("DELETE FROM snapshots WHERE id=?", (second,))
    rest = request(
        p, "snapshots.list", {"target_pk": "7", "limit": 1, "cursor": page["next_cursor"]}
    )
    assert [item["snapshot"]["id"] for item in rest["items"]] == [first]
    assert rest["next_cursor"]  # conservative cursor at exactly the visible limit
    final = request(
        p, "snapshots.list", {"target_pk": "7", "limit": 1, "cursor": rest["next_cursor"]}
    )
    assert final == {"items": [], "next_cursor": None, "scan_complete": True, "scanned": 0}
    feed_rest = request(p, "changes.list", {"limit": 1, "cursor": feed["next_cursor"]})
    assert [item["kind"] for item in feed_rest["items"]] == ["baseline"]
    assert feed_rest["items"][0]["snapshot"]["id"] == first


def test_history_reads_under_held_profile_lease_have_no_application_writes(
    monitoring_profile, monkeypatch
):
    import os

    from insto.desktop import configuration
    from insto.service.history import HistoryStore

    p = monitoring_profile
    insert(p)
    original = (p.home / "store.db").read_bytes()
    before = {str(path.relative_to(p.root)) for path in p.root.rglob("*")}

    def forbidden(*args, **kwargs):
        raise AssertionError("history must use the shared reader")

    monkeypatch.setattr(configuration, "check_database", forbidden)
    monkeypatch.setattr(HistoryStore, "__init__", forbidden)
    with p.locked():
        assert request(p, "snapshots.list", {"target_pk": "7"})["items"]
    assert (p.home / "store.db").read_bytes() == original
    after = {str(path.relative_to(p.root)) for path in p.root.rglob("*")}
    assert after - before <= {"profile/store.db-wal", "profile/store.db-shm"}
    for suffix in ("-wal", "-shm"):
        path = p.home / ("store.db" + suffix)
        if path.exists():
            info = path.stat()
            assert info.st_uid == os.getuid() and info.st_mode & 0o777 == 0o600


def test_missing_history_profile_is_not_created(tmp_path):
    from insto.desktop.profile import Profile

    profile = Profile(tmp_path / "absent")
    with pytest.raises(DesktopError, match="not_configured"):
        request(profile, "changes.list", {})
    assert not profile.root.exists()


def test_cpu_timeout_after_decoding_discards_partial_page_and_closes_transaction(
    monitoring_profile, monkeypatch
):
    from insto.desktop import history as desktop_history
    from insto.service import history_readonly as saved

    p = monitoring_profile
    insert(p)
    original = saved._json
    checks = 0

    def expired(deadline):
        nonlocal checks
        checks += 1
        raise DesktopError("operation_timeout")

    def decode_then_expire(raw, check):
        result = original(raw, check)
        monkeypatch.setattr(desktop_history, "check_deadline", expired)
        return result

    monkeypatch.setattr(saved, "_json", decode_then_expire)
    with pytest.raises(DesktopError, match="operation_timeout"):
        request(p, "snapshots.list", {"target_pk": "7"})
    assert checks > 0
    with closing(sqlite3.connect(p.home / "store.db", timeout=0.1)) as writer:
        assert writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone() == (0, 0, 0)


def test_encoding_timeout_is_not_a_partial_success(monitoring_profile, monkeypatch):
    # The first `_wire` call is PageBudget's EMPTY envelope reservation. Expiring
    # there would never reach the populated final encoding or the deadline check
    # that follows it, so this test only expires once a populated page was encoded.
    from insto.desktop import history as desktop_history

    p = monitoring_profile
    insert(p)
    original = desktop_history._wire
    reached = []
    checks = 0

    def expired(deadline):
        nonlocal checks
        checks += 1
        raise DesktopError("operation_timeout")

    def expire_after_populated_encoding(result):
        encoded = original(result)
        if result["items"]:
            reached.append(len(result["items"]))
            monkeypatch.setattr(desktop_history, "check_deadline", expired)
        return encoded

    monkeypatch.setattr(desktop_history, "_wire", expire_after_populated_encoding)
    with pytest.raises(DesktopError, match="operation_timeout"):
        request(p, "snapshots.list", {"target_pk": "7"})
    assert reached == [1] and checks >= 1
    with closing(sqlite3.connect(p.home / "store.db", timeout=0.1)) as writer:
        assert writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone() == (0, 0, 0)


def test_timeout_on_second_candidate_discards_the_accumulated_first_item(
    monitoring_profile,
    monkeypatch,
):
    # The first candidate is fully accumulated before the second is decoded; an
    # expiry while handling the second must fail the whole request, never return
    # a one-item page. A second `snapshot` call proves the first item completed.
    from insto.desktop import history as desktop_history

    p = monitoring_profile
    older = insert(p, stamp=1)
    newer = insert(p, stamp=2)
    original_snapshot = desktop_history.snapshot
    original_wire = desktop_history._wire
    decoded = []
    populated = []

    def expired(deadline):
        raise DesktopError("operation_timeout")

    def decode_then_expire_on_second(row, check):
        current = original_snapshot(row, check)
        decoded.append(desktop_history.metadata(row).dto()["id"])
        if len(decoded) == 2:
            monkeypatch.setattr(desktop_history, "check_deadline", expired)
        return current

    def observe(result):
        if result["items"]:
            populated.append(result["items"])
        return original_wire(result)

    monkeypatch.setattr(desktop_history, "snapshot", decode_then_expire_on_second)
    monkeypatch.setattr(desktop_history, "_wire", observe)
    with pytest.raises(DesktopError, match="operation_timeout"):
        request(p, "snapshots.list", {"target_pk": "7"})
    assert decoded == [newer, older]
    assert populated == []
    with closing(sqlite3.connect(p.home / "store.db", timeout=0.1)) as writer:
        assert writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone() == (0, 0, 0)


def test_sqlite_progress_timeout_closes_the_history_transaction(monitoring_profile, monkeypatch):
    from insto.service.history_readonly import Reader

    p = monitoring_profile
    insert(p)

    def expensive(self, ceiling, frontier, target_pk, limit):
        cursor = self.connection.execute("""WITH RECURSIVE numbers(x) AS (
            SELECT 1 UNION ALL SELECT x+1 FROM numbers WHERE x < 1000000000
        ) SELECT sum(x) FROM numbers""")
        try:
            yield cursor.fetchone()
        finally:
            cursor.close()

    monkeypatch.setattr(Reader, "rows", expensive)
    started = time.monotonic()
    with pytest.raises(DesktopError, match="operation_timeout"):
        run(p, "changes.list", validate_params("changes.list", {}), deadline=started + 0.1)
    assert time.monotonic() - started < 1.5
    with closing(sqlite3.connect(p.home / "store.db", timeout=0.1)) as writer:
        assert writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone() == (0, 0, 0)


def test_page_is_one_wal_snapshot_and_reader_releases_it(monitoring_profile, monkeypatch):
    from insto.service.history_readonly import Reader

    p = monitoring_profile
    first = insert(p, stamp=1)
    original = Reader.ceiling
    inserted = []

    def commit_after_first_select(self):
        ceiling = original(self)
        inserted.append(insert(p, stamp=2))
        return ceiling

    monkeypatch.setattr(Reader, "ceiling", commit_after_first_select)
    page = request(p, "snapshots.list", {"target_pk": "7"})
    assert [item["snapshot"]["id"] for item in page["items"]] == [first]
    monkeypatch.setattr(Reader, "ceiling", original)
    next_page = request(p, "snapshots.list", {"target_pk": "7"})
    assert [item["snapshot"]["id"] for item in next_page["items"]] == [inserted[0], first]
    with closing(sqlite3.connect(p.home / "store.db", timeout=0.1)) as writer:
        assert writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone() == (0, 0, 0)


def test_sqlite_busy_is_bounded_and_explicit(monitoring_profile):
    p = monitoring_profile
    insert(p)
    writer = sqlite3.connect(p.home / "store.db", isolation_level=None)
    try:
        assert writer.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
        writer.execute("BEGIN EXCLUSIVE")
        assert not (p.home / "store.db-journal").exists()
        started = time.monotonic()
        with pytest.raises(DesktopError, match="profile_busy"):
            request(p, "snapshots.list", {"target_pk": "7"})
        assert time.monotonic() - started < 1.6
    finally:
        writer.rollback()
        writer.close()


def test_media_hashes_are_compared_only_when_both_rows_carry_the_algorithm(monitoring_profile):
    p = monitoring_profile
    old = insert(p, stamp=5, payload=stable())
    new = insert(p, stamp=9, payload=stable())
    hashes(p, old, avatar="a" * 64, banner="c" * 64)
    hashes(p, new, avatar="b" * 64, banner="d" * 64)
    params = {"target_pk": "7", "older_id": old, "newer_id": new}

    # Both rows carry it: a real swap is reported, through both operations.
    assert request(p, "snapshots.compare", params)["changes"] == [
        {"field": "avatar", "old": "a" * 64, "new": "b" * 64},
        {"field": "banner", "old": "c" * 64, "new": "d" * 64},
    ]
    feed = request(p, "changes.list", {"target_pk": "7"})
    assert [item["kind"] for item in feed["items"]] == ["comparison", "baseline"]
    assert feed["items"][0]["unknown_fields"] == []
    assert MEDIA_HASH_ALGO_FIELD not in json.dumps(feed)


def _legacy_pair(profile, legacy):
    """A pair where `legacy` says which side an older insto wrote (no sentinel)."""
    older_payload = fields() if legacy in ("older", "both") else stable()
    newer_payload = fields() if legacy in ("newer", "both") else stable()
    old = insert(profile, stamp=5, payload=older_payload)
    new = insert(profile, stamp=9, payload=newer_payload)
    hashes(profile, old, avatar="a" * 64, banner="c" * 64)
    hashes(profile, new, avatar="b" * 64, banner="d" * 64)
    return old, new


@pytest.mark.parametrize("legacy", ["older", "newer", "both"])
def test_compare_never_reports_a_row_without_the_algorithm_in_either_position(
    monitoring_profile, legacy
):
    """An old insto sharing the store writes whole-URL digests before *and* after."""
    p = monitoring_profile
    old, new = _legacy_pair(p, legacy)
    result = request(p, "snapshots.compare", {"target_pk": "7", "older_id": old, "newer_id": new})
    assert result["changes"] == []
    assert result["unknown_fields"] == []
    # `snapshots.read` is not gated: it reports the stored digest as it is.
    stored = request(p, "snapshots.read", {"target_pk": "7", "snapshot_id": old})
    assert stored["fields"]["avatar"] == "a" * 64
    assert stored["fields"]["banner"] == "c" * 64


@pytest.mark.parametrize("legacy", ["older", "newer", "both"])
def test_the_feed_yields_no_item_for_a_pair_without_the_algorithm(monitoring_profile, legacy):
    p = monitoring_profile
    old, _ = _legacy_pair(p, legacy)
    # Nothing else differs, so the feed shows only the baseline - no item at all,
    # exactly as for an unchanged pair.
    assert request(p, "changes.list", {"target_pk": "7"})["items"] == [
        {"kind": "baseline", "snapshot": {"id": old, "target_pk": "7", "captured_at": 5}}
    ]


def test_the_algorithm_sentinel_never_reaches_a_desktop_result(monitoring_profile):
    p = monitoring_profile
    old = insert(p, stamp=5, payload=stable(username="alice"))
    new = insert(p, stamp=9, payload=stable(username="renamed"))
    hashes(p, old, avatar="a" * 64)
    hashes(p, new, avatar="b" * 64)
    surfaces = [
        request(p, "snapshots.read", {"target_pk": "7", "snapshot_id": new}),
        request(p, "snapshots.compare", {"target_pk": "7", "older_id": old, "newer_id": new}),
        request(p, "changes.list", {"target_pk": "7"}),
        request(p, "changes.list", {}),
        request(p, "snapshots.list", {"target_pk": "7"}),
        request(p, "snapshots.targets", {"username": "alice"}),
    ]
    for result in surfaces:
        assert MEDIA_HASH_ALGO_FIELD not in json.dumps(result)
    read = surfaces[0]
    assert MEDIA_HASH_ALGO_FIELD not in read["fields"]
    assert MEDIA_HASH_ALGO_FIELD not in read["unknown_fields"]
    assert set(read["fields"]) == {*_PROFILE_TRACKED_FIELDS, "avatar", "banner"}
    compared = surfaces[1]
    assert [change["field"] for change in compared["changes"]] == ["username", "avatar"]
    assert compared["unknown_fields"] == []


def test_an_unknown_extra_field_is_ignored_rather_than_rejected(monitoring_profile):
    """The property that lets an older insto read a newer row: extras are skipped."""
    p = monitoring_profile
    payload = stable(username="alice")
    payload["_some_future_key"] = 7
    identifier = insert(p, stamp=1, payload=payload)
    result = request(p, "snapshots.read", {"target_pk": "7", "snapshot_id": identifier})
    assert result["kind"] == "snapshot_fields"
    assert "_some_future_key" not in json.dumps(result)


def test_comparing_never_writes_into_the_store(monitoring_profile, monkeypatch):
    p = monitoring_profile
    old = insert(p, stamp=5, payload=stable())
    new = insert(p, stamp=9, payload=stable())
    hashes(p, old, avatar="a" * 64)
    hashes(p, new, avatar="b" * 64)
    from insto.desktop import database
    from insto.desktop import history as desktop_history

    def forbidden(*args, **kwargs):
        raise AssertionError("the saved-history reader must never open the store for writing")

    monkeypatch.setattr(database, "write_database", forbidden)
    monkeypatch.setattr(desktop_history, "write_database", forbidden, raising=False)
    with closing(sqlite3.connect(p.home / "store.db")) as db:
        before = db.execute("SELECT COUNT(*) FROM _meta").fetchone()[0]
    request(p, "snapshots.compare", {"target_pk": "7", "older_id": old, "newer_id": new})
    request(p, "changes.list", {"target_pk": "7"})
    with closing(sqlite3.connect(p.home / "store.db")) as db:
        assert db.execute("SELECT COUNT(*) FROM _meta").fetchone()[0] == before


def _posts_pair(profile, older, newer):
    old = insert(profile, stamp=5, payload=stable(), posts=json.dumps(older))
    new = insert(profile, stamp=9, payload=stable(), posts=json.dumps(newer))
    params = {"target_pk": "7", "older_id": old, "newer_id": new}
    return request(profile, "snapshots.compare", params)["posts"]


@pytest.mark.parametrize(
    ("older", "newer", "expected"),
    [
        # Two posts published since: the oldest one merely left the window.
        (["30", "20", "10"], ["50", "40", "30"], {"added": ["50", "40"], "window_full": False}),
        # A pinned old post entering the window is not new.
        (["100", "90"], ["5", "110", "100"], {"added": ["110"], "window_full": False}),
        # A deleted post letting an older one back in is not new either.
        (["30", "20"], ["30", "10"], {"added": [], "window_full": False}),
        # Every post in the newer window is new: more may lie beyond it.
        (["1"], ["3", "2"], {"added": ["3", "2"], "window_full": True}),
        (["1"], ["3", "3"], {"added": ["3"], "window_full": True}),
        (["2", "1"], ["2", "1"], {"added": [], "window_full": False}),
    ],
)
def test_compare_reports_posts_published_after_the_older_window(
    monitoring_profile, older, newer, expected
):
    assert _posts_pair(monitoring_profile, older, newer) == expected


@pytest.mark.parametrize(
    ("older", "newer"),
    [
        ([], ["2", "1"]),
        (["2", "1"], []),
        (["01"], ["2"]),
        (["1"], ["2_12345"]),
    ],
)
def test_posts_are_not_comparable_without_two_canonical_windows(monitoring_profile, older, newer):
    assert _posts_pair(monitoring_profile, older, newer) is None


def test_feed_lists_a_pair_whose_only_difference_is_a_new_post(monitoring_profile):
    p = monitoring_profile
    insert(p, stamp=1, payload=stable(), posts='["10"]')
    same = insert(p, stamp=2, payload=stable(), posts='["10"]')
    published = insert(p, stamp=3, payload=stable(), posts='["20","10"]')
    feed = request(p, "changes.list", {"target_pk": "7"})
    assert [item["kind"] for item in feed["items"]] == ["comparison", "baseline"]
    item = feed["items"][0]
    assert item["older"]["id"] == same and item["newer"]["id"] == published
    assert item["changes"] == []
    assert item["posts"] == {"added": ["20"], "window_full": False}
