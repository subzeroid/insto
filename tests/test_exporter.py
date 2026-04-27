"""Tests for insto.service.exporter."""

from __future__ import annotations

import csv as csv_module
import io
import json
import re
from pathlib import Path

import pytest

from insto.models import User
from insto.service.exporter import (
    CSV_FLAT_COMMANDS,
    SCHEMA_VERSION,
    default_export_path,
    to_csv,
    to_json,
)


def test_schema_version_is_v1() -> None:
    assert SCHEMA_VERSION == "insto.v1"


def test_default_export_path_basic() -> None:
    p = default_export_path(command="info", target="cristiano", ext="json")
    assert p == Path("output") / "cristiano" / "info.json"


def test_default_export_path_strips_at_and_handles_none() -> None:
    assert default_export_path(command="followers", target="@nasa", ext="csv") == (
        Path("output") / "nasa" / "followers.csv"
    )
    assert default_export_path(command="quota", target=None, ext="json") == (
        Path("output") / "_" / "quota.json"
    )


def test_default_export_path_custom_output_dir(tmp_path: Path) -> None:
    p = default_export_path(command="posts", target="@u", ext="json", output_dir=tmp_path)
    assert p == tmp_path / "u" / "posts.json"


def test_to_json_writes_schema_envelope(tmp_path: Path) -> None:
    dest = tmp_path / "out.json"
    to_json({"foo": "bar"}, command="info", target="@nasa", dest=dest)
    blob = json.loads(dest.read_text("utf-8"))
    assert blob["_schema"] == "insto.v1"
    assert blob["command"] == "info"
    assert blob["target"] == "@nasa"
    assert blob["data"] == {"foo": "bar"}
    assert "captured_at" in blob


def test_to_json_captured_at_is_iso_utc(tmp_path: Path) -> None:
    dest = tmp_path / "out.json"
    to_json({}, command="info", target="@n", dest=dest)
    blob = json.loads(dest.read_text("utf-8"))
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", blob["captured_at"]), blob[
        "captured_at"
    ]


def test_to_json_serializes_dataclass(tmp_path: Path) -> None:
    dest = tmp_path / "u.json"
    user = User(pk="42", username="nasa", full_name="NASA")
    to_json(user, command="info", target="@nasa", dest=dest)
    blob = json.loads(dest.read_text("utf-8"))
    assert blob["data"] == {
        "pk": "42",
        "username": "nasa",
        "full_name": "NASA",
        "is_private": False,
        "is_verified": False,
    }


def test_to_json_serializes_nested_dataclass_list(tmp_path: Path) -> None:
    dest = tmp_path / "u.json"
    users = [User(pk="1", username="a"), User(pk="2", username="b")]
    to_json(users, command="followers", target="@u", dest=dest)
    blob = json.loads(dest.read_text("utf-8"))
    assert [u["username"] for u in blob["data"]] == ["a", "b"]


def test_to_json_writes_to_binary_stream() -> None:
    buf = io.BytesIO()
    to_json({"x": 1}, command="info", target="@u", dest=buf)
    blob = json.loads(buf.getvalue().decode("utf-8"))
    assert blob["data"] == {"x": 1}
    assert blob["_schema"] == "insto.v1"


def test_to_json_creates_parent_dirs(tmp_path: Path) -> None:
    dest = tmp_path / "deep" / "nested" / "info.json"
    to_json({}, command="info", target="@u", dest=dest)
    assert dest.exists()


def test_to_json_target_can_be_none(tmp_path: Path) -> None:
    dest = tmp_path / "q.json"
    to_json({"remaining": 100}, command="quota", target=None, dest=dest)
    blob = json.loads(dest.read_text("utf-8"))
    assert blob["target"] is None


def test_to_json_returns_path_for_path_dest(tmp_path: Path) -> None:
    dest = tmp_path / "out.json"
    result = to_json({}, command="info", target="@u", dest=dest)
    assert result == dest


def test_to_json_returns_none_for_stream_dest() -> None:
    assert to_json({}, command="info", target="@u", dest=io.BytesIO()) is None


def test_to_csv_writes_header_and_rows(tmp_path: Path) -> None:
    dest = tmp_path / "followers.csv"
    rows = [
        {"pk": "1", "username": "alice"},
        {"pk": "2", "username": "bob"},
    ]
    to_csv(rows, command="followers", target="@nasa", dest=dest)
    parsed = list(csv_module.reader(io.StringIO(dest.read_text("utf-8"))))
    assert parsed[0] == ["pk", "username"]
    assert parsed[1] == ["1", "alice"]
    assert parsed[2] == ["2", "bob"]


def test_to_csv_writes_to_binary_stream() -> None:
    buf = io.BytesIO()
    rows = [{"pk": "1", "username": "x"}]
    to_csv(rows, command="followers", target="@u", dest=buf)
    text = buf.getvalue().decode("utf-8")
    assert text.splitlines()[0] == "pk,username"


def test_to_csv_rejects_non_flat_command(tmp_path: Path) -> None:
    dest = tmp_path / "info.csv"
    with pytest.raises(ValueError) as ei:
        to_csv([{"a": 1}], command="info", target="@u", dest=dest)
    msg = str(ei.value)
    assert "/info" in msg
    assert "--json" in msg
    for flat in ("followers", "mutuals", "hashtags"):
        assert flat in msg


def test_to_csv_lists_become_comma_joined(tmp_path: Path) -> None:
    dest = tmp_path / "hashtags.csv"
    rows = [{"tag": "art", "posts": ["a", "b", "c"]}]
    to_csv(rows, command="hashtags", target="@u", dest=dest)
    parsed = list(csv_module.DictReader(io.StringIO(dest.read_text("utf-8"))))
    assert parsed[0]["tag"] == "art"
    assert parsed[0]["posts"] == "a,b,c"


def test_to_csv_none_becomes_empty_string(tmp_path: Path) -> None:
    dest = tmp_path / "followers.csv"
    rows = [{"pk": "1", "username": None}]
    to_csv(rows, command="followers", target="@u", dest=dest)
    parsed = list(csv_module.DictReader(io.StringIO(dest.read_text("utf-8"))))
    assert parsed[0]["username"] == ""


def test_to_csv_bool_serialization(tmp_path: Path) -> None:
    dest = tmp_path / "followers.csv"
    rows = [{"pk": "1", "is_private": True, "is_verified": False}]
    to_csv(rows, command="followers", target="@u", dest=dest)
    parsed = list(csv_module.DictReader(io.StringIO(dest.read_text("utf-8"))))
    assert parsed[0]["is_private"] == "true"
    assert parsed[0]["is_verified"] == "false"


def test_to_csv_union_of_keys_across_rows(tmp_path: Path) -> None:
    dest = tmp_path / "mutuals.csv"
    rows = [
        {"pk": "1", "username": "a"},
        {"pk": "2", "username": "b", "full_name": "Bob"},
    ]
    to_csv(rows, command="mutuals", target="@u", dest=dest)
    parsed = list(csv_module.DictReader(io.StringIO(dest.read_text("utf-8"))))
    assert parsed[0]["full_name"] == ""
    assert parsed[1]["full_name"] == "Bob"


def test_to_csv_empty_rows_writes_empty_file(tmp_path: Path) -> None:
    dest = tmp_path / "followers.csv"
    to_csv([], command="followers", target="@u", dest=dest)
    assert dest.exists()
    assert dest.read_bytes() == b""


def test_to_csv_creates_parent_dirs(tmp_path: Path) -> None:
    dest = tmp_path / "deep" / "nested" / "f.csv"
    to_csv([{"pk": "1"}], command="followers", target="@u", dest=dest)
    assert dest.exists()


def test_csv_flat_commands_includes_network_and_analytics() -> None:
    network = {"followers", "followings", "mutuals", "similar"}
    analytics = {"hashtags", "mentions", "locations", "captions", "likes"}
    interactions = {"comments", "wcommented", "wtagged"}
    assert network | analytics | interactions <= CSV_FLAT_COMMANDS


def test_to_csv_returns_path_for_path_dest(tmp_path: Path) -> None:
    dest = tmp_path / "f.csv"
    result = to_csv([{"pk": "1"}], command="followers", target="@u", dest=dest)
    assert result == dest


def test_to_csv_returns_none_for_stream_dest() -> None:
    assert to_csv([{"pk": "1"}], command="followers", target="@u", dest=io.BytesIO()) is None
