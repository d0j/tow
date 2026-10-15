"""Untrusted preview requests cannot inherit private-tracker access."""

from __future__ import annotations

import socket

import httpcore
import httpx
import pytest
from helpers import PUBLIC_IPV4

from tow import guess, http, title

# Capture the real transport before the per-test network guard replaces it.
# The fixture restores only this HTTP layer; DNS and the socket backend are
# synthetic, and the guard still refuses real socket connects and processes.
_HTTP_HANDLE_REQUEST = httpx.HTTPTransport.handle_request


class _Stream(httpcore.NetworkStream):
    def __init__(self, body=b"<html><h1>Show A</h1></html>", status="200 OK", headers=b""):
        self.response = (
            f"HTTP/1.1 {status}\r\nContent-Type: text/html\r\nContent-Length: {len(body)}\r\n".encode()
            + headers
            + b"Connection: close\r\n\r\n"
            + body
        )
        self.writes = []
        self.sni = []
        self.closed = False

    def read(self, max_bytes, timeout=None):
        value, self.response = self.response[:max_bytes], self.response[max_bytes:]
        return value

    def write(self, buffer, timeout=None):
        self.writes.append(bytes(buffer))

    def close(self):
        self.closed = True

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self.sni.append(server_hostname)
        return self

    def get_extra_info(self, info):
        return None


@pytest.fixture
def network(monkeypatch):
    evidence = {"dns": [], "connect": [], "addresses": [PUBLIC_IPV4], "stream": _Stream()}

    def resolve(host, port, **kwargs):
        evidence["dns"].append((host, port))
        return [
            (socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))
            for address in evidence["addresses"]
        ]

    def connect(self, host, port, *args, **kwargs):
        evidence["connect"].append((host, port))
        return evidence["stream"]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", connect)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _HTTP_HANDLE_REQUEST)
    return evidence


@pytest.mark.parametrize("private_enabled", [False, True])
@pytest.mark.parametrize("entry", ["site", "title"])
@pytest.mark.parametrize(
    "addresses",
    [
        ["127.0.0.1"],
        ["192.168.1.2"],
        ["169.254.169.254"],
        ["100.64.0.1"],
        ["::1"],
        ["::ffff:127.0.0.1"],
        ["64:ff9b::7f00:1"],
        [PUBLIC_IPV4, "127.0.0.1"],
    ],
)
def test_preview_entry_points_refuse_unsafe_dns(network, monkeypatch, addresses, entry, private_enabled):
    cfg = {"allow_private_tracker_hosts": private_enabled}
    monkeypatch.setattr("tow.config.load_config", lambda: cfg)
    network["addresses"] = addresses
    if entry == "site":
        assert guess._topic_page("https://forum.example/viewtopic.php?t=7") == ""
    else:
        # A configured public site must still use checked sockets when private access is off.
        # With private access explicitly enabled, configured origins are the owner's allowlist;
        # an unconfigured address must not be fetched at all.
        assert title._title_from_page("https://forum.example/viewtopic.php?t=7") == ""
    assert network["connect"] == []


def test_configured_title_uses_public_dns_policy_by_default(network, monkeypatch):
    cfg = {
        "trackers": {
            "custom": {
                "url_regex": r"^https://forum\.example/viewtopic\.php\?t=(\d+)$",
                "fetch_hosts": ["https://forum.example"],
            }
        }
    }
    monkeypatch.setattr("tow.config.load_config", lambda: cfg)
    network["addresses"] = ["127.0.0.1"]
    assert title._title_from_page("https://forum.example/viewtopic.php?t=7") == ""
    assert network["dns"] == [("forum.example", 443)]
    assert network["connect"] == []


def test_public_boundary_pins_socket_and_keeps_host_and_tls_sni(network, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9999")
    response = http.get_public_page("https://forum.example/viewtopic.php?t=7")
    assert response.status_code == 200
    assert response.content == b"<html><h1>Show A</h1></html>"
    assert network["dns"] == [("forum.example", 443)]
    assert network["connect"] == [(PUBLIC_IPV4, 443)]
    assert network["stream"].sni == ["forum.example"]
    request = b"".join(network["stream"].writes).lower()
    assert b"host: forum.example\r\n" in request
    assert b"cookie:" not in request
    assert b"authorization:" not in request
    assert network["stream"].closed


@pytest.mark.parametrize("target", ["http://127.0.0.1/private", "https://other.example/", "/next"])
def test_public_preview_does_not_follow_any_redirect(network, target):
    network["stream"] = _Stream(status="302 Found", headers=f"Location: {target}\r\n".encode())
    assert http.get_public_page("https://forum.example/viewtopic.php?t=7").status_code == 302
    assert len(network["dns"]) == len(network["connect"]) == 1


@pytest.mark.parametrize(
    "url", ["file:///etc/passwd", "ftp://forum.example/", "https://user:pass@forum.example/", "https:///", "http://[x/"]
)
def test_invalid_preview_url_is_refused_before_dns(network, url):
    with pytest.raises((ValueError, httpx.InvalidURL)):
        http.get_public_page(url)
    assert network["dns"] == network["connect"] == []


def test_public_preview_keeps_bounded_body_and_closes_client(network, monkeypatch):
    monkeypatch.setattr(http, "MAX_HTML_RESPONSE_BYTES", 8)
    with pytest.raises(http.ResponseTooLargeError):
        http.get_public_page("https://forum.example/viewtopic.php?t=7")
    assert network["stream"].closed


def test_a_new_preview_rechecks_dns_and_refuses_rebinding(network):
    assert http.get_public_page("https://forum.example/viewtopic.php?t=7").status_code == 200
    network["addresses"] = ["127.0.0.1"]
    with pytest.raises(httpx.ConnectError, match="non-public"):
        http.get_public_page("https://forum.example/viewtopic.php?t=7")
    assert network["dns"] == [("forum.example", 443), ("forum.example", 443)]
    assert network["connect"] == [(PUBLIC_IPV4, 443)]


@pytest.mark.parametrize("route", ["/sites/guess", "/topics/guess-title"])
def test_web_preview_routes_cannot_contact_internal_hosts(network, monkeypatch, route):
    from fastapi.testclient import TestClient

    from tow.web import app

    monkeypatch.setattr("tow.config.load_config", lambda: {"allow_private_tracker_hosts": True})
    network["addresses"] = ["169.254.169.254"]
    response = TestClient(app, headers={"Origin": "http://127.0.0.1"}).post(
        route, data={"url": "https://forum.example/viewtopic.php?t=7"}
    )
    assert response.status_code == 200
    assert network["connect"] == []


def test_private_configured_title_cannot_replace_trusted_authority(monkeypatch):
    cfg = {
        "allow_private_tracker_hosts": True,
        "trackers": {
            "custom": {"url_regex": r"^http://forum\.example/.*\?t=(\d+)$", "fetch_hosts": ["http://forum.example"]}
        },
    }
    monkeypatch.setattr("tow.config.load_config", lambda: cfg)
    calls = []
    monkeypatch.setattr(
        http,
        "get_limited",
        lambda c, url, max_bytes: (
            calls.append(url)
            or httpx.Response(200, content=b"<html><h1>Show A</h1></html>", headers={"content-type": "text/html"})
        ),
    )
    assert title._title_from_page("http://forum.example//other.example/viewtopic.php?t=7") == "Show A"
    assert calls == ["http://forum.example//other.example/viewtopic.php?t=7"]
    assert title._title_from_page("http://other.example/viewtopic.php?t=7") == ""
    assert len(calls) == 1


@pytest.mark.parametrize("host", ["http://[::1]:8080", "https://[fd00::1]", "http://forum.example:8080"])
def test_explicit_configured_private_title_preserves_ipv6_and_port(monkeypatch, host):
    cfg = {
        "allow_private_tracker_hosts": True,
        "trackers": {"custom": {"url_regex": r"^https?://.+/viewtopic\.php\?t=(\d+)$", "fetch_hosts": [host]}},
    }
    monkeypatch.setattr("tow.config.load_config", lambda: cfg)
    calls = []
    monkeypatch.setattr(
        http,
        "get_limited",
        lambda c, url, max_bytes: (
            calls.append(url)
            or httpx.Response(200, content=b"<html><h1>Show A</h1></html>", headers={"content-type": "text/html"})
        ),
    )
    assert title._title_from_page(host + "/viewtopic.php?t=7") == "Show A"
    assert calls == [host + "/viewtopic.php?t=7"]


@pytest.mark.parametrize("option", ["public_only", "follow_redirects", "cookies", "transport", "client"])
def test_public_preview_does_not_accept_a_caller_policy_override(option, monkeypatch):
    monkeypatch.setattr(http, "client", lambda **kwargs: pytest.fail("an override reached client creation"))
    with pytest.raises(TypeError):
        http.get_public_page("https://forum.example/viewtopic.php?t=7", **{option: False})


def test_public_preview_owns_its_client_policy(monkeypatch):
    calls = []

    def client(**kwargs):
        calls.append(kwargs)
        return httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"page")))

    monkeypatch.setattr(http, "client", client)
    assert http.get_public_page("https://forum.example/viewtopic.php?t=7").content == b"page"
    assert calls == [{"ua": None, "public_only": True, "follow_redirects": False}]
