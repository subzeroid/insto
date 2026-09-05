"""Static public errors; exception detail and credentials never enter DTOs."""

from __future__ import annotations

MESSAGES: dict[str, tuple[str, bool]] = {
    "invalid_params": ("Invalid operation parameters.", False),
    "invalid_token": ("The token was rejected.", False),
    "quota_exhausted": ("Provider quota is exhausted.", False),
    "rate_limited": ("Provider access is temporarily rate limited.", True),
    "network_error": ("Provider access is temporarily unavailable.", True),
    "access_unconfirmed": ("Provider access could not be confirmed.", True),
    "operation_timeout": ("The operation timed out; inspect its state before retrying.", False),
    "profile_busy": ("Another profile operation is in progress.", True),
    "profile_ownership": ("The profile cannot be managed safely.", False),
    "not_configured": ("The profile is not configured.", False),
    "already_configured": ("Use credential replacement for a configured profile.", False),
    "recovery_required": ("The profile requires recovery before another change.", False),
    "service_error": ("The background service could not reach the requested state.", False),
    "storage_error": ("Private profile storage is unavailable.", False),
    "schema_mismatch": ("The profile database schema is incompatible.", False),
    "unsupported_platform": ("Desktop service management requires macOS.", False),
}


class DesktopError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code if code in MESSAGES else "storage_error"
        super().__init__(self.code)
