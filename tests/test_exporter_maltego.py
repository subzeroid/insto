"""Tests for the Maltego CSV exporter."""

from __future__ import annotations

import csv as csv_module
import io
import json
import logging
from pathlib import Path

import pytest

from insto.service.exporter import (
    MALTEGO_COMMANDS,
    MALTEGO_ENTITY_TYPES,
    MALTEGO_HEADER,
    to_maltego_csv,
)


def _read(dest: Path) -> list[list[str]]:
    return list(csv_module.reader(io.StringIO(dest.read_text("utf-8"))))


def test_header_is_fixed() -> None:
    assert MALTEGO_HEADER == ("Type", "Value", "Weight", "Notes", "Properties")


def test_entity_type_map_covers_required_kinds() -> None:
    assert MALTEGO_ENTITY_TYPES["user"] == "maltego.Person"
    assert MALTEGO_ENTITY_TYPES["mention"] == "maltego.Person"
    assert MALTEGO_ENTITY_TYPES["hashtag"] == "maltego.Phrase"
    assert MALTEGO_ENTITY_TYPES["location"] == "maltego.GPS"


def test_maltego_commands_subset_of_csv_flat() -> None:
    from insto.service.exporter import CSV_FLAT_COMMANDS

    assert MALTEGO_COMMANDS <= CSV_FLAT_COMMANDS


def test_writes_header_and_rows(tmp_path: Path) -> None:
    dest = tmp_path / "followers.maltego.csv"
    rows = [
        {"value": "alice", "weight": 1, "notes": "Alice A.", "pk": "1"},
        {"value": "bob", "weight": 1, "notes": "Bob B.", "pk": "2"},
    ]
    result = to_maltego_csv(rows, entity_type="user", dest=dest)
    assert result == dest

    parsed = _read(dest)
    assert parsed[0] == ["Type", "Value", "Weight", "Notes", "Properties"]
    assert parsed[1][0] == "maltego.Person"
    assert parsed[1][1] == "alice"
    assert parsed[1][2] == "1"
    assert parsed[1][3] == "Alice A."
    props = json.loads(parsed[1][4])
    assert props == {"pk": "1"}


def test_empty_rows_writes_header_only(tmp_path: Path) -> None:
    dest = tmp_path / "f.csv"
    to_maltego_csv([], entity_type="user", dest=dest)
    parsed = _read(dest)
    assert parsed == [list(MALTEGO_HEADER)]


def test_writes_to_binary_stream() -> None:
    buf = io.BytesIO()
    rows = [{"value": "alice"}]
    result = to_maltego_csv(rows, entity_type="user", dest=buf)
    assert result is None
    text = buf.getvalue().decode("utf-8")
    lines = list(csv_module.reader(io.StringIO(text)))
    assert lines[0] == list(MALTEGO_HEADER)
    assert lines[1][0] == "maltego.Person"
    assert lines[1][1] == "alice"


def test_creates_parent_dirs(tmp_path: Path) -> None:
    dest = tmp_path / "deep" / "nested" / "x.maltego.csv"
    to_maltego_csv([{"value": "x"}], entity_type="user", dest=dest)
    assert dest.exists()


def test_short_kinds_resolve_to_maltego_types(tmp_path: Path) -> None:
    cases = {
        "user": "maltego.Person",
        "mention": "maltego.Person",
        "hashtag": "maltego.Phrase",
        "location": "maltego.GPS",
    }
    for short, literal in cases.items():
        dest = tmp_path / f"{short}.csv"
        to_maltego_csv([{"value": "x"}], entity_type=short, dest=dest)
        parsed = _read(dest)
        assert parsed[1][0] == literal, short


def test_literal_maltego_type_passes_through(tmp_path: Path) -> None:
    dest = tmp_path / "x.csv"
    to_maltego_csv([{"value": "Berlin"}], entity_type="maltego.City", dest=dest)
    parsed = _read(dest)
    assert parsed[1][0] == "maltego.City"


def test_unknown_entity_type_raises(tmp_path: Path) -> None:
    dest = tmp_path / "x.csv"
    with pytest.raises(ValueError) as ei:
        to_maltego_csv([{"value": "x"}], entity_type="banana", dest=dest)
    msg = str(ei.value)
    assert "banana" in msg
    for kind in ("user", "hashtag", "location", "mention"):
        assert kind in msg


def test_default_weight_is_one(tmp_path: Path) -> None:
    dest = tmp_path / "x.csv"
    to_maltego_csv([{"value": "x"}], entity_type="user", dest=dest)
    parsed = _read(dest)
    assert parsed[1][2] == "1"


def test_default_notes_is_empty(tmp_path: Path) -> None:
    dest = tmp_path / "x.csv"
    to_maltego_csv([{"value": "x"}], entity_type="user", dest=dest)
    parsed = _read(dest)
    assert parsed[1][3] == ""


def test_no_extra_props_writes_empty_properties(tmp_path: Path) -> None:
    dest = tmp_path / "x.csv"
    to_maltego_csv([{"value": "x"}], entity_type="user", dest=dest)
    parsed = _read(dest)
    assert parsed[1][4] == ""


def test_properties_are_json_encoded_in_one_column(tmp_path: Path) -> None:
    dest = tmp_path / "x.csv"
    rows = [
        {
            "value": "alice",
            "pk": "42",
            "is_private": True,
            "is_verified": False,
            "rank": 1,
        }
    ]
    to_maltego_csv(rows, entity_type="user", dest=dest)
    parsed = _read(dest)
    # exactly five columns regardless of how many extra keys there are
    assert len(parsed[1]) == 5
    props = json.loads(parsed[1][4])
    assert props == {
        "pk": "42",
        "is_private": True,
        "is_verified": False,
        "rank": 1,
    }


def test_properties_keys_sorted(tmp_path: Path) -> None:
    dest = tmp_path / "x.csv"
    to_maltego_csv(
        [{"value": "alice", "z_last": 1, "a_first": 2, "m_mid": 3}],
        entity_type="user",
        dest=dest,
    )
    parsed = _read(dest)
    # Sorted order makes diffs stable across runs.
    assert parsed[1][4] == '{"a_first": 2, "m_mid": 3, "z_last": 1}'


def test_commas_in_value_are_escaped(tmp_path: Path) -> None:
    dest = tmp_path / "x.csv"
    to_maltego_csv(
        [{"value": "Berlin, DE", "notes": "city,state"}],
        entity_type="location",
        dest=dest,
    )
    raw = dest.read_text("utf-8")
    assert '"Berlin, DE"' in raw
    assert '"city,state"' in raw
    parsed = _read(dest)
    assert parsed[1][1] == "Berlin, DE"
    assert parsed[1][3] == "city,state"


def test_double_quotes_in_value_are_escaped(tmp_path: Path) -> None:
    dest = tmp_path / "x.csv"
    to_maltego_csv(
        [{"value": 'name "the rock"', "notes": 'he said "hi"'}],
        entity_type="user",
        dest=dest,
    )
    parsed = _read(dest)
    assert parsed[1][1] == 'name "the rock"'
    assert parsed[1][3] == 'he said "hi"'


def test_dedup_by_value_keeps_first(tmp_path: Path) -> None:
    """One user appearing in both followers and mutuals → one row, first wins."""
    dest = tmp_path / "x.csv"
    rows = [
        {"value": "alice", "weight": 1, "notes": "from followers"},
        {"value": "bob", "weight": 1, "notes": "from followers"},
        {"value": "alice", "weight": 1, "notes": "from mutuals"},
    ]
    to_maltego_csv(rows, entity_type="user", dest=dest)
    parsed = _read(dest)
    # header + 2 unique rows
    assert len(parsed) == 3
    assert [r[1] for r in parsed[1:]] == ["alice", "bob"]
    # First occurrence's notes survives.
    assert parsed[1][3] == "from followers"


def test_dedup_logs_warning(tmp_path: Path) -> None:
    """A duplicate `value` triggers a WARNING-level log on `insto.exporter`."""
    dest = tmp_path / "x.csv"
    rows = [
        {"value": "alice"},
        {"value": "alice"},
    ]
    captured: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    handler = _Capture(level=logging.WARNING)
    logger = logging.getLogger("insto.exporter")
    logger.addHandler(handler)
    prior_level = logger.level
    logger.setLevel(logging.WARNING)
    try:
        to_maltego_csv(rows, entity_type="user", dest=dest)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prior_level)

    assert any(
        record.levelno == logging.WARNING
        and "duplicate" in record.getMessage()
        and "alice" in record.getMessage()
        for record in captured
    ), [r.getMessage() for r in captured]


def test_blank_value_is_skipped(tmp_path: Path) -> None:
    dest = tmp_path / "x.csv"
    rows = [
        {"value": ""},
        {"value": None},
        {"value": "   "},
        {"value": "alice"},
    ]
    to_maltego_csv(rows, entity_type="user", dest=dest)
    parsed = _read(dest)
    # header + alice only
    assert len(parsed) == 2
    assert parsed[1][1] == "alice"


def test_weight_can_be_int(tmp_path: Path) -> None:
    dest = tmp_path / "x.csv"
    to_maltego_csv(
        [{"value": "art", "weight": 42}],
        entity_type="hashtag",
        dest=dest,
    )
    parsed = _read(dest)
    assert parsed[1][2] == "42"


def test_returns_path_for_path_dest(tmp_path: Path) -> None:
    dest = tmp_path / "x.csv"
    result = to_maltego_csv([{"value": "x"}], entity_type="user", dest=dest)
    assert result == dest


def test_returns_none_for_stream_dest() -> None:
    result = to_maltego_csv([{"value": "x"}], entity_type="user", dest=io.BytesIO())
    assert result is None
