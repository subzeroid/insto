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
        "comments",
        "likes",
        "wcommented",
        "wtagged",
        "hashtags",
        "mentions",
        "locations",
        "captions",
    }
)


def _now_iso_utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_target(target: str | None) -> str:
    if target is None:
        return "_"
    cleaned = target.lstrip("@").strip()
    return cleaned or "_"


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


def _csv_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ",".join(str(v) for v in value)
    return value


def _write(blob: bytes, dest: Path | IO[bytes]) -> Path | None:
    if isinstance(dest, Path):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(blob)
        return dest
    dest.write(blob)
    return None
