"""Bounded HTTP bodies: size and total time."""

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
