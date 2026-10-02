from __future__ import annotations

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


def html_text(response: Any) -> str:
    """Decode a tracker page the way a browser would.

    httpx falls back to UTF-8 when the Content-Type has no charset, which garbles
    windows-1251 forums. Order: header charset, <meta charset>, UTF-8, cp1251.
    """
    content = getattr(response, "content", None)
    if not isinstance(content, (bytes, bytearray)):
        return str(getattr(response, "text", "") or "")
    candidates: list[str] = []
    header = _HEADER_CHARSET.search(str((getattr(response, "headers", None) or {}).get("content-type") or ""))
    if header:
        candidates.append(header.group(1))
    meta = _META_CHARSET.search(bytes(content[:8192]))
    if meta:
        candidates.append(meta.group(1).decode("ascii", "ignore"))
    for encoding in [*candidates, "utf-8"]:
        try:
            return bytes(content).decode(encoding)
        except LookupError, UnicodeDecodeError:
            continue
    return bytes(content).decode("cp1251", errors="replace")


def is_cloudflare(status: int, text: str) -> bool:
    if status in (403, 503) and "just a moment" in text[:4000].lower():
        return True
    return "cf-challenge" in text[:4000].lower()


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
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    # A few adapter tests use a deliberately tiny client double. Real httpx
    # clients always take the streaming branch below.
    if not hasattr(c, "stream"):
        response = c.get(url)
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
    with c.stream("GET", url) as response:
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
        decoded_headers = [
            (name, value)
            for name, value in response.headers.multi_items()
            if name.lower() not in {"content-encoding", "content-length", "transfer-encoding"}
        ]
        return httpx.Response(
            status_code=response.status_code,
            headers=decoded_headers,
            content=b"".join(chunks),
            request=response.request,
        )
