"""Backend exception taxonomy for insto.

All backend implementations raise these exceptions; never raw HTTP / SDK errors
leak above the backend layer. Command and service layers catch `BackendError`
and let the cli/repl format it for the user.
"""

from __future__ import annotations


class BackendError(Exception):
    """Base class for all backend-level errors raised by insto."""

    def __str__(self) -> str:
        return self.args[0] if self.args else self.__class__.__name__


class DailyBudgetReached(BackendError):
    """The local UTC-day request-attempt ceiling has been reached."""

    def __init__(self) -> None:
        super().__init__("Daily request-attempt limit reached; resets at midnight UTC.")


class UsageStorageError(BackendError):
    """Attempt accounting cannot be read or committed; no request may proceed."""

    def __init__(self) -> None:
        super().__init__("Local request-attempt accounting is unavailable.")


class ProfileNotFound(BackendError):
    """The username does not resolve to any Instagram profile."""

    def __init__(self, username: str) -> None:
        super().__init__(f"profile not found: @{username}")
        self.username = username


class ProfilePrivate(BackendError):
    """Profile exists but is private and the caller is not following."""

    def __init__(self, username: str) -> None:
        super().__init__(f"profile is private: @{username}")
        self.username = username


class ProfileBlocked(BackendError):
    """Profile has blocked the backend account; cannot fetch data."""

    def __init__(self, username: str) -> None:
        super().__init__(f"profile has blocked us: @{username}")
        self.username = username


class ProfileDeleted(BackendError):
    """Profile has been deleted or deactivated."""

    def __init__(self, username: str) -> None:
        super().__init__(f"profile deleted: @{username}")
        self.username = username


class PostNotFound(BackendError):
    """The post (media pk or shortcode) does not resolve."""

    def __init__(self, ref: str) -> None:
        super().__init__(f"post not found: {ref}")
        self.ref = ref


class PostPrivate(BackendError):
    """Post belongs to a private account we cannot read."""

    def __init__(self, ref: str) -> None:
        super().__init__(f"post is private: {ref}")
        self.ref = ref


class AuthInvalid(BackendError):
    """Backend credentials (token / session) are missing or rejected."""

    def __init__(self, detail: str = "auth invalid") -> None:
        super().__init__(detail)
        self.detail = detail


class QuotaExhausted(BackendError):
    """Backend monthly / daily quota has been exhausted."""

    def __init__(self, detail: str = "quota exhausted") -> None:
        super().__init__(detail)
        self.detail = detail


class RateLimited(BackendError):
    """Backend is rate-limiting; retry after `retry_after` seconds."""

    def __init__(self, retry_after: float, detail: str | None = None) -> None:
        msg = detail or f"rate limited; retry after {retry_after:.1f}s"
        super().__init__(msg)
        self.retry_after = retry_after


class SchemaDrift(BackendError):
    """Backend response is missing a documented field — schema has drifted."""

    def __init__(self, endpoint: str, missing_field: str) -> None:
        super().__init__(f"schema drift in {endpoint}: missing field {missing_field!r}")
        self.endpoint = endpoint
        self.missing_field = missing_field


class Transient(BackendError):
    """Transient backend failure (network blip, 5xx). Safe to retry."""

    def __init__(self, detail: str = "transient backend error") -> None:
        super().__init__(detail)
        self.detail = detail


class Banned(BackendError):
    """Backend account is banned / suspended; not retriable."""

    def __init__(self, detail: str = "backend account banned") -> None:
        super().__init__(detail)
        self.detail = detail


class PageBudgetExceeded(BackendError):
    """A cursor kept offering pages after the caller's page budget was spent.

    Its message is the one `iter_*` has always raised for a non-terminating
    cursor, so nothing a user reads changes; the type lets a caller that chose
    a deliberately small budget (the desktop lookups) tell its own ceiling from
    a backend failure and keep what it already paid for.
    """

    def __init__(self, endpoint: str, pages: int) -> None:
        super().__init__(f"{endpoint}: cursor did not terminate after {pages} pages")
        self.endpoint = endpoint
        self.pages = pages
