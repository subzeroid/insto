from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from insto._redact import clear_registered_secrets, register_secret

MODULE_PATH = Path(__file__).parent / "live" / "aiograpi_private_graphql_audit.py"
SPEC = importlib.util.spec_from_file_location("aiograpi_private_graphql_audit", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
audit = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = audit
SPEC.loader.exec_module(audit)


@pytest.fixture(autouse=True)
def _clean_redaction_registry() -> None:
    clear_registered_secrets()
    yield
    clear_registered_secrets()


def test_private_graphql_user_parser_accepts_users_shape() -> None:
    payload = {
        "data": {
            "xdt_api__v1__friendships__followers": {
                "users": [
                    {"pk": "1", "username": "private-user"},
                    {"id": "2", "username": "another-private-user"},
                ],
                "next_max_id": "secret-cursor",
            }
        }
    }

    ids, cursor_present = audit._private_graphql_user_ids(
        payload,
        root_field="xdt_api__v1__friendships__followers",
    )

    assert ids == ["1", "2"]
    assert cursor_present is True


def test_private_graphql_user_parser_accepts_edge_shape() -> None:
    payload = {
        "xdt_api__v1__friendships__following": {
            "edges": [
                {"node": {"pk_id": "7", "username": "hidden"}},
                {"node": {"id": "8", "username": "hidden2"}},
            ],
            "page_info": {"has_next_page": True, "end_cursor": "cursor-secret"},
        }
    }

    ids, cursor_present = audit._private_graphql_user_ids(
        payload,
        root_field="xdt_api__v1__friendships__following",
    )

    assert ids == ["7", "8"]
    assert cursor_present is True


def test_private_graphql_user_parser_accepts_stream_rows_shape() -> None:
    payload = {
        "stream_rows": [
            {
                "data": {
                    "xdt_api__v1__friendships__followers": {
                        "users": [{"pk": "9"}],
                    }
                }
            }
        ]
    }

    ids, cursor_present = audit._private_graphql_user_ids(
        payload,
        root_field="xdt_api__v1__friendships__followers",
    )

    assert ids == ["9"]
    assert cursor_present is False


def test_comparison_summary_never_prints_ids_or_cursors() -> None:
    out = audit._comparison_summary(
        current_ids=["111", "222", "333"],
        candidate_ids=["222", "333", "444"],
        candidate_cursor_present=True,
    )

    assert out == "current=3 candidate=3 overlap=2 cursor=yes verdict=partial"
    assert "111" not in out
    assert "444" not in out


def test_comparison_summary_marks_matching_prefix_as_ok() -> None:
    out = audit._comparison_summary(
        current_ids=["111", "222"],
        candidate_ids=["111", "222"],
        candidate_cursor_present=False,
    )

    assert out == "current=2 candidate=2 overlap=2 cursor=no verdict=ok"


def test_object_ids_reads_common_aiograpi_attrs_without_usernames() -> None:
    class User:
        pk = 123
        username = "private-user"

    class Media:
        id = "456"
        code = "SECRETCODE"

    assert audit._object_ids([User(), Media(), {"pk_id": "789", "username": "hidden"}]) == [
        "123",
        "456",
        "789",
    ]


def test_safe_error_redacts_registered_values_and_query_tokens() -> None:
    register_secret("secret-account-provider")
    exc = RuntimeError(
        "failed secret-account-provider "
        "https://api.example.test/path?token=abc123 "
        "socks5://user:pass@127.0.0.1:9050"
    )

    out = audit._safe_error(exc)

    assert "secret-account-provider" not in out
    assert "abc123" not in out
    assert "user:pass" not in out
    assert "***" in out


async def test_run_check_suppresses_sdk_stdout_and_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def noisy_sdk_call() -> str:
        print("raw html fallback that should not leak")
        print("raw stderr fallback that should not leak", file=sys.stderr)
        return "count=1"

    result = await audit._run_check("surface", noisy_sdk_call)

    captured = capsys.readouterr()
    assert result == audit.CheckResult("surface", "ok", "count=1")
    assert captured.out == ""
    assert captured.err == ""
