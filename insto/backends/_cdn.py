"""CDN streamer with defense in depth.

Single helper used by the service facade to download CDN-hosted media
(profile pictures, posts, stories, highlights) into a target directory.
All defenses live here so that command and service code never touch raw
HTTP for media.

Defenses (each one is exercised by tests/test_cdn.py):

- HTTPS-only (http:// rejected, http:// redirects rejected)
- Host allowlist (only `*.cdninstagram.com` and `*.fbcdn.net` accepted, both
  for the initial URL and any redirect target)
- Filename built from the caller-supplied ``dest`` (intended to be the post
  pk or similar stable id) plus an extension chosen from response
  Content-Type cross-checked against magic-byte sniffing — the CDN-supplied
  filename in the URL is ignored entirely
- Whitelist of extensions (.jpg/.jpeg/.png/.webp/.mp4/.mov)
- Per-resource byte budget (default 500 MB)
- Pre-flight free-disk check (default 1 GB minimum)
- Atomic write: stream to ``<final>.part``, fsync, rename → final
- Collision suffix: never overwrites an existing file; appends ``_<n>``
- ``mtime`` is set from ``taken_at`` if supplied (so file dates match post
  capture time, not download time)
- macOS xattr tagging via ctypes ``setxattr(2)`` — adds
  ``com.apple.metadata:kMDItemUserTags = insto`` so downloaded media is
  visible in Finder Smart Folders. No-op on non-darwin. On darwin, an
  ``OSError`` (e.g. NFS, exFAT) is reported once-per-process to stderr
  and otherwise silently swallowed.
"""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from insto.exceptions import BackendError

ALLOWED_HOST_SUFFIXES: tuple[str, ...] = ("cdninstagram.com", "fbcdn.net")
ALLOWED_EXTENSIONS: frozenset[str] = frozenset({".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov"})

CT_TO_EXT: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
}

DEFAULT_BYTE_BUDGET: int = 500 * 1024 * 1024
DEFAULT_MIN_FREE_DISK: int = 1024 * 1024 * 1024
SNIFF_SIZE: int = 512
CHUNK_SIZE: int = 64 * 1024
MAX_REDIRECTS: int = 5
DEFAULT_TIMEOUT: float = 30.0

_XATTR_NAME: bytes = b"com.apple.metadata:kMDItemUserTags"
_XATTR_VALUE: bytes = b"insto"
_XATTR_WARN_LINE: str = "note: filesystem does not support xattr; tagging skipped"

_xattr_warned: bool = False


def _is_host_allowed(host: str) -> bool:
    host = host.lower()
    return any(host == s or host.endswith("." + s) for s in ALLOWED_HOST_SUFFIXES)


def _normalize_ct(ct: str | None) -> str | None:
    if not ct:
        return None
    return ct.split(";", 1)[0].strip().lower() or None


def _sniff(prefix: bytes) -> tuple[str, str] | None:
    """Sniff magic bytes; return (extension, mime) or None."""
    if prefix.startswith(b"\xff\xd8\xff"):
        return ".jpg", "image/jpeg"
    if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png", "image/png"
    if prefix.startswith(b"RIFF") and len(prefix) >= 12 and prefix[8:12] == b"WEBP":
        return ".webp", "image/webp"
    if len(prefix) >= 12 and prefix[4:8] == b"ftyp":
        brand = prefix[8:12]
        if brand == b"qt  ":
            return ".mov", "video/quicktime"
        return ".mp4", "video/mp4"
    return None


def _ct_compatible(declared: str, sniffed: str) -> bool:
    if declared == sniffed:
        return True
    return {declared, sniffed} <= {"image/jpeg", "image/jpg"}


def _resolve_collision(base: Path) -> Path:
    """Return a non-existing path; if ``base`` exists, append ``_1``, ``_2`` …"""
    if not base.exists():
        return base
    stem, suffix, parent = base.stem, base.suffix, base.parent
    n = 1
    while True:
        candidate = parent / f"{stem}_{n}{suffix}"
        if not candidate.exists():
            return candidate
        n += 1


def _set_macos_tag(path: Path) -> None:
    """Tag ``path`` with ``com.apple.metadata:kMDItemUserTags=insto`` on darwin.

    No-op on other platforms. On darwin, errors from the underlying
    ``setxattr(2)`` (e.g. filesystem without xattr support such as NFS or
    exFAT) are swallowed and reported once per process to stderr.
    """
    global _xattr_warned
    if sys.platform != "darwin":
        return
    libc_path = ctypes.util.find_library("c")
    if libc_path is None:  # pragma: no cover - libc is always present on darwin
        return
    libc = ctypes.CDLL(libc_path, use_errno=True)
    libc.setxattr.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_uint32,
        ctypes.c_int,
    ]
    libc.setxattr.restype = ctypes.c_int
    rc = libc.setxattr(
        str(path).encode("utf-8"),
        _XATTR_NAME,
        _XATTR_VALUE,
        len(_XATTR_VALUE),
        0,
        0,
    )
    if rc != 0 and not _xattr_warned:
        _xattr_warned = True
        print(_XATTR_WARN_LINE, file=sys.stderr)


def _validate_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise BackendError(f"non-https CDN url rejected: {url}")
    if not parsed.hostname or not _is_host_allowed(parsed.hostname):
        raise BackendError(f"CDN host not in allowlist: {parsed.hostname}")


def _validate_redirect(current: str, location: str) -> str:
    new_url = urljoin(current, location)
    parsed = urlparse(new_url)
    if parsed.scheme != "https":
        raise BackendError(f"CDN redirect to non-https rejected: {new_url}")
    if not parsed.hostname or not _is_host_allowed(parsed.hostname):
        raise BackendError(f"CDN cross-host redirect rejected: {parsed.hostname}")
    return new_url


def _decide_extension(declared_ct: str | None, sniffed: tuple[str, str]) -> str:
    sniff_ext, sniff_mime = sniffed
    if declared_ct is not None:
        if not _ct_compatible(declared_ct, sniff_mime):
            raise BackendError(
                f"CDN content-type mismatch: header={declared_ct} sniff={sniff_mime}"
            )
        ext = CT_TO_EXT.get(declared_ct, sniff_ext)
    else:
        ext = sniff_ext
    if ext not in ALLOWED_EXTENSIONS:
        raise BackendError(f"CDN extension not in allowlist: {ext}")
    return ext


def _coerce_taken_at(taken_at: datetime | float | int | None) -> float | None:
    if taken_at is None:
        return None
    if isinstance(taken_at, datetime):
        return taken_at.timestamp()
    return float(taken_at)


async def stream_to_file(
    url: str,
    dest: Path,
    *,
    content_type_hint: str | None = None,
    byte_budget: int = DEFAULT_BYTE_BUDGET,
    taken_at: datetime | float | int | None = None,
    client: httpx.AsyncClient | None = None,
    min_free_disk: int = DEFAULT_MIN_FREE_DISK,
    timeout: float = DEFAULT_TIMEOUT,
) -> Path:
    """Stream a CDN URL to ``dest`` (path *without* extension).

    The chosen extension comes from the response Content-Type, cross-checked
    against magic-byte sniffing of the leading bytes; if both the header and
    sniff agree, that extension is appended to ``dest``. If the resulting
    path already exists, ``_1`` / ``_2`` … is appended before the extension.

    Returns the path actually written.
    """

    _validate_url(url)

    parent = dest.parent
    parent.mkdir(parents=True, exist_ok=True)

    free = shutil.disk_usage(parent).free
    if free < min_free_disk:
        raise BackendError(f"insufficient disk space: {free} bytes free < {min_free_disk}")

    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(follow_redirects=False, timeout=timeout)

    try:
        return await _download_with_redirects(
            url=url,
            dest=dest,
            content_type_hint=content_type_hint,
            byte_budget=byte_budget,
            taken_at=_coerce_taken_at(taken_at),
            client=client,
        )
    finally:
        if owns_client:
            await client.aclose()


async def _download_with_redirects(
    *,
    url: str,
    dest: Path,
    content_type_hint: str | None,
    byte_budget: int,
    taken_at: float | None,
    client: httpx.AsyncClient,
) -> Path:
    current_url = url
    redirects = 0
    while True:
        async with client.stream("GET", current_url) as resp:
            if 300 <= resp.status_code < 400:
                if redirects >= MAX_REDIRECTS:
                    raise BackendError("too many CDN redirects")
                redirects += 1
                location = resp.headers.get("location")
                if not location:
                    raise BackendError("CDN redirect without Location header")
                current_url = _validate_redirect(current_url, location)
                continue

            if resp.status_code != 200:
                raise BackendError(f"CDN GET failed: HTTP {resp.status_code}")

            declared_ct = _normalize_ct(resp.headers.get("content-type")) or _normalize_ct(
                content_type_hint
            )
            return await _stream_response(
                resp=resp,
                dest=dest,
                declared_ct=declared_ct,
                byte_budget=byte_budget,
                taken_at=taken_at,
            )


async def _stream_response(
    *,
    resp: httpx.Response,
    dest: Path,
    declared_ct: str | None,
    byte_budget: int,
    taken_at: float | None,
) -> Path:
    parent = dest.parent
    tmp_path = parent / (dest.name + ".part")
    if tmp_path.exists():
        tmp_path.unlink()

    sniff_buf = bytearray()
    sniffed: tuple[str, str] | None = None
    total = 0

    try:
        with open(tmp_path, "wb") as fh:
            async for chunk in resp.aiter_bytes(CHUNK_SIZE):
                if not chunk:
                    continue
                if sniffed is None and len(sniff_buf) < SNIFF_SIZE:
                    sniff_buf.extend(chunk[: SNIFF_SIZE - len(sniff_buf)])
                    if len(sniff_buf) >= SNIFF_SIZE:
                        sniffed = _sniff(bytes(sniff_buf))
                        if sniffed is None:
                            raise BackendError("CDN content-type sniff failed: unknown magic bytes")
                        if declared_ct is not None and not _ct_compatible(declared_ct, sniffed[1]):
                            raise BackendError(
                                "CDN content-type mismatch: "
                                f"header={declared_ct} sniff={sniffed[1]}"
                            )
                fh.write(chunk)
                total += len(chunk)
                if total > byte_budget:
                    raise BackendError(f"CDN response exceeded byte budget {byte_budget}")

            if sniffed is None:
                if not sniff_buf:
                    raise BackendError("CDN response was empty")
                sniffed = _sniff(bytes(sniff_buf))
                if sniffed is None:
                    raise BackendError("CDN content-type sniff failed: unknown magic bytes")
            fh.flush()
            os.fsync(fh.fileno())

        ext = _decide_extension(declared_ct, sniffed)
        final_base = parent / (dest.name + ext)
        final = _resolve_collision(final_base)
        os.rename(tmp_path, final)

        if taken_at is not None:
            os.utime(final, (taken_at, taken_at))

        _set_macos_tag(final)
        return final
    except BaseException:
        if tmp_path.exists():
            with contextlib.suppress(OSError):
                tmp_path.unlink()
        raise
