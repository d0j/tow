"""Bounded HTTP bodies: size and total time."""

import gzip
import re
import zlib

import httpcore
import httpx
import pytest

from tow import http as thttp

# The real transport (the test guard refuses it): the trickle tests fake the network below it.
_REAL_HANDLE_REQUEST = httpx.HTTPTransport.handle_request


def _client(body: bytes) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body)))


def test_body_within_limits_is_returned():
    with _client(b"d4:infod4:name1:aee") as c:
        assert thttp.get_limited(c, "https://t.example/x", max_bytes=1024).content == b"d4:infod4:name1:aee"


def test_slow_body_hits_total_deadline(monkeypatch):
    monkeypatch.setattr(thttp, "MAX_RESPONSE_SECONDS", -1.0)
    with _client(b"x" * 10) as c, pytest.raises(thttp.ResponseTooSlowError):
        thttp.get_limited(c, "https://t.example/x", max_bytes=1024)


def test_oversized_body_is_rejected():
    with _client(b"x" * 2048) as c, pytest.raises(thttp.ResponseTooLargeError):
        thttp.get_limited(c, "https://t.example/x", max_bytes=1024)


def _encoded_client(raw: bytes, encoding: str, *, chunk_size: int = 16384) -> httpx.Client:
    class Wire(httpx.SyncByteStream):
        def __iter__(self):
            for at in range(0, len(raw), chunk_size):
                yield raw[at : at + chunk_size]

    return httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=Wire(), headers={"content-encoding": encoding})
        )
    )


def _deflate(raw: bytes) -> bytes:
    compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    return compressor.compress(raw) + compressor.flush()


@pytest.mark.parametrize(
    ("encoding", "compress"),
    [("gzip", gzip.compress), ("deflate", zlib.compress), ("deflate", _deflate)],
)
@pytest.mark.parametrize("chunk_size", [1, 17, 16384])
def test_streaming_gzip_and_wrapped_or_raw_deflate_decode_with_bounded_chunks(encoding, compress, chunk_size):
    original = "Страница раздачи — серия 1".encode()
    with _encoded_client(compress(original), encoding, chunk_size=chunk_size) as client:
        response = thttp.get_limited(client, "https://t.example/x", max_bytes=1024)
    assert response.content == original
    assert "content-encoding" not in response.headers
    assert response.headers["content-length"] == str(len(original))


@pytest.fixture
def bounded_decompression(monkeypatch):
    original = zlib.decompressobj
    produced = []

    class Observed:
        def __init__(self, *args):
            self.delegate = original(*args)

        def __getattr__(self, name):
            return getattr(self.delegate, name)

        def decompress(self, raw, max_length=0):
            assert max_length > 0, "an unbounded decompression was requested"
            out = self.delegate.decompress(raw, max_length)
            produced.append((max_length, len(out)))
            return out

        def flush(self, *args):
            pytest.fail("unbounded decompressor flush called")

    monkeypatch.setattr(zlib, "decompressobj", Observed)
    return produced


def test_gzip_bomb_is_refused_before_expanding_the_whole_payload(bounded_decompression):
    raw = gzip.compress(b"x" * (3 * 1024 * 1024))
    assert len(raw) < 8192
    with _encoded_client(raw, "gzip") as client, pytest.raises(thttp.ResponseTooLargeError, match="decoded"):
        thttp.get_limited(client, "https://t.example/x", max_bytes=8192)
    assert bounded_decompression == [(8193, 8193)]


def test_concatenated_gzip_members_share_the_output_budget(bounded_decompression):
    raw = b"".join(gzip.compress(b"x" * 4096) for _ in range(3))
    with _encoded_client(raw, "gzip") as client, pytest.raises(thttp.ResponseTooLargeError, match="decoded"):
        thttp.get_limited(client, "https://t.example/x", max_bytes=8192)
    assert bounded_decompression == [(8193, 4096), (4097, 4096), (1, 1)]


def test_complete_concatenated_gzip_members_are_returned():
    raw = gzip.compress(b"first") + gzip.compress(b"second")
    with _encoded_client(raw, "gzip", chunk_size=1) as client:
        assert thttp.get_limited(client, "https://t.example/x", max_bytes=1024).content == b"firstsecond"


@pytest.mark.parametrize("outer", [gzip.compress, zlib.compress])
def test_nested_encodings_each_enforce_the_output_limit(bounded_decompression, outer):
    raw = outer(gzip.compress(b"x" * (3 * 1024 * 1024)))
    encoding = "gzip, gzip" if outer is gzip.compress else "gzip, deflate"
    with _encoded_client(raw, encoding) as client, pytest.raises(thttp.ResponseTooLargeError, match="decoded"):
        thttp.get_limited(client, "https://t.example/x", max_bytes=8192)
    assert max(produced for _bound, produced in bounded_decompression) == 8193
    assert all(1 <= bound <= 8193 and produced <= bound for bound, produced in bounded_decompression)


def test_nested_encoding_order_returns_the_original_content():
    original = b"a torrent page"
    raw = zlib.compress(gzip.compress(original))
    with _encoded_client(raw, "gzip, deflate", chunk_size=1) as client:
        assert thttp.get_limited(client, "https://t.example/x", max_bytes=1024).content == original


@pytest.mark.parametrize("encoding", ["br", "zstd", "compress", "gzip, gzip, gzip, gzip", "gzip,,deflate"])
def test_unsupported_or_excessive_encoding_is_refused(encoding):
    with _encoded_client(b"not decoded", encoding) as client, pytest.raises(httpx.DecodingError, match="unsupported"):
        thttp.get_limited(client, "https://t.example/x", max_bytes=1024)


@pytest.mark.parametrize(
    ("encoding", "raw"),
    [
        ("gzip", gzip.compress(b"hello")[:-1]),
        ("deflate", zlib.compress(b"hello")[:-1]),
        ("deflate", _deflate(b"hello")[:-1]),
        ("deflate", b"x"),
        ("gzip", gzip.compress(b"hello") + b"x"),
    ],
)
def test_truncated_compression_is_refused(encoding, raw):
    with _encoded_client(raw, encoding) as client, pytest.raises(httpx.DecodingError, match="truncated"):
        thttp.get_limited(client, "https://t.example/x", max_bytes=1024)


@pytest.mark.parametrize(
    ("encoding", "raw"),
    [
        ("gzip", gzip.compress(b"hello")[:-8] + b"\0" * 8),
        ("gzip", b"not a gzip stream"),
        ("deflate", zlib.compress(b"hello") + b"unexpected trailing bytes"),
    ],
)
def test_bad_crc_invalid_compression_and_deflate_junk_are_refused(encoding, raw):
    with _encoded_client(raw, encoding) as client, pytest.raises(httpx.DecodingError, match=r"invalid|trailing"):
        thttp.get_limited(client, "https://t.example/x", max_bytes=1024)


def test_wire_size_is_bounded_even_when_decoded_output_is_small():
    raw = b"".join(gzip.compress(b"") for _ in range(100))
    with _encoded_client(raw, "gzip") as client, pytest.raises(thttp.ResponseTooLargeError, match="encoded"):
        thttp.get_limited(client, "https://t.example/x", max_bytes=1024)


def test_tracker_client_only_advertises_bounded_supported_encodings():
    with thttp.client(public_only=True) as client:
        assert client.headers["accept-encoding"] == "gzip, deflate, identity"


@pytest.mark.parametrize("compress", [zlib.compress, _deflate])
def test_deflate_prefix_and_trailer_can_cross_decoder_feeds(compress):
    decoder = thttp._BoundedDecoder("deflate", 1024, float("inf"))
    original = b"a page split across reads"
    raw = compress(original)
    decoded = b"".join(decoder.feed(raw[at : at + 1]) for at in range(len(raw)))
    decoder.finish()
    assert decoded == original


def test_decoder_timeout_also_applies_between_empty_gzip_members(monkeypatch):
    ticks = iter([1.0, 1.0, 1.0, 3.0])
    monkeypatch.setattr(thttp.time, "monotonic", lambda: next(ticks))
    decoder = thttp._BoundedDecoder("gzip", 1024, 2.0)
    with pytest.raises(thttp.ResponseTooSlowError, match="time budget"):
        decoder.feed(gzip.compress(b"") + gzip.compress(b""))


def test_raw_deflate_can_start_with_bytes_resembling_a_zlib_header(bounded_decompression):
    raw = b"\x78\x9c\x00\x63\xff" + b"x" * 156 + b"\x01\x00\x00\xff\xff"
    with _encoded_client(raw, "deflate") as client:
        assert thttp.get_limited(client, "https://t.example/x", max_bytes=1024).content == b"x" * 156
    assert bounded_decompression == [(1025, 156)]


def test_bad_zlib_trailer_after_returned_output_never_falls_back_to_raw(bounded_decompression):
    decoder = thttp._BoundedDecoder("deflate", 1024, float("inf"))
    raw = zlib.compress(b"hello")
    assert decoder.feed(raw[:-4]) == b"hello"
    with pytest.raises(httpx.DecodingError, match="invalid"):
        decoder.feed(b"\0" * 4)
    assert bounded_decompression == [(1025, 5)]


@pytest.mark.parametrize("encoding", ["gzip", "deflate"])
@pytest.mark.parametrize(("method", "status"), [("GET", 200), ("GET", 302), ("HEAD", 200)])
def test_zero_wire_body_keeps_empty_compressed_response_compatibility(encoding, method, status):
    class Empty(httpx.SyncByteStream):
        def __iter__(self):
            yield from ()

    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                status, stream=Empty(), headers={"content-encoding": encoding, "location": "/next"}
            )
        )
    ) as client:
        response = thttp.request_limited(client, method, "https://t.example/x", max_bytes=1024)
    assert response.status_code == status
    assert response.content == b""
    assert response.headers["location"] == "/next"
    assert "content-encoding" not in response.headers


def _response(content: bytes, content_type: str) -> httpx.Response:
    return httpx.Response(200, content=content, headers={"content-type": content_type})


def test_html_text_uses_meta_charset_when_header_has_none():
    page = '<html><head><meta charset="windows-1251"><title>Сериал А</title></head></html>'.encode("cp1251")
    assert "Сериал А" in thttp.html_text(_response(page, "text/html"))


def test_html_text_prefers_header_charset():
    page = "<title>Сериал А</title>".encode("cp1251")
    assert "Сериал А" in thttp.html_text(_response(page, "text/html; charset=windows-1251"))


def test_html_text_falls_back_to_cp1251_for_undeclared_non_utf8():
    page = "<title>Сериал А</title>".encode("cp1251")
    assert "Сериал А" in thttp.html_text(_response(page, "text/html"))


def test_html_text_keeps_utf8():
    assert "Сериал А" in thttp.html_text(_response("<title>Сериал А</title>".encode(), "text/html"))


TITLE = "Сериал Ж [01x01-10 из 12] — 1080p"


def test_html_text_meta_beats_a_servers_latin1_default():
    page = f"<html><head><meta charset=windows-1251><title>{TITLE}</title></head></html>".encode("cp1251")
    assert TITLE in thttp.html_text(_response(page, "text/html; charset=ISO-8859-1"))


@pytest.mark.parametrize("charset", ["ISO-8859-1", "latin1", "windows-1252"])
def test_html_text_a_servers_latin1_default_alone_is_not_believed(charset):
    """Without a <meta charset> the server's default named the page: latin-1 never fails, so a
    windows-1251 page came out as "Ñåðèàë"."""
    page = f"<html><head><title>{TITLE}</title></head></html>".encode("cp1251")
    assert TITLE in thttp.html_text(_response(page, f"text/html; charset={charset}"))


def test_html_text_valid_utf8_beats_a_wrong_cp1251_header():
    page = f"<html><title>{TITLE}</title></html>".encode()
    assert TITLE in thttp.html_text(_response(page, "text/html; charset=windows-1251"))


def test_html_text_byte_order_mark_comes_first():
    page = b"\xef\xbb\xbf" + f"<html><title>{TITLE}</title></html>".encode()
    assert thttp.html_text(_response(page, "text/html; charset=windows-1251")).startswith("<html><title>Сериал")


def test_a_page_title_without_an_html_content_type_is_read_with_the_same_rules(monkeypatch):
    from tow import title

    page = b"\xef\xbb\xbf" + f"<html><title>{TITLE}</title></html>".encode()
    response = httpx.Response(200, content=page, headers={"content-type": "application/octet-stream"})
    monkeypatch.setattr("tow.mirrors.origin_key", lambda url: "o")
    monkeypatch.setattr("tow.trackers.load_trackers", lambda cfg: {})
    monkeypatch.setattr("tow.http.get_limited", lambda c, url, max_bytes: response)
    monkeypatch.setattr(
        "tow.trackers.match_tracker",
        lambda trackers, url: type("T", (), {"spec": {"fetch_hosts": ["https://t.example"]}})(),
    )
    assert title._title_from_page("https://t.example/1") == TITLE


def test_a_raw_utf8_header_survives_the_bounded_response():
    # A site sends the .torrent's Cyrillic file name as raw UTF-8 bytes in Content-Disposition:
    # rebuilding the response from the decoded text raised UnicodeEncodeError on every download.
    disposition = 'attachment; filename="Кириллица.Сериал.torrent"'.encode()

    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers=[(b"Content-Type", b"application/x-bittorrent"), (b"Content-Disposition", disposition)],
            content=b"d4:infod4:name1:aee",
        )

    with httpx.Client(transport=httpx.MockTransport(answer)) as c:
        response = thttp.get_limited(c, "https://t.example/download/108", max_bytes=1024)
    assert response.content == b"d4:infod4:name1:aee"
    assert (b"Content-Disposition", disposition) in response.headers.raw


_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


@pytest.mark.parametrize(
    "charset",
    ["unicode_escape", "raw_unicode_escape", "utf-7", "undefined", "idna", "punycode", "charmap", "base64", "rot_13"],
)
def test_a_charset_that_is_no_page_encoding_is_not_used(charset):
    # "undefined" raised a plain UnicodeError (a 500 on /sites/guess); the escapes and UTF-7 gave
    # lone surrogates that no state file can hold, so every topic's check results were lost.
    page = f'<meta charset="{charset}"><title>{TITLE} \\udfff +2//f-</title>'.encode("cp1251")
    for content_type in ("text/html", f"text/html; charset={charset}"):
        text = thttp.html_text(_response(page, content_type))
        assert TITLE in text
        assert not _LONE_SURROGATE.search(text)


def test_the_fuzzed_pages_decode_without_an_error_or_a_lone_surrogate():
    undefined = b"<meta charset=undefined><a href=dl.php?t=1>\xcf</a>"
    assert thttp.html_text(_response(undefined, "text/html")) == "<meta charset=undefined><a href=dl.php?t=1>П</a>"
    escapes = rb'<meta charset="unicode_escape"><h1>Show \udfff</h1>'  # the six characters, not a code point
    assert thttp.html_text(_response(escapes, "text/html")) == escapes.decode("ascii")


def _http_date(epoch: float) -> str:
    from datetime import UTC, datetime
    from email.utils import format_datetime

    return format_datetime(datetime.fromtimestamp(epoch, UTC), usegmt=True)


@pytest.mark.parametrize("skew", [-7200, 0, 7200])
def test_an_http_date_retry_after_is_read_against_the_servers_own_clock(skew):
    # The server says "retry at 10:10" and its Date says 10:00: ten minutes, whatever this
    # computer's clock says (two hours behind, right or two hours ahead).
    server_now = 1_000_000.0
    value = _http_date(server_now + 600)
    assert thttp.retry_after_seconds(value, now=server_now + skew, date=_http_date(server_now)) == 600


def test_a_cached_answer_subtracts_its_age():
    value = _http_date(1_000_600)
    assert thttp.retry_after_seconds(value, now=0, date=_http_date(1_000_000), age="200") == 400
    assert thttp.retry_after_seconds(value, now=0, date=_http_date(1_000_000), age="900") == 0


@pytest.mark.parametrize(("date", "age"), [("not a date", None), (None, "-1"), ("", "1.5"), ("x" * 200, "9" * 40)])
def test_unusable_date_or_age_falls_back_to_the_local_clock(date, age):
    value = _http_date(1_000_600)
    assert thttp.retry_after_seconds(value, now=1_000_000, date=date, age=age) == 600


@pytest.mark.parametrize(
    ("retry_at", "server_now", "expected"),
    [(10**10, 0, 86400), (0, 10**9, 0), (1_086_400, 1_000_000, 86400), (1_086_401, 1_000_000, 86400)],
)
def test_a_server_date_cannot_make_an_unbounded_pause(retry_at, server_now, expected):
    value = _http_date(retry_at)
    assert thttp.retry_after_seconds(value, now=5, date=_http_date(server_now)) == expected


def test_delta_seconds_ignore_the_date():
    assert thttp.retry_after_seconds("120", now=0, date=_http_date(10**9), age="50") == 120


def test_retry_after_from_headers_reads_date_and_age():
    headers = httpx.Headers({"retry-after": _http_date(1_000_600), "date": _http_date(1_000_000), "age": "100"})
    assert thttp.retry_after_from(headers, now=0) == 500
    assert thttp.retry_after_from(httpx.Headers({}), now=0) == 0


def test_a_services_dated_retry_after_uses_its_date():
    from tow.protection import providers

    assert providers._retry_after(_http_date(10**9 + 120), _http_date(10**9)) == 120


def test_a_body_cut_by_a_shorter_deadline_says_that_deadline(monkeypatch):
    # The message said "longer than 120 s" when the caller's 30-second recovery deadline cut it.
    clock = [1000.0]
    monkeypatch.setattr(thttp.time, "monotonic", lambda: clock[0])

    def slow_chunks(*_args, **_kwargs):
        clock[0] += 100  # the body trickles in
        yield b"x"

    monkeypatch.setattr(thttp, "_decoded_chunks", slow_chunks)
    with _client(b"x" * 10) as c, pytest.raises(thttp.ResponseTooSlowError) as slow:
        thttp.get_limited(c, "https://t.example/x", max_bytes=1024, deadline=1030.0)
    assert str(slow.value) == "response body took longer than 30 s"


# --- one deadline for the whole request: connect, TLS, headers and body ---------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


class _TrickleStream(httpcore.NetworkStream):
    """A mirror that sends one byte of its headers every 19 s, under the 20 s read timeout.

    Each read waits as long as that would (on the fake clock), or times out like a socket
    when it is given less. After ``give_up`` bytes it closes the connection, so a test of
    code that has no overall deadline ends too (with a protocol error instead)."""

    def __init__(self, clock: _Clock, *, every: float = 19.0, give_up: int = 3000) -> None:
        self.clock, self.every, self.give_up = clock, every, give_up
        self.sent = 0
        self.wire = b"HTTP/1.1 200 OK\r\nX-Slow: " + b"a" * (give_up + 100)

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        if timeout is not None and timeout < self.every:
            self.clock.now += timeout
            raise httpcore.ReadTimeout("timed out")
        self.clock.now += self.every
        if self.sent >= self.give_up:
            return b""
        self.sent += 1
        return self.wire[self.sent - 1 : self.sent]

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        return None

    def close(self) -> None:
        return None

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        return self

    def get_extra_info(self, info: str):
        return None


@pytest.fixture
def trickling_mirror(monkeypatch):
    from types import SimpleNamespace

    from tow import net_guard

    clock = _Clock()
    streams: list[_TrickleStream] = []

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        streams.append(_TrickleStream(clock))
        return streams[-1]

    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", connect_tcp)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _REAL_HANDLE_REQUEST)
    monkeypatch.setattr(net_guard, "public_addresses", lambda host, port: ["203.0.113.7"])
    monkeypatch.setattr(thttp, "time", SimpleNamespace(monotonic=clock.monotonic))
    return clock, streams


@pytest.mark.parametrize("public_only", [True, False])
def test_a_mirror_trickling_its_headers_is_cut_at_the_request_deadline(trickling_mirror, public_only):
    clock, streams = trickling_mirror
    started = clock.now
    with thttp.client(follow_redirects=False, public_only=public_only) as c, pytest.raises(thttp.ResponseTooSlowError):
        thttp.get_limited(c, "http://mirror.example/topic", max_bytes=thttp.MAX_HTML_RESPONSE_BYTES)
    assert clock.now - started <= 151  # MAX_RESPONSE_SECONDS + 30, not hours
    assert streams[0].sent < 50


def test_a_callers_shorter_deadline_also_cuts_the_headers(trickling_mirror):
    clock, _streams = trickling_mirror
    started = clock.now
    with thttp.client(follow_redirects=False) as c, pytest.raises(thttp.ResponseTooSlowError):
        thttp.request_limited(c, "POST", "http://mirror.example/login", max_bytes=1024, deadline=started + 30)
    assert clock.now - started <= 31


def test_every_request_gets_its_own_deadline(trickling_mirror, monkeypatch):
    clock, streams = trickling_mirror

    def answer(self, max_bytes, timeout=None):
        self.clock.now += 1
        if self.sent:
            return b""
        self.sent = 1
        return b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"

    monkeypatch.setattr(_TrickleStream, "read", answer)
    with thttp.client(follow_redirects=False) as c:
        for _ in range(3):
            clock.now += thttp.MAX_REQUEST_SECONDS  # far past the previous request's deadline
            assert thttp.get_limited(c, "http://mirror.example/topic", max_bytes=1024).content == b"ok"
    assert len(streams) == 3


class _RecordingStream(httpcore.NetworkStream):
    """A connection that records the timeout of each operation it is given."""

    def __init__(self, seen: list) -> None:
        self.seen = seen

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        self.seen.append(("read", timeout))
        return b""

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self.seen.append(("write", timeout))

    def close(self) -> None:
        return None

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self.seen.append(("start_tls", timeout))
        return self

    def get_extra_info(self, info: str):
        return info


class _RecordingBackend(httpcore.NetworkBackend):
    def __init__(self) -> None:
        self.seen: list = []

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.seen.append(("connect_tcp", timeout))
        return _RecordingStream(self.seen)

    def connect_unix_socket(self, path, timeout=None, socket_options=None):
        self.seen.append(("connect_unix_socket", timeout))
        return _RecordingStream(self.seen)

    def sleep(self, seconds: float) -> None:
        self.seen.append(("sleep", seconds))


def test_every_socket_operation_gets_what_is_left_of_the_request_deadline(monkeypatch):
    """The deadline backend itself (not only that it is installed): each connect, read, write and
    TLS handshake gets the time left, the deadline's end refuses them, and without a deadline
    the timeouts pass unchanged."""
    from types import SimpleNamespace

    clock = _Clock()
    monkeypatch.setattr(thttp, "time", SimpleNamespace(monotonic=clock.monotonic))
    wrapped = _RecordingBackend()
    backend = thttp.RequestDeadlineBackend(wrapped)

    token = thttp._REQUEST_DEADLINE.set(clock.now + 10)
    try:
        stream = backend.connect_tcp("mirror.example", 443, timeout=30)
        clock.now += 4
        stream.read(1024, timeout=20)
        stream.write(b"x", timeout=None)
        tls = stream.start_tls(None, "mirror.example", timeout=2)
        assert tls.get_extra_info("server_addr") == "server_addr"
        backend.connect_unix_socket("/run/x.sock", timeout=None)
        backend.sleep(0.5)
        assert wrapped.seen == [
            ("connect_tcp", 10),
            ("read", 6),
            ("write", 6),
            ("start_tls", 2),
            ("connect_unix_socket", 6),
            ("sleep", 0.5),
        ]
        clock.now += 6  # the deadline is reached
        with pytest.raises(httpcore.ReadTimeout, match="budget exhausted"):
            stream.read(1024, timeout=20)
        with pytest.raises(httpcore.WriteTimeout, match="budget exhausted"):
            stream.write(b"x", timeout=20)
        with pytest.raises(httpcore.ConnectTimeout, match="budget exhausted"):
            stream.start_tls(None, "mirror.example", timeout=20)
        with pytest.raises(httpcore.ConnectTimeout, match="budget exhausted"):
            backend.connect_tcp("mirror.example", 443, timeout=30)
        with pytest.raises(httpcore.ConnectTimeout, match="budget exhausted"):
            backend.connect_unix_socket("/run/x.sock", timeout=30)
    finally:
        thttp._REQUEST_DEADLINE.reset(token)
    wrapped.seen.clear()
    stream = backend.connect_tcp("mirror.example", 443, timeout=30)
    stream.read(1024, timeout=None)
    assert wrapped.seen == [("connect_tcp", 30), ("read", None)]
