"""Bounded HTTP bodies: size and total time."""

import gzip
import re
import zlib

import httpx
import pytest

from tow import http as thttp


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
        "tow.trackers.match_tracker", lambda trackers, url: type("T", (), {"spec": {"fetch_hosts": ["x"]}})()
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
