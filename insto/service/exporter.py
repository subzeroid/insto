"""Versioned JSON / flat CSV exporters.

Every JSON file insto writes has a stable envelope:

    {
        "_schema": "insto.v1",
        "command": "<cmd>",
        "target":  "@<user>" | null,
        "captured_at": "<ISO-8601 UTC>",
        "data": <payload>,
    }

Bumping `_schema` is a versioning event — readers (Maltego transform, Linear
imports, custom scripts) pin against this string. The wrapper exists so
downstream code never has to guess what an exported blob is or when it was
captured.

CSV export is allowed only on a small set of *flat-row* commands listed in
`CSV_FLAT_COMMANDS`. Anything nested (a `Profile` with arrays, a `/info`
report) raises `ValueError` with the list of CSV-eligible commands. CSV
deliberately has *no* `_schema` line — it is meant to be loaded by tools
that do not understand insto's envelope (Excel, awk, csvkit). Versioning
lives in JSON only.

Both writers accept either a `Path` (file gets created, parents made) or
a writable binary stream (`io.BytesIO`, `sys.stdout.buffer`). The stream
form is what powers `--json -` / `--csv -` pipeline mode in Task 15.
"""

from __future__ import annotations

import csv
import dataclasses
import io
import json
import logging
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

SCHEMA_VERSION = "insto.v1"

CSV_FLAT_COMMANDS: frozenset[str] = frozenset(
    {
        "followers",
        "followings",
        "mutuals",
        "similar",
        "search",
        "comments",
        "likes",
        "wcommented",
        "wtagged",
        "hashtags",
        "mentions",
        "locations",
        "captions",
        "posts",
    }
)

# Subset of flat commands whose rows carry a single canonical entity per row
# and therefore make sense as a Maltego entity-import CSV. `/captions`,
# `/likes`, `/comments` are flat but row-shaped around posts/comments and
# do not map cleanly to one Maltego entity per row.
MALTEGO_COMMANDS: frozenset[str] = frozenset(
    {
        "followers",
        "followings",
        "mutuals",
        "similar",
        "search",
        "wcommented",
        "wtagged",
        "hashtags",
        "mentions",
        "locations",
    }
)

# Short kinds used by the command layer → Maltego built-in entity type
# literals. Callers may also pass a `maltego.<Type>` literal directly,
# which is forwarded unchanged.
MALTEGO_ENTITY_TYPES: dict[str, str] = {
    "user": "maltego.Person",
    "mention": "maltego.Person",
    "hashtag": "maltego.Phrase",
    "location": "maltego.GPS",
}

MALTEGO_HEADER: tuple[str, ...] = ("Type", "Value", "Weight", "Notes", "Properties")

_logger = logging.getLogger("insto.exporter")


def _now_iso_utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


_SAFE_TARGET_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _normalize_target(target: str | None) -> str:
    """Reduce `target` to a safe filesystem path segment for `default_export_path`.

    The user-input target boundary already runs through `_validate_username`,
    but DTO-derived targets (e.g. `profile.username` from the backend) reach
    this helper too. Substituting `_` for anything containing a path separator
    or `..` keeps a drifted / malicious payload from escaping `output_dir`.
    """
    if target is None:
        return "_"
    cleaned = target.lstrip("@").strip()
    if not cleaned or cleaned in (".", "..") or not _SAFE_TARGET_RE.fullmatch(cleaned):
        return "_"
    return cleaned


def default_export_path(
    *,
    command: str,
    target: str | None,
    ext: str,
    output_dir: Path | None = None,
) -> Path:
    """Return `./output/<user>/<cmd>.<ext>` (or `<output_dir>/...`)."""
    base = output_dir if output_dir is not None else Path("output")
    return base / _normalize_target(target) / f"{command}.{ext}"


def _json_default(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    raise TypeError(f"object of type {type(obj).__name__} is not JSON serializable")


def to_json(
    payload: Any,
    *,
    command: str,
    target: str | None,
    dest: Path | IO[bytes],
) -> Path | None:
    """Write the schema-wrapped payload to `dest`. Return Path if file, else None."""
    envelope: dict[str, Any] = {
        "_schema": SCHEMA_VERSION,
        "command": command,
        "target": target,
        "captured_at": _now_iso_utc(),
        "data": payload,
    }
    blob = json.dumps(
        envelope,
        default=_json_default,
        ensure_ascii=False,
        indent=2,
        sort_keys=False,
    ).encode("utf-8")
    return _write(blob, dest)


def to_csv(
    rows: Iterable[Mapping[str, Any]],
    *,
    command: str,
    target: str | None,
    dest: Path | IO[bytes],
) -> Path | None:
    """Write `rows` as flat CSV. Raise ValueError if `command` is not flat."""
    if command not in CSV_FLAT_COMMANDS:
        flat_list = ", ".join(sorted(CSV_FLAT_COMMANDS))
        raise ValueError(
            f"/{command} is not a flat-row command and cannot be exported as CSV; "
            f"use --json instead. Flat-row commands that support --csv: {flat_list}"
        )
    rows_list = list(rows)
    if not rows_list:
        return _write(b"", dest)

    header: list[str] = []
    seen: set[str] = set()
    for row in rows_list:
        for key in row:
            if key not in seen:
                header.append(key)
                seen.add(key)

    buf = io.StringIO(newline="")
    writer = csv.DictWriter(buf, fieldnames=header, extrasaction="ignore")
    writer.writeheader()
    for row in rows_list:
        writer.writerow({k: _csv_value(row.get(k)) for k in header})
    return _write(buf.getvalue().encode("utf-8"), dest)


def _resolve_maltego_type(entity_type: str) -> str:
    """Resolve a short kind (`user`, `hashtag`, ...) into a Maltego type literal.

    A literal already starting with `maltego.` is forwarded unchanged so
    callers can pass a custom type. Anything else must be a known short
    kind, otherwise `ValueError` is raised with the list of valid kinds.
    """
    if entity_type in MALTEGO_ENTITY_TYPES:
        return MALTEGO_ENTITY_TYPES[entity_type]
    if entity_type.startswith("maltego."):
        return entity_type
    valid = ", ".join(sorted(MALTEGO_ENTITY_TYPES))
    raise ValueError(
        f"unknown entity_type {entity_type!r}; expected a Maltego type "
        f"literal (e.g. 'maltego.Person') or one of: {valid}"
    )


def to_maltego_csv(
    rows: Iterable[Mapping[str, Any]],
    *,
    entity_type: str,
    dest: Path | IO[bytes],
) -> Path | None:
    """Write `rows` as a Maltego entity-import CSV.

    The CSV always has the same header: `Type, Value, Weight, Notes,
    Properties`. Maltego's bulk-importer reads exactly those columns; the
    Properties column is a JSON-encoded blob of every other key in the
    source row, so analysts can re-create attributes without inventing a
    new column per project.

    Each row must carry a non-empty `value` key — that becomes the entity's
    canonical value (a username, hashtag, location name, etc.). Reserved
    keys `weight` (int, default 1) and `notes` (str, default empty) populate
    their own columns. Every remaining key is sorted and JSON-dumped into
    the `Properties` column.

    Rows sharing a `value` are deduplicated (first occurrence wins). Each
    drop is logged at WARNING so an analyst pulling overlapping windows
    (e.g. followers + mutuals into one Maltego graph) can see what was
    collapsed.
    """
    type_literal = _resolve_maltego_type(entity_type)
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL)
    writer.writerow(MALTEGO_HEADER)

    seen: set[str] = set()
    for raw in rows:
        value = raw.get("value")
        if value is None:
            continue
        value_str = str(value).strip()
        if not value_str:
            continue
        if value_str in seen:
            _logger.warning(
                "to_maltego_csv: duplicate %s %r dropped (first occurrence kept)",
                type_literal,
                value_str,
            )
            continue
        seen.add(value_str)
        weight = raw.get("weight", 1)
        weight_str = str(weight) if weight is not None else "1"
        notes = raw.get("notes")
        notes_str = "" if notes is None else str(notes)
        value_str = _escape_formula(value_str)
        notes_str = _escape_formula(notes_str)
        props = {k: v for k, v in raw.items() if k not in {"value", "weight", "notes"}}
        if props:
            props_blob = json.dumps(
                props,
                default=_json_default,
                ensure_ascii=False,
                sort_keys=True,
            )
        else:
            props_blob = ""
        writer.writerow([type_literal, value_str, weight_str, notes_str, props_blob])

    return _write(buf.getvalue().encode("utf-8"), dest)


_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _escape_formula(value: str) -> str:
    """Prefix `'` to defang spreadsheet formula injection on user-controlled text.

    Targets like Instagram bios, captions, or usernames can begin with `=`,
    `+`, `-`, or `@` and would otherwise be executed as formulas when an
    insto CSV is opened in Excel / Google Sheets.
    """
    if value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def _csv_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, (list, tuple)):
        return _escape_formula(",".join(str(v) for v in value))
    if isinstance(value, str):
        return _escape_formula(value)
    # Defensive: any other scalar (datetime, custom DTO with __str__) flows
    # through DictWriter's implicit `str()` — formula-escape it explicitly so
    # adding a new flat-row command can't reopen the CSV-injection hole.
    return _escape_formula(str(value))


def _write(blob: bytes, dest: Path | IO[bytes]) -> Path | None:
    if isinstance(dest, Path):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(blob)
        return dest
    dest.write(blob)
    return None
