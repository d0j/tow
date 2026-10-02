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
