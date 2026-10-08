from __future__ import annotations

import codecs
import re
import time
from typing import Any

import httpx

MAX_HTML_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_TORRENT_RESPONSE_BYTES = 16 * 1024 * 1024
# Per-read timeouts do not bound a mirror that trickles bytes; this bounds the whole body.
MAX_RESPONSE_SECONDS = 120.0


class ResponseTooLargeError(RuntimeError):
    """Raised before an untrusted HTTP response can consume unbounded memory."""


class ResponseTooSlowError(RuntimeError):
    """Raised when a response body does not arrive within MAX_RESPONSE_SECONDS."""


UA_DEFAULT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


_HEADER_CHARSET = re.compile(r"charset\s*=\s*[\"']?([\w.:-]+)", re.IGNORECASE)
_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?([\w.:-]+)""", re.IGNORECASE)


# A charset a server names by default rather than for the page (its own setting, not the site's).
_WEAK_CHARSETS = frozenset({"iso-8859-1", "iso8859-1", "latin1", "latin-1", "l1", "us-ascii", "ascii", "windows-1252"})
_BOMS = ((b"\xef\xbb\xbf", "utf-8-sig"), (b"\xff\xfe", "utf-16"), (b"\xfe\xff", "utf-16"))
# Codecs that turn bytes into text but are no page encoding: the escapes and UTF-7 give any
# code point (lone surrogates too, which no store can save), the name codecs, the
# never-defined one and charmap (latin-1 by another name). Bytes-to-bytes and text-to-text
# codecs (base64, hex, rot_13...) are refused by their own mark.
_NOT_PAGE_CODECS = frozenset(
    {"unicode-escape", "raw-unicode-escape", "utf-7", "idna", "punycode", "undefined", "charmap"}
)
_SURROGATE = re.compile("[\ud800-\udfff]")


def _page_codec(name: str) -> str | None:
    """The codec a page may be decoded with, or None for a name that is no text encoding."""
    try:
        info = codecs.lookup(name)
    except LookupError:
        return None
    if not getattr(info, "_is_text_encoding", True) or info.name in _NOT_PAGE_CODECS:
        return None
    return info.name


def html_text(response: Any) -> str:
    """Decode a tracker page the way a browser would - and where Russian forums need it,
    better than one.

    Order: a byte-order mark; the <meta charset> when the header names none or only a
    server's latin-1 default; a body that is valid UTF-8 (a windows-1251 page almost never
    is); the header's charset (not a latin-1 default without a meta), the meta's;
    windows-1251.
    """
    content = getattr(response, "content", None)
    if not isinstance(content, (bytes, bytearray)):
        return str(getattr(response, "text", "") or "")
    body = bytes(content)
    for bom, encoding in _BOMS:
        if body.startswith(bom):
            return body.decode(encoding, errors="replace")
    found = _HEADER_CHARSET.search(str((getattr(response, "headers", None) or {}).get("content-type") or ""))
    header = found.group(1).casefold() if found else ""
    found_meta = _META_CHARSET.search(body[:8192])
    meta = found_meta.group(1).decode("ascii", "ignore") if found_meta else ""
    first = [meta] if meta and (not header or header in _WEAK_CHARSETS) else []
    if header in _WEAK_CHARSETS and not meta:
        # A server's default alone does not name the page, and latin-1 never fails to decode.
        header = ""
    for name in [*first, "utf-8", header, meta]:
        codec = _page_codec(name) if name else None
        if codec is None:
            continue
        try:
            text = body.decode(codec)
        except UnicodeError:  # "undefined" and its kind raise a plain UnicodeError
            continue
        if not _SURROGATE.search(text):
            return text
    return body.decode("cp1251", errors="replace")


# The titles of Cloudflare's own check pages.
_CLOUDFLARE_TITLES = frozenset({"just a moment...", "just a moment…", "attention required! | cloudflare"})
_TITLE = re.compile(r"<title[^>]*>([^<]{0,200})</title>", re.IGNORECASE)


def is_cloudflare(status: int, text: str, headers: Any = None) -> bool:
    """A Cloudflare check page instead of the answer: Cloudflare says so in its
    ``cf-mitigated`` header, or the page has the check's exact title with the check's status.

    Nothing else counts: Cloudflare puts its ``challenge-platform`` script into ordinary pages
    of the sites it protects, a film may be called "Just a Moment" and a post may quote the
    check's code."""
    if str((headers or {}).get("cf-mitigated") or "").lower() == "challenge":
        return True
    if status not in (403, 503):
        return False
    title = _TITLE.search(text[:8192])
    return title is not None and " ".join(title.group(1).split()).casefold() in _CLOUDFLARE_TITLES


def client(
    ua: str | None = None,
    cookies: dict[str, str] | None = None,
    follow_redirects: bool = True,
    *,
    public_only: bool = False,
) -> httpx.Client:
    if public_only:
        from tow.net_guard import PublicOnlyTransport

        transport = PublicOnlyTransport()
    else:
        transport = None
    return httpx.Client(
        headers={"User-Agent": ua or UA_DEFAULT},
        cookies=cookies or {},
        follow_redirects=follow_redirects,
        timeout=httpx.Timeout(20.0, connect=5.0),
        transport=transport,
        trust_env=not public_only,
    )


def get_limited(c: httpx.Client, url: str, *, max_bytes: int) -> httpx.Response:
    """Stream one response into a bounded in-memory httpx.Response."""
    return request_limited(c, "GET", url, max_bytes=max_bytes)


def request_limited(c: httpx.Client, method: str, url: str, *, max_bytes: int, **kwargs: Any) -> httpx.Response:
    """``c.request(method, url, **kwargs)`` with the body streamed into a bounded response."""
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    # A few adapter tests use a deliberately tiny client double. Real httpx
    # clients always take the streaming branch below.
    if not hasattr(c, "stream"):
        response = c.get(url) if method == "GET" and not kwargs else c.request(method, url, **kwargs)
        declared = response.headers.get("content-length")
        if declared:
            try:
                if int(declared) > max_bytes:
                    raise ResponseTooLargeError(f"response body exceeds {max_bytes} bytes")
            except ValueError:
                pass
        if len(response.content) > max_bytes:
            raise ResponseTooLargeError(f"response body exceeds {max_bytes} bytes")
        return response
    with c.stream(method, url, **kwargs) as response:
        declared = response.headers.get("content-length")
        if declared:
            try:
                declared_size = int(declared)
            except ValueError:
                declared_size = -1
            if declared_size > max_bytes:
                raise ResponseTooLargeError(f"response body exceeds {max_bytes} bytes")
        chunks: list[bytes] = []
        size = 0
        deadline = time.monotonic() + MAX_RESPONSE_SECONDS
        for chunk in response.iter_bytes():
            if time.monotonic() > deadline:
                raise ResponseTooSlowError(f"response body took longer than {MAX_RESPONSE_SECONDS:.0f} s")
            size += len(chunk)
            if size > max_bytes:
                raise ResponseTooLargeError(f"response body exceeds {max_bytes} bytes")
            chunks.append(chunk)
        # ``iter_bytes()`` returns the decoded entity body.  Reusing the
        # upstream Content-Encoding header would make the newly constructed
        # response decode those bytes a second time (NNMClub serves gzip),
        # producing ``incorrect header check``.  Content-Length describes the
        # encoded wire body as well, so it must not survive reconstruction.
        # The headers are copied as the bytes the server sent: a value with raw UTF-8
        # (a Cyrillic file name in Content-Disposition) decodes to text httpx cannot
        # encode back as ASCII, and rebuilding from that text failed every download.
        decoded_headers = [
            (name, value)
            for name, value in response.headers.raw
            if name.lower() not in {b"content-encoding", b"content-length", b"transfer-encoding"}
        ]
        return httpx.Response(
            status_code=response.status_code,
            headers=decoded_headers,
            content=b"".join(chunks),
            request=response.request,
        )
