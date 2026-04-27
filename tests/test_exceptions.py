"""Tests for the backend exception taxonomy."""

from __future__ import annotations

import pytest

from insto.exceptions import (
    AuthInvalid,
    BackendError,
    Banned,
    PostNotFound,
    PostPrivate,
    ProfileBlocked,
    ProfileDeleted,
    ProfileNotFound,
    ProfilePrivate,
    QuotaExhausted,
    RateLimited,
    SchemaDrift,
    Transient,
)


def test_all_inherit_backend_error() -> None:
    for cls in (
        ProfileNotFound,
        ProfilePrivate,
        ProfileBlocked,
        ProfileDeleted,
        PostNotFound,
        PostPrivate,
        AuthInvalid,
        QuotaExhausted,
        RateLimited,
        SchemaDrift,
        Transient,
        Banned,
    ):
        assert issubclass(cls, BackendError)


def test_profile_not_found_fields_and_str() -> None:
    err = ProfileNotFound("alice")
    assert err.username == "alice"
    assert "alice" in str(err)
    assert "not found" in str(err)


def test_profile_private_fields_and_str() -> None:
    err = ProfilePrivate("bob")
    assert err.username == "bob"
    assert "private" in str(err)
    assert "@bob" in str(err)


def test_profile_blocked_fields_and_str() -> None:
    err = ProfileBlocked("carol")
    assert err.username == "carol"
    assert "blocked" in str(err)


def test_profile_deleted_fields_and_str() -> None:
    err = ProfileDeleted("dave")
    assert err.username == "dave"
    assert "deleted" in str(err)


def test_post_not_found_fields_and_str() -> None:
    err = PostNotFound("ABC123")
    assert err.ref == "ABC123"
    assert "ABC123" in str(err)
    assert "not found" in str(err)


def test_post_private_fields_and_str() -> None:
    err = PostPrivate("ABC123")
    assert err.ref == "ABC123"
    assert "private" in str(err)


def test_auth_invalid_default_and_custom() -> None:
    default = AuthInvalid()
    assert default.detail == "auth invalid"
    assert "auth invalid" in str(default)
    custom = AuthInvalid("token rejected by hiker")
    assert custom.detail == "token rejected by hiker"
    assert "rejected" in str(custom)


def test_quota_exhausted_default_and_custom() -> None:
    err = QuotaExhausted()
    assert "quota" in str(err)
    custom = QuotaExhausted("daily quota exhausted")
    assert custom.detail == "daily quota exhausted"
    assert "daily" in str(custom)


def test_rate_limited_carries_retry_after() -> None:
    err = RateLimited(retry_after=12.5)
    assert err.retry_after == pytest.approx(12.5)
    assert "12.5" in str(err)
    assert "retry" in str(err)


def test_rate_limited_custom_detail() -> None:
    err = RateLimited(retry_after=1.0, detail="slow down")
    assert err.retry_after == 1.0
    assert str(err) == "slow down"


def test_schema_drift_carries_endpoint_and_field() -> None:
    err = SchemaDrift(endpoint="user/by/username", missing_field="pk")
    assert err.endpoint == "user/by/username"
    assert err.missing_field == "pk"
    assert "user/by/username" in str(err)
    assert "pk" in str(err)


def test_transient_default_and_custom() -> None:
    err = Transient()
    assert "transient" in str(err)
    custom = Transient("network blip")
    assert custom.detail == "network blip"
    assert "blip" in str(custom)


def test_banned_default_and_custom() -> None:
    err = Banned()
    assert "banned" in str(err)
    custom = Banned("suspended")
    assert custom.detail == "suspended"
    assert "suspended" in str(custom)


def test_backend_error_str_with_no_args() -> None:
    err = BackendError()
    assert str(err) == "BackendError"


def test_can_be_raised_and_caught_as_backend_error() -> None:
    with pytest.raises(BackendError):
        raise RateLimited(retry_after=2.0)
    with pytest.raises(BackendError):
        raise SchemaDrift("ep", "f")
