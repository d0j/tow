"""Torrent-client logins and the browser sign-in's debugging traffic never go through a proxy.

A system or ``HTTP_PROXY`` proxy (often set to reach blocked trackers) would otherwise receive
Transmission's ``Authorization`` header, Deluge's and qBittorrent's passwords and the tracker
cookies the sign-in browser hands over, even for a client on ``127.0.0.1``.
"""

from __future__ import annotations

import contextlib
import email.message
import io
import urllib.request
import urllib.response
from typing import Any

import httpx
import pytest
import requests
from requests.adapters import HTTPAdapter
from requests.utils import select_proxy

PROXY = "http://proxy.invalid:3128"


@pytest.fixture
def system_proxy(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(name, PROXY)
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)


def _httpx_routes(monkeypatch) -> list[tuple[str, str | None]]:
    """Each request's URL and the proxy its transport would connect through (None: direct)."""
    seen: list[tuple[str, str | None]] = []

    def handle_request(self, request):
        proxy = getattr(self._pool, "_proxy_url", None)
        seen.append((str(request.url), None if proxy is None else str(proxy)))
        return httpx.Response(200, json={"result": "success", "arguments": {}, "error": None, "id": 1})

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle_request)
    return seen


def test_transmission_rpc_with_its_password_never_takes_a_proxy(monkeypatch, system_proxy):
    from tow.clients.transmission import TransmissionClient

    seen = _httpx_routes(monkeypatch)
    client = TransmissionClient("127.0.0.1", 9091, "tow", "secret")
    client._rpc("session-get")
    assert seen
    assert all(proxy is None for _url, proxy in seen), seen


def test_deluge_login_never_takes_a_proxy(monkeypatch, system_proxy):
    from tow.clients.deluge import DelugeClient

    seen = _httpx_routes(monkeypatch)
    client = DelugeClient("127.0.0.1", 8112, "secret")
    client._post("auth.login", ["secret"])
    assert seen
    assert all(proxy is None for _url, proxy in seen), seen


def _requests_routes(monkeypatch, answer=None) -> list[tuple[str, str, str | None]]:
    seen: list[tuple[str, str, str | None]] = []

    def send(self, request, **kwargs):
        seen.append((request.method, request.url, select_proxy(request.url, kwargs.get("proxies") or {})))
        response = requests.Response()
        response.status_code, response.url, response.request = 200, request.url, request
        response.headers["Content-Type"] = "text/plain"
        response._content = b"v5.0.0"
        if answer is not None:
            answer(request, response)
        return response

    monkeypatch.setattr(HTTPAdapter, "send", send)
    return seen


def test_qbittorrent_requests_never_take_a_proxy(monkeypatch, system_proxy):
    from tow.clients import qbittorrent

    seen = _requests_routes(monkeypatch)
    client = qbittorrent.QBittorrentClient("127.0.0.1", 8080, "tow", "secret")
    assert client._c.app_version() == "v5.0.0"
    assert seen
    assert all(proxy is None for _method, _url, proxy in seen), seen


def test_qbittorrent_never_follows_a_redirect_with_its_session(monkeypatch):
    """A redirect would carry the login form (307/308) or the session cookie elsewhere."""
    from tow.clients import qbittorrent

    def elsewhere(request: Any, response: requests.Response) -> None:
        if "elsewhere.invalid" not in request.url:
            response.status_code = 307
            response.headers["Location"] = "http://elsewhere.invalid/api/v2/app/version"

    seen = _requests_routes(monkeypatch, elsewhere)
    client = qbittorrent.QBittorrentClient("127.0.0.1", 8080, "tow", "secret")
    with contextlib.suppress(Exception):  # whatever the library makes of the 307 itself
        client._c.app_version()
    assert not [url for _method, url, _proxy in seen if "elsewhere.invalid" in url], seen


def test_the_sign_in_browser_debugging_endpoint_is_read_without_a_proxy(monkeypatch, system_proxy):
    from tow import browser_auth as ba

    hosts: list[str] = []

    def http_open(self, req):
        hosts.append(req.host)
        headers = email.message.Message()
        headers["Content-Type"] = "application/json"
        answer = urllib.response.addinfourl(io.BytesIO(b"[]"), headers, req.full_url, 200)
        answer.msg = "OK"
        return answer

    monkeypatch.setattr(urllib.request.HTTPHandler, "http_open", http_open)
    monkeypatch.setattr(urllib.request, "_opener", None)  # no opener built before the proxy was set
    assert ba._json_get(9222, "/json/list") == []
    assert hosts == ["127.0.0.1:9222"]


def test_the_sign_in_browser_debugging_socket_is_opened_without_a_proxy(monkeypatch):
    import asyncio

    import websockets.asyncio.client as ws_client

    from tow import browser_auth as ba

    opened: list[dict[str, Any]] = []

    class Stop(Exception):
        pass

    def connect(url, **kwargs):
        opened.append({"url": url, **kwargs})
        raise Stop

    async def target(*_a):
        return "ws://127.0.0.1:9222/devtools/page/1"

    monkeypatch.setattr(ws_client, "connect", connect)
    monkeypatch.setattr(ba, "_wait_target", target)
    with pytest.raises(Stop):
        asyncio.run(ba._authenticated_topic(9222, "https://tracker.example/t/1", None, 10**12))  # type: ignore[arg-type]
    assert len(opened) == 1
    assert "proxy" in opened[0]
    assert opened[0]["proxy"] is None
