"""Tests for ``insto.backends._cdn.stream_to_file``.

The streamer is exercised end-to-end against an ``httpx.MockTransport``: we
return canned responses (with headers, status codes, redirect chains, body
bytes) and assert each defense and the happy path. Where the test depends on
filesystem state we use ``tmp_path``; for disk-full and macOS xattr we patch
``shutil.disk_usage`` and skipif respectively.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from insto.backends import _cdn
from insto.exceptions import BackendError

JPEG_MAGIC = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
WEBP_MAGIC = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 8
MP4_MAGIC = b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00" + b"\x00" * 16
MOV_MAGIC = b"\x00\x00\x00\x18ftypqt  \x00\x00\x00\x00" + b"\x00" * 16
HTML_BODY = b"<!DOCTYPE html><html>not an image</html>"

CDN_HOST = "scontent-iad3-1.cdninstagram.com"
FBCDN_HOST = "instagram.fjfk1-2.fbcdn.net"


def _pad(magic: bytes, total: int = 1024) -> bytes:
    """Pad magic bytes out to ``total`` bytes so sniff buffer fills cleanly."""
    if len(magic) >= total:
        return magic
    return magic + b"\x00" * (total - len(magic))


def _client(handler: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=handler, follow_redirects=False)


def _ok(body: bytes, content_type: str) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": content_type, "content-length": str(len(body))},
        content=body,
    )


async def test_happy_path_jpeg(tmp_path: Path) -> None:
    body = _pad(JPEG_MAGIC)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == CDN_HOST
        return _ok(body, "image/jpeg")

    async with _client(httpx.MockTransport(handler)) as client:
        out = await _cdn.stream_to_file(
            f"https://{CDN_HOST}/p/abc.jpg",
            tmp_path / "12345",
            client=client,
        )

    assert out == tmp_path / "12345.jpg"
    assert out.read_bytes() == body
    assert not (tmp_path / "12345.part").exists()


async def test_happy_path_png(tmp_path: Path) -> None:
    body = _pad(PNG_MAGIC)

    def handler(request: httpx.Request) -> httpx.Response:
        return _ok(body, "image/png")

    async with _client(httpx.MockTransport(handler)) as client:
        out = await _cdn.stream_to_file(f"https://{CDN_HOST}/x", tmp_path / "id", client=client)

    assert out.suffix == ".png"
    assert out.read_bytes() == body


async def test_happy_path_webp(tmp_path: Path) -> None:
    body = _pad(WEBP_MAGIC)

    def handler(request: httpx.Request) -> httpx.Response:
        return _ok(body, "image/webp")

    async with _client(httpx.MockTransport(handler)) as client:
        out = await _cdn.stream_to_file(f"https://{CDN_HOST}/x", tmp_path / "id", client=client)

    assert out.suffix == ".webp"


async def test_happy_path_mp4(tmp_path: Path) -> None:
    body = _pad(MP4_MAGIC, total=2048)

    def handler(request: httpx.Request) -> httpx.Response:
        return _ok(body, "video/mp4")

    async with _client(httpx.MockTransport(handler)) as client:
        out = await _cdn.stream_to_file(f"https://{CDN_HOST}/x", tmp_path / "vid", client=client)

    assert out.suffix == ".mp4"


async def test_happy_path_mov(tmp_path: Path) -> None:
    body = _pad(MOV_MAGIC, total=2048)

    def handler(request: httpx.Request) -> httpx.Response:
        return _ok(body, "video/quicktime")

    async with _client(httpx.MockTransport(handler)) as client:
        out = await _cdn.stream_to_file(f"https://{CDN_HOST}/x", tmp_path / "vid", client=client)

    assert out.suffix == ".mov"


async def test_fbcdn_host_accepted(tmp_path: Path) -> None:
    body = _pad(JPEG_MAGIC)

    def handler(request: httpx.Request) -> httpx.Response:
        return _ok(body, "image/jpeg")

    async with _client(httpx.MockTransport(handler)) as client:
        out = await _cdn.stream_to_file(f"https://{FBCDN_HOST}/p", tmp_path / "x", client=client)

    assert out.read_bytes() == body


async def test_rejects_non_https(tmp_path: Path) -> None:
    async with _client(httpx.MockTransport(lambda r: _ok(b"", "image/jpeg"))) as client:
        with pytest.raises(BackendError, match="non-https"):
            await _cdn.stream_to_file(f"http://{CDN_HOST}/p", tmp_path / "x", client=client)


async def test_rejects_disallowed_host(tmp_path: Path) -> None:
    async with _client(httpx.MockTransport(lambda r: _ok(b"", "image/jpeg"))) as client:
        with pytest.raises(BackendError, match="not in allowlist"):
            await _cdn.stream_to_file("https://evil.example.com/p", tmp_path / "x", client=client)


async def test_rejects_redirect_to_http(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"location": f"http://{CDN_HOST}/elsewhere"},
        )

    async with _client(httpx.MockTransport(handler)) as client:
        with pytest.raises(BackendError, match="non-https"):
            await _cdn.stream_to_file(f"https://{CDN_HOST}/p", tmp_path / "x", client=client)


async def test_rejects_cross_host_redirect(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.example.com/q"})

    async with _client(httpx.MockTransport(handler)) as client:
        with pytest.raises(BackendError, match="cross-host redirect"):
            await _cdn.stream_to_file(f"https://{CDN_HOST}/p", tmp_path / "x", client=client)


async def test_follows_intra_allowlist_redirect(tmp_path: Path) -> None:
    body = _pad(JPEG_MAGIC)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        if "first" in url:
            return httpx.Response(302, headers={"location": f"https://{FBCDN_HOST}/second"})
        return _ok(body, "image/jpeg")

    async with _client(httpx.MockTransport(handler)) as client:
        out = await _cdn.stream_to_file(f"https://{CDN_HOST}/first", tmp_path / "x", client=client)

    assert len(calls) == 2
    assert out.read_bytes() == body


async def test_redirect_loop_aborts(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": f"https://{CDN_HOST}/loop"})

    async with _client(httpx.MockTransport(handler)) as client:
        with pytest.raises(BackendError, match="too many"):
            await _cdn.stream_to_file(f"https://{CDN_HOST}/loop", tmp_path / "x", client=client)


async def test_redirect_without_location(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302)

    async with _client(httpx.MockTransport(handler)) as client:
        with pytest.raises(BackendError, match="without Location"):
            await _cdn.stream_to_file(f"https://{CDN_HOST}/p", tmp_path / "x", client=client)


async def test_non_200_status(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    async with _client(httpx.MockTransport(handler)) as client:
        with pytest.raises(BackendError, match="HTTP 404"):
            await _cdn.stream_to_file(f"https://{CDN_HOST}/p", tmp_path / "x", client=client)


async def test_mime_mismatch_html_body_with_jpeg_header(tmp_path: Path) -> None:
    """Critical: server claims image/jpeg but body is HTML."""
    body = _pad(HTML_BODY)

    def handler(request: httpx.Request) -> httpx.Response:
        return _ok(body, "image/jpeg")

    async with _client(httpx.MockTransport(handler)) as client:
        with pytest.raises(BackendError, match=r"sniff failed|content-type mismatch"):
            await _cdn.stream_to_file(f"https://{CDN_HOST}/p", tmp_path / "x", client=client)

    assert not (tmp_path / "x.part").exists()


async def test_mime_mismatch_png_body_with_jpeg_header(tmp_path: Path) -> None:
    body = _pad(PNG_MAGIC)

    def handler(request: httpx.Request) -> httpx.Response:
        return _ok(body, "image/jpeg")

    async with _client(httpx.MockTransport(handler)) as client:
        with pytest.raises(BackendError, match="content-type mismatch"):
            await _cdn.stream_to_file(f"https://{CDN_HOST}/p", tmp_path / "x", client=client)


async def test_extension_not_in_allowlist(tmp_path: Path) -> None:
    """Server returns a content-type that maps to an unknown extension."""
    body = _pad(JPEG_MAGIC)

    def handler(request: httpx.Request) -> httpx.Response:
        return _ok(body, "application/octet-stream")

    async with _client(httpx.MockTransport(handler)) as client:
        with pytest.raises(BackendError):
            await _cdn.stream_to_file(f"https://{CDN_HOST}/p", tmp_path / "x", client=client)


async def test_byte_budget_enforced(tmp_path: Path) -> None:
    body = _pad(JPEG_MAGIC, total=200_000)

    def handler(request: httpx.Request) -> httpx.Response:
        return _ok(body, "image/jpeg")

    async with _client(httpx.MockTransport(handler)) as client:
        with pytest.raises(BackendError, match="byte budget"):
            await _cdn.stream_to_file(
                f"https://{CDN_HOST}/p",
                tmp_path / "x",
                byte_budget=100_000,
                client=client,
            )

    assert not (tmp_path / "x.part").exists()


async def test_disk_guard_aborts_when_low(tmp_path: Path, monkeypatch) -> None:
    fake_usage = type("U", (), {"total": 10**12, "used": 10**12, "free": 100})()
    monkeypatch.setattr(_cdn.shutil, "disk_usage", lambda _p: fake_usage)

    async with _client(
        httpx.MockTransport(lambda r: _ok(_pad(JPEG_MAGIC), "image/jpeg"))
    ) as client:
        with pytest.raises(BackendError, match="insufficient disk"):
            await _cdn.stream_to_file(f"https://{CDN_HOST}/p", tmp_path / "x", client=client)


async def test_atomic_write_uses_part_then_rename(tmp_path: Path) -> None:
    body = _pad(JPEG_MAGIC)

    def handler(request: httpx.Request) -> httpx.Response:
        return _ok(body, "image/jpeg")

    async with _client(httpx.MockTransport(handler)) as client:
        out = await _cdn.stream_to_file(f"https://{CDN_HOST}/p", tmp_path / "media", client=client)

    assert out.exists()
    assert not out.with_name("media.part").exists()


async def test_collision_appends_suffix(tmp_path: Path) -> None:
    body = _pad(JPEG_MAGIC)

    def handler(request: httpx.Request) -> httpx.Response:
        return _ok(body, "image/jpeg")

    (tmp_path / "media.jpg").write_bytes(b"existing")

    async with _client(httpx.MockTransport(handler)) as client:
        out1 = await _cdn.stream_to_file(f"https://{CDN_HOST}/p", tmp_path / "media", client=client)
        out2 = await _cdn.stream_to_file(f"https://{CDN_HOST}/p", tmp_path / "media", client=client)

    assert out1.name == "media_1.jpg"
    assert out2.name == "media_2.jpg"
    assert (tmp_path / "media.jpg").read_bytes() == b"existing"


async def test_mtime_set_from_taken_at(tmp_path: Path) -> None:
    body = _pad(JPEG_MAGIC)
    when = datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC)

    def handler(request: httpx.Request) -> httpx.Response:
        return _ok(body, "image/jpeg")

    async with _client(httpx.MockTransport(handler)) as client:
        out = await _cdn.stream_to_file(
            f"https://{CDN_HOST}/p",
            tmp_path / "x",
            taken_at=when,
            client=client,
        )

    assert os.path.getmtime(out) == pytest.approx(when.timestamp(), abs=1.0)


async def test_mtime_set_from_taken_at_float(tmp_path: Path) -> None:
    body = _pad(JPEG_MAGIC)
    ts = 1_700_000_000.0

    def handler(request: httpx.Request) -> httpx.Response:
        return _ok(body, "image/jpeg")

    async with _client(httpx.MockTransport(handler)) as client:
        out = await _cdn.stream_to_file(
            f"https://{CDN_HOST}/p",
            tmp_path / "x",
            taken_at=ts,
            client=client,
        )

    assert os.path.getmtime(out) == pytest.approx(ts, abs=1.0)


async def test_creates_parent_directories(tmp_path: Path) -> None:
    body = _pad(JPEG_MAGIC)

    def handler(request: httpx.Request) -> httpx.Response:
        return _ok(body, "image/jpeg")

    nested = tmp_path / "deep" / "nested" / "dir"

    async with _client(httpx.MockTransport(handler)) as client:
        out = await _cdn.stream_to_file(f"https://{CDN_HOST}/p", nested / "x", client=client)

    assert out.parent == nested


async def test_content_type_hint_used_when_header_absent(tmp_path: Path) -> None:
    body = _pad(JPEG_MAGIC)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    async with _client(httpx.MockTransport(handler)) as client:
        out = await _cdn.stream_to_file(
            f"https://{CDN_HOST}/p",
            tmp_path / "x",
            content_type_hint="image/jpeg",
            client=client,
        )

    assert out.suffix == ".jpg"


async def test_content_type_with_charset_param(tmp_path: Path) -> None:
    body = _pad(JPEG_MAGIC)

    def handler(request: httpx.Request) -> httpx.Response:
        return _ok(body, "image/jpeg; charset=binary")

    async with _client(httpx.MockTransport(handler)) as client:
        out = await _cdn.stream_to_file(f"https://{CDN_HOST}/p", tmp_path / "x", client=client)

    assert out.suffix == ".jpg"


async def test_no_header_no_hint_uses_sniff(tmp_path: Path) -> None:
    body = _pad(JPEG_MAGIC)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    async with _client(httpx.MockTransport(handler)) as client:
        out = await _cdn.stream_to_file(f"https://{CDN_HOST}/p", tmp_path / "x", client=client)

    assert out.suffix == ".jpg"


async def test_unknown_magic_bytes_aborts(tmp_path: Path) -> None:
    body = b"absolutely not a known media file" * 64

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    async with _client(httpx.MockTransport(handler)) as client:
        with pytest.raises(BackendError, match="sniff failed"):
            await _cdn.stream_to_file(f"https://{CDN_HOST}/p", tmp_path / "x", client=client)


async def test_empty_body_aborts(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"", headers={"content-type": "image/jpeg"})

    async with _client(httpx.MockTransport(handler)) as client:
        with pytest.raises(BackendError, match="empty"):
            await _cdn.stream_to_file(f"https://{CDN_HOST}/p", tmp_path / "x", client=client)


async def test_subdomain_host_allowlisting() -> None:
    assert _cdn._is_host_allowed("scontent.cdninstagram.com")
    assert _cdn._is_host_allowed("instagram.fxxx-1.fbcdn.net")
    assert _cdn._is_host_allowed("CDNINSTAGRAM.COM")
    assert not _cdn._is_host_allowed("evil-cdninstagram.com.attacker.test")
    assert not _cdn._is_host_allowed("cdninstagram.com.fake")
    assert not _cdn._is_host_allowed("example.com")


@pytest.mark.skipif(sys.platform != "darwin", reason="darwin-only xattr")
async def test_macos_xattr_tag_value(tmp_path: Path) -> None:
    """On darwin the file ends up tagged with the literal bytes ``insto``."""
    import ctypes
    import ctypes.util

    body = _pad(JPEG_MAGIC)

    def handler(request: httpx.Request) -> httpx.Response:
        return _ok(body, "image/jpeg")

    async with _client(httpx.MockTransport(handler)) as client:
        out = await _cdn.stream_to_file(f"https://{CDN_HOST}/p", tmp_path / "x", client=client)

    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    libc.getxattr.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_uint32,
        ctypes.c_int,
    ]
    libc.getxattr.restype = ctypes.c_ssize_t

    buf = ctypes.create_string_buffer(64)
    n = libc.getxattr(
        str(out).encode("utf-8"),
        b"com.apple.metadata:kMDItemUserTags",
        buf,
        64,
        0,
        0,
    )
    assert n > 0, f"xattr not set (errno={ctypes.get_errno()})"
    assert bytes(buf.raw[:n]) == b"insto"


async def test_xattr_skipped_on_non_darwin(tmp_path: Path, monkeypatch) -> None:
    """On non-darwin platforms ``_set_macos_tag`` is a no-op."""
    monkeypatch.setattr(_cdn.sys, "platform", "linux")
    monkeypatch.setattr(_cdn, "_xattr_warned", False)
    target = tmp_path / "x.jpg"
    target.write_bytes(b"x")
    _cdn._set_macos_tag(target)
