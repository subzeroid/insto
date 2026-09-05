"""Saved-history desktop DTOs with one bounded read transaction per request."""

from __future__ import annotations

import json
from contextlib import closing
from typing import Any

from insto.desktop.database import check_deadline, read_database
from insto.desktop.errors import DesktopError
from insto.desktop.history_params import MAX_CURSOR, encode_cursor
from insto.desktop.profile import Profile
from insto.desktop.protocol import MAX_OUTPUT_BYTES, PROTOCOL_VERSION, encode
from insto.service.history_readonly import (
    Check,
    HistoryReadError,
    Reader,
    comparison,
    metadata,
    snapshot,
)


def _page(items: list[dict[str, Any]], cursor: str | None, scanned: int) -> dict[str, Any]:
    return {
        "items": items,
        "next_cursor": cursor,
        "scan_complete": cursor is None,
        "scanned": scanned,
    }


def _wire(result: dict[str, Any]) -> bytes:
    return encode({"protocol_version": PROTOCOL_VERSION, "request_id": "x" * 64, "result": result})


class PageBudget:
    def __init__(self, check: Check) -> None:
        self.check = check
        self.items: list[dict[str, Any]] = []
        check()
        self.used = len(_wire(_page([], "x" * MAX_CURSOR, 2000)))
        check()

    def add(self, item: dict[str, Any]) -> bool:
        self.check()
        size = len(
            json.dumps(item, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode(
                "ascii"
            )
        )
        self.check()
        needed = size + (1 if self.items else 0)
        if self.used + needed >= MAX_OUTPUT_BYTES:
            if not self.items:
                raise DesktopError("history_oversized")
            return False
        self.used += needed
        self.items.append(item)
        return True


def _pair(reader: Reader, params: dict[str, Any], check: Check) -> dict[str, Any]:
    before = reader.selected(int(params["older_id"]))
    after = reader.selected(int(params["newer_id"]))
    if before is None or after is None:
        raise DesktopError("snapshot_unavailable")
    older_meta, newer_meta = metadata(before), metadata(after)
    if older_meta.target_pk != params["target_pk"] or newer_meta.target_pk != params["target_pk"]:
        raise DesktopError("snapshot_identity_mismatch")
    if older_meta.key >= newer_meta.key:
        raise DesktopError("invalid_params")
    return comparison(snapshot(before, check), snapshot(after, check), check)


def _pages(reader: Reader, operation: str, params: dict[str, Any], check: Check) -> dict[str, Any]:
    searching = operation == "snapshots.targets"
    listing = operation == "snapshots.list"
    filter_value = params["username"] if searching else params["target_pk"]
    if params["cursor"] is None:
        ceiling, frontier = reader.ceiling(), None
    else:
        ceiling, frontier = params["cursor"]
    if ceiling == 0:
        return _page([], None, 0)
    cap = 2000 if searching else (params["limit"] if listing else 200)
    target_filter = None if searching else params["target_pk"]
    budget = PageBudget(check)
    seen: set[str] = set()
    scanned = 0
    more = False
    with closing(reader.rows(ceiling, frontier, target_filter, cap)) as rows:
        for row in rows:
            check()
            scanned += 1
            current_meta = metadata(row)
            item: dict[str, Any] | None = None
            matched_pk: str | None = None
            try:
                current = snapshot(row, check)
                if searching:
                    username = current.fields.get("username")
                    if username is None:
                        item = {
                            "kind": "diagnostic",
                            "snapshot": current_meta.dto(),
                            "code": "history_identity_unknown",
                        }
                    elif username.lower() == filter_value and current_meta.target_pk not in seen:
                        item = {
                            "kind": "target",
                            "target_pk": current_meta.target_pk,
                            "snapshot": current_meta.dto(),
                        }
                        matched_pk = current_meta.target_pk
                elif listing:
                    item = {"kind": "snapshot", "snapshot": current_meta.dto()}
                else:
                    previous = reader.predecessor(current_meta, ceiling)
                    if previous is None:
                        item = {"kind": "baseline", "snapshot": current_meta.dto()}
                    else:
                        difference = comparison(snapshot(previous, check), current, check)
                        if difference["changes"] or difference["unknown_fields"]:
                            item = difference
                            if difference["unknown_fields"]:
                                item["kind"] = "incomplete"
            except HistoryReadError as error:
                item = {"kind": "diagnostic", "snapshot": current_meta.dto(), "code": error.code}
            check()
            if item is not None and not budget.add(item):
                more = True
                break
            frontier = current_meta.key
            if matched_pk is not None:
                seen.add(matched_pk)
            if len(budget.items) >= params["limit"] or scanned >= cap:
                more = True
                break
    check()
    cursor = None
    if more:
        if frontier is None:
            raise DesktopError("history_oversized")
        cursor = encode_cursor(operation, filter_value, ceiling, frontier)
    check()
    return _page(budget.items, cursor, scanned)


def run(
    profile: Profile,
    operation: str,
    params: dict[str, Any],
    *,
    deadline: float,
) -> dict[str, Any]:
    """Consume already normalized parameters from the pure dispatch validator."""

    def check() -> None:
        check_deadline(deadline)

    check()
    try:
        with read_database(profile, deadline=deadline) as connection:
            reader = Reader(connection, check)
            result = (
                _pair(reader, params, check)
                if operation == "snapshots.compare"
                else _pages(
                    reader,
                    operation,
                    params,
                    check,
                )
            )
            check()
            if len(_wire(result)) >= MAX_OUTPUT_BYTES:
                raise DesktopError("history_oversized")
            check()
            return result
    except HistoryReadError as error:
        raise DesktopError(error.code) from None
