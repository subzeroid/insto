"""Single source of truth for secret redaction.

Used by `insto.cli._format_error` (user-facing error strings) and by the
logging formatter (file-side log output). Anything that may end up in
front of human eyes — stderr, log files, copy-pasted bug reports — passes
through `redact_secrets()` first.

Patterns covered:

- the literal value of `$HIKERAPI_TOKEN` if it is set in the environment;
- the literal values of any tokens / proxy credentials registered at
  runtime via `register_secret()` (used by the config loader so a token
  loaded from `~/.insto/config.toml` or supplied by `--proxy user:pass@`
  is redacted the same way `$HIKERAPI_TOKEN` is);
- query-string `signature=` and `token=` parameters in URLs (HikerAPI
  signs every CDN URL — those signatures are short-lived but still
  sensitive);
- `Authorization: Bearer <token>` style headers if they ever surface
  in an exception message;
- `proxy://user:pass@host` userinfo segments.
"""

from __future__ import annotations

import os
import re
import threading

_QS_SECRET_RE = re.compile(
    r"((?:^|[?&])(?:signature|token)=)[^&\s'\"]+",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(
    r"(Bearer\s+)[A-Za-z0-9._~+/=-]+",
    re.IGNORECASE,
)
_PROXY_USERINFO_RE = re.compile(
    r"(\b[a-zA-Z][a-zA-Z0-9+.-]*://)([^:/@\s]+):([^@/\s]+)@",
)

_secrets_lock = threading.Lock()
_registered_secrets: set[str] = set()


def register_secret(value: str | None) -> None:
    """Add `value` to the runtime redaction set.

    Safe to call multiple times with the same value. Values shorter than
    4 characters are ignored to avoid pathological matches against common
    substrings. Threadsafe under a small mutex so logging handlers that
    call `redact_secrets` concurrently never see a torn set.
    """
    if not value or len(value) < 4:
        return
    with _secrets_lock:
        _registered_secrets.add(value)


def clear_registered_secrets() -> None:
    """Drop every value registered via `register_secret`. Useful in tests."""
    with _secrets_lock:
        _registered_secrets.clear()


def redact_secrets(text: str) -> str:
    """Return `text` with known secret-shaped substrings replaced with `***`.

    Stable: never raises. Threadsafe; the registered-secrets set is read
    under a short mutex.
    """
    if not text:
        return text
    redacted = text
    env_token = os.environ.get("HIKERAPI_TOKEN")
    if env_token and len(env_token) >= 4:
        redacted = redacted.replace(env_token, "***")
    with _secrets_lock:
        registered = tuple(_registered_secrets)
    for secret in registered:
        if secret and secret in redacted:
            redacted = redacted.replace(secret, "***")
    redacted = _PROXY_USERINFO_RE.sub(r"\1***:***@", redacted)
    redacted = _QS_SECRET_RE.sub(r"\1***", redacted)
    redacted = _BEARER_RE.sub(r"\1***", redacted)
    return redacted


__all__ = ["clear_registered_secrets", "redact_secrets", "register_secret"]
