from __future__ import annotations

import codecs
import math
import re
import time
import zlib
from collections.abc import Iterable, Iterator
from contextvars import ContextVar
from datetime import UTC
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

import httpcore
import httpx

MAX_HTML_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_TORRENT_RESPONSE_BYTES = 16 * 1024 * 1024
# Per-read timeouts do not bound a mirror that trickles bytes; this bounds the whole body.
MAX_RESPONSE_SECONDS = 120.0
# And this the whole request - connect, TLS, headers and body: a mirror that sends one header
# byte every 19 s (under the 20 s read timeout) would otherwise hold the check for hours.
MAX_REQUEST_SECONDS = MAX_RESPONSE_SECONDS + 30.0


def _http_date(value: object) -> float | None:
    """An HTTP-date header as epoch seconds; None when it is not one."""
    if not isinstance(value, str) or not value or len(value) > 128:
        return None
    try:
        when = parsedate_to_datetime(value.strip())
        when = when.replace(tzinfo=UTC) if when.tzinfo is None else when
        seconds = when.timestamp()
    except ValueError, TypeError, OverflowError, OSError:
        return None
    return seconds if math.isfinite(seconds) else None


def retry_after_seconds(value: object, *, now: float, date: object = None, age: object = None) -> float:
    """Bounded HTTP Retry-After, delta-seconds or HTTP-date; malformed input is no hint.

    An HTTP-date is counted from the answer's own ``Date`` (plus its cache ``Age``) when the
    server sent a usable one: a computer clock that is off neither lengthens nor shortens the
    pause. Without one, from ``now``. Whatever the dates say, the result is 0 to 86400 seconds.
    """
    if not isinstance(value, str) or not value or len(value) > 128 or not math.isfinite(now):
        return 0
    value = value.strip()
    if re.fullmatch(r"[0-9]+", value):
        return float(min(86400, int(value)))
    when = _http_date(value)
    if when is None:
        return 0
    server_now = _http_date(date)
    if server_now is None:
        seconds = when - now
    else:
        cached = age.strip() if isinstance(age, str) and len(age) <= 16 else ""
        seconds = when - server_now - (int(cached) if re.fullmatch(r"[0-9]+", cached) else 0)
    return min(86400.0, max(0.0, seconds))


def retry_after_from(headers: Any, *, now: float) -> float:
    """``retry_after_seconds`` of an answer's headers: Retry-After with its Date and Age."""
    return retry_after_seconds(headers.get("retry-after"), now=now, date=headers.get("date"), age=headers.get("age"))


class ResponseTooLargeError(RuntimeError):
    """Raised before an untrusted HTTP response can consume unbounded memory."""


class ResponseTooSlowError(RuntimeError):
    """Raised when a response body does not arrive within MAX_RESPONSE_SECONDS."""


class UnsupportedEncodingError(httpx.DecodingError):
    """The server compressed its answer in a way TOW did not ask for and cannot read (``br``,
    ``zstd``...): an answer TOW cannot read, not a connection that failed."""


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


# The deadline of the request this thread is making (request_limited sets it); None: none.
_REQUEST_DEADLINE: ContextVar[float | None] = ContextVar("tow_request_deadline", default=None)


def _budget(timeout: float | None, expired: type[Exception]) -> float | None:
    """A socket operation's timeout cut to what is left of the request's deadline."""
    deadline = _REQUEST_DEADLINE.get()
    if deadline is None:
        return timeout
    left = deadline - time.monotonic()
    if left <= 0:
        raise expired("request time budget exhausted")
    return left if timeout is None else min(left, timeout)


class _DeadlineStream(httpcore.NetworkStream):
    """A connection whose every read, write and TLS handshake ends by the deadline of the
    request using it - also when the pool hands the connection to a later request."""

    def __init__(self, wrapped: httpcore.NetworkStream) -> None:
        self.wrapped = wrapped

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return self.wrapped.read(max_bytes, timeout=_budget(timeout, httpcore.ReadTimeout))

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self.wrapped.write(buffer, timeout=_budget(timeout, httpcore.WriteTimeout))

    def close(self) -> None:
        self.wrapped.close()

    def start_tls(
        self, ssl_context: Any, server_hostname: str | None = None, timeout: float | None = None
    ) -> httpcore.NetworkStream:
        tls = self.wrapped.start_tls(ssl_context, server_hostname, timeout=_budget(timeout, httpcore.ConnectTimeout))
        return _DeadlineStream(tls)

    def get_extra_info(self, info: str) -> Any:
        return self.wrapped.get_extra_info(info)


class RequestDeadlineBackend(httpcore.SyncBackend):
    """The pool's own backend (the public-only one, or httpcore's), with every connection it
    makes held to the deadline of the request using it."""

    def __init__(self, wrapped: httpcore.NetworkBackend) -> None:
        self.wrapped = wrapped

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.NetworkStream:
        timeout = _budget(timeout, httpcore.ConnectTimeout)
        return _DeadlineStream(self.wrapped.connect_tcp(host, port, timeout, local_address, socket_options))

    def connect_unix_socket(
        self, path: str, timeout: float | None = None, socket_options: Iterable[Any] | None = None
    ) -> httpcore.NetworkStream:
        timeout = _budget(timeout, httpcore.ConnectTimeout)
        return _DeadlineStream(self.wrapped.connect_unix_socket(path, timeout, socket_options))

    def sleep(self, seconds: float) -> None:
        self.wrapped.sleep(seconds)


def _hold_to_request_deadlines(c: httpx.Client) -> httpx.Client:
    """Put RequestDeadlineBackend under every connection pool of ``c`` (also a proxy's)."""
    for transport in [c._transport, *c._mounts.values()]:
        pool = getattr(transport, "_pool", None)
        backend = getattr(pool, "_network_backend", None)
        if isinstance(pool, httpcore.ConnectionPool) and backend is not None:
            pool._network_backend = RequestDeadlineBackend(backend)
    return c


_LOCAL_SUFFIXES = (".localhost", ".local", ".lan", ".internal", ".home.arpa")


def is_local_host(host: str) -> bool:
    """A loopback, private or link-local address, ``localhost``, a name of the local network
    (``.local``, ``.lan``, ``.home.arpa``...) or a name without a dot: what a proxy from the
    environment never carries (as a browser bypasses the proxy for local addresses)."""
    import ipaddress

    name = host.strip("[]").rstrip(".").lower()
    if not name:
        return False
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return name == "localhost" or "." not in name or name.endswith(_LOCAL_SUFFIXES)
    return address.is_loopback or address.is_private or address.is_link_local


class _LocalHostsDirect(httpx.BaseTransport):
    """A proxy taken from the environment (HTTP(S)_PROXY): a local host goes direct instead.

    With ``allow_private_tracker_hosts`` a site on this computer or the home network is allowed,
    and a system proxy would otherwise carry its traffic - its sign-in and cookies too - unless
    NO_PROXY named it (1.33.1 QA-14). The torrent clients' adapters use no proxy at all."""

    def __init__(self, proxied: httpx.BaseTransport, direct: httpx.BaseTransport) -> None:
        self.proxied, self.direct = proxied, direct

    def route(self, url: httpx.URL) -> httpx.BaseTransport:
        return self.direct if is_local_host(url.host) else self.proxied

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return self.route(request.url).handle_request(request)

    def close(self) -> None:
        self.proxied.close()  # the direct transport is the client's own: it closes it


def _local_hosts_direct(c: httpx.Client) -> httpx.Client:
    for pattern, transport in list(c._mounts.items()):
        if transport is not None:
            c._mounts[pattern] = _LocalHostsDirect(transport, c._transport)
    return c


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
    return _local_hosts_direct(
        _hold_to_request_deadlines(
            httpx.Client(
                headers={"User-Agent": ua or UA_DEFAULT, "Accept-Encoding": "gzip, deflate, identity"},
                cookies=cookies or {},
                follow_redirects=follow_redirects,
                timeout=httpx.Timeout(20.0, connect=5.0),
                transport=transport,
                trust_env=not public_only,
            )
        )
    )


def get_limited(c: httpx.Client, url: str, *, max_bytes: int, deadline: float | None = None) -> httpx.Response:
    """Stream one response into a bounded in-memory httpx.Response."""
    return request_limited(c, "GET", url, max_bytes=max_bytes, deadline=deadline)


def get_public_page(url: str, *, ua: str | None = None) -> httpx.Response:
    """Read an untrusted preview URL through a fresh public-only, IP-pinned client.

    No caller-supplied transport, proxy, cookies or redirect policy can weaken this
    boundary. Each socket checks all DNS answers and connects to a checked numeric
    address, including after DNS rebinding. The response retains the HTML bounds.
    """
    # Reject malformed bracket/authority syntax before HTTPX normalizes it.
    parts = urlsplit(url)
    if not parts.hostname:
        raise ValueError("preview requires an HTTP URL without credentials")
    parsed = httpx.URL(url)
    if parsed.scheme not in {"http", "https"} or not parsed.host or parsed.userinfo:
        raise ValueError("preview requires an HTTP URL without credentials")
    with client(ua=ua, public_only=True, follow_redirects=False) as c:
        # HTTPX parses this same input for the request; keep its data flow visible
        # rather than relying on analyzer models of URL-object string conversion.
        return get_limited(c, url, max_bytes=MAX_HTML_RESPONSE_BYTES)


def _check_response_time(deadline: float) -> None:
    if time.monotonic() > deadline:
        raise ResponseTooSlowError("response time budget exhausted")


class _BoundedDecoder:
    """One coding layer: every zlib allocation has a maximum output length.

    Complete streams need no unbounded flush: eof checks the trailer/CRC and all the
    output has already been produced by decompress. Each layer's aggregate output,
    including every concatenated gzip member, has the same byte limit.
    """

    def __init__(self, encoding: str, limit: int, deadline: float) -> None:
        self.encoding = encoding
        self.limit = limit
        self.deadline = deadline
        self.total = 0
        self.prefix = b""
        self.raw_retry = False
        self.decoder = zlib.decompressobj(zlib.MAX_WBITS | 16) if encoding == "gzip" else None

    def feed(self, raw: bytes) -> bytes:
        _check_response_time(self.deadline)
        if self.decoder is None:
            self.prefix += raw
            if len(self.prefix) < 2:
                return b""
            # HTTP deflate may be zlib-wrapped or raw; deciding from its two-byte
            # zlib header also handles a raw stream split across tiny wire chunks.
            first, second = self.prefix[:2]
            wrapped = first & 15 == 8 and first >> 4 <= 7 and (first * 256 + second) % 31 == 0
            self.raw_retry = wrapped
            self.decoder = zlib.decompressobj(zlib.MAX_WBITS if wrapped else -zlib.MAX_WBITS)
            raw, self.prefix = self.prefix, b""
        out = bytearray()
        while raw:
            _check_response_time(self.deadline)
            if self.decoder.eof:
                if self.encoding != "gzip":
                    raise httpx.DecodingError("trailing deflate data")
                self.decoder = zlib.decompressobj(zlib.MAX_WBITS | 16)
            retry_raw, self.raw_retry = self.raw_retry, False
            try:
                decoded = self.decoder.decompress(raw, self.limit - self.total + 1)
            except zlib.error as exc:
                if not retry_raw:
                    raise httpx.DecodingError("invalid compressed response") from exc
                # A valid raw stored block can resemble a zlib header. Retry only
                # the first failed call, before any output was returned to callers.
                self.decoder = zlib.decompressobj(-zlib.MAX_WBITS)
                try:
                    decoded = self.decoder.decompress(raw, self.limit - self.total + 1)
                except zlib.error as raw_exc:
                    raise httpx.DecodingError("invalid compressed response") from raw_exc
            _check_response_time(self.deadline)
            if len(decoded) > self.limit - self.total:
                raise ResponseTooLargeError(f"decoded response body exceeds {self.limit} bytes")
            self.total += len(decoded)
            out.extend(decoded)
            raw = self.decoder.unused_data if self.decoder.eof else self.decoder.unconsumed_tail
        return bytes(out)

    def finish(self) -> None:
        if self.decoder is None or not self.decoder.eof:
            raise httpx.DecodingError("truncated compressed response")


def _response_encodings(response: httpx.Response) -> list[str]:
    value = response.headers.get("content-encoding", "identity")
    names = [name.strip().lower() for name in value.split(",")]
    if len(names) > 3 or any(name not in {"gzip", "deflate", "identity"} for name in names):
        raise UnsupportedEncodingError("unsupported response content encoding")
    return [name for name in reversed(names) if name != "identity"]


def _decoded_chunks(response: httpx.Response, *, limit: int, deadline: float, has_body: bool) -> Iterator[bytes]:
    encodings = _response_encodings(response)
    # MockTransport's preloaded responses (and tiny adapters) have already decoded
    # their content before this function. Real streaming HTTP follows iter_raw.
    if response.is_stream_consumed:
        yield from response.iter_bytes(chunk_size=16384)
        return
    decoders = [_BoundedDecoder(name, limit, deadline) for name in encodings]
    wire_size = 0
    for raw in response.iter_raw(chunk_size=16384):
        _check_response_time(deadline)
        wire_size += len(raw)
        if wire_size > limit:
            raise ResponseTooLargeError(f"encoded response body exceeds {limit} bytes")
        for decoder in decoders:
            raw = decoder.feed(raw)
        yield raw
    if has_body and wire_size:
        for decoder in decoders:
            decoder.finish()


def request_limited(
    c: httpx.Client, method: str, url: str, *, max_bytes: int, deadline: float | None = None, **kwargs: Any
) -> httpx.Response:
    """``c.request(method, url, **kwargs)`` with the body streamed into a bounded response.

    The whole request ends by ``deadline``, and at the latest MAX_REQUEST_SECONDS after it
    started: connect, TLS and headers too on a client of ``client()``."""
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    if deadline is not None and (isinstance(deadline, bool) or not math.isfinite(deadline)):
        raise ValueError("invalid response deadline")
    if deadline is not None and time.monotonic() >= deadline:
        raise ResponseTooSlowError("response time budget exhausted")
    whole = time.monotonic() + MAX_REQUEST_SECONDS
    request_deadline = whole if deadline is None else min(deadline, whole)
    token = _REQUEST_DEADLINE.set(request_deadline)
    try:
        return _request_limited(c, method, url, max_bytes=max_bytes, deadline=deadline, **kwargs)
    except httpx.TimeoutException as exc:
        if time.monotonic() >= request_deadline:
            raise ResponseTooSlowError("request time budget exhausted") from exc
        raise
    finally:
        _REQUEST_DEADLINE.reset(token)


def _request_limited(
    c: httpx.Client, method: str, url: str, *, max_bytes: int, deadline: float | None, **kwargs: Any
) -> httpx.Response:
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
        if deadline is not None and time.monotonic() >= deadline:
            raise ResponseTooSlowError("response time budget exhausted")
        return response
    with c.stream(method, url, **kwargs) as response:
        if deadline is not None and time.monotonic() >= deadline:
            raise ResponseTooSlowError("response time budget exhausted")
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
        body_started = time.monotonic()
        body_deadline = body_started + MAX_RESPONSE_SECONDS
        if deadline is not None:
            body_deadline = min(body_deadline, deadline)
        for chunk in _decoded_chunks(
            response,
            limit=max_bytes,
            deadline=body_deadline,
            has_body=method != "HEAD" and response.status_code not in {204, 304},
        ):
            if time.monotonic() > body_deadline:
                # The time it had: a caller's shorter deadline (a recovery's) may have cut it.
                budget = max(0.0, body_deadline - body_started)
                raise ResponseTooSlowError(f"response body took longer than {budget:.0f} s")
            size += len(chunk)
            if size > max_bytes:
                raise ResponseTooLargeError(f"response body exceeds {max_bytes} bytes")
            chunks.append(chunk)
        if deadline is not None and time.monotonic() >= deadline:
            raise ResponseTooSlowError("response time budget exhausted")
        # The bounded decoders return the decoded entity body. Reusing the
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
