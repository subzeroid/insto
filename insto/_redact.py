"""Single source of truth for secret redaction.

Used by `insto.cli._format_error` (user-facing error strings) and by the
logging formatter (file-side log output). Anything that may end up in
front of human eyes — stderr, log files, copy-pasted bug reports — passes
through `redact_secrets()` first.

Patterns covered:

- the literal value of `$HIKERAPI_TOKEN` if it is set in the environment
  (so even error messages like `connection failed for token=abc...` lose
  the leaked secret);
- query-string `signature=` and `token=` parameters in URLs (HikerAPI
  signs every CDN URL — those signatures are short-lived but still
  sensitive);
- `Authorization: Bearer <token>` style headers if they ever surface
  in an exception message.
"""

from __future__ import annotations

import os
import re

_QS_SECRET_RE = re.compile(
    r"((?:^|[?&])(?:signature|token)=)[^&\s'\"]+",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(
    r"(Bearer\s+)[A-Za-z0-9._~+/=-]+",
    re.IGNORECASE,
)


def redact_secrets(text: str) -> str:
    """Return `text` with known secret-shaped substrings replaced with `***`.

    Stable: never raises, never depends on anything outside `os` and `re`.
    Same input → same output (only depends on the live process env), so
    safe to call from logging handlers without locking.
    """
    if not text:
        return text
    redacted = text
    env_token = os.environ.get("HIKERAPI_TOKEN")
    if env_token and len(env_token) >= 4:
        redacted = redacted.replace(env_token, "***")
    redacted = _QS_SECRET_RE.sub(r"\1***", redacted)
    redacted = _BEARER_RE.sub(r"\1***", redacted)
    return redacted


__all__ = ["redact_secrets"]
