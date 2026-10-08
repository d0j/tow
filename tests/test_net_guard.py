"""Outbound tracker and notifier sockets use a checked numeric DNS answer."""

from __future__ import annotations

import socket

import httpcore
import httpx
import pytest

from tow.net_guard import PublicOnlyBackend, PublicOnlyTransport, is_public, public_addresses


def _answers(*addresses: str):
    return [
        (socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))
        for address in addresses
    ]


def test_public_only_backend_connects_to_a_checked_numeric_ip(monkeypatch):
    looked_up = []
    connected = []
    stream = object()

    def resolve(host, port, **_kwargs):
        looked_up.append((host, port))
        return _answers("93.184.216.34")

    def connect(_self, host, port, *_args):
        connected.append((host, port))
        return stream

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", connect)

    assert PublicOnlyBackend().connect_tcp("tracker.example", 443) is stream
    assert looked_up == [("tracker.example", 443)]
    assert connected == [("93.184.216.34", 443)]


@pytest.mark.parametrize("failure", [httpcore.ConnectError, httpcore.ConnectTimeout])
def test_pinned_connect_tries_each_checked_address_in_turn(monkeypatch, failure):
    connected = []
    stream = object()

    def connect(_self, host, port, *_args):
        connected.append(host)
        if host == "93.184.216.34":
            raise failure("unreachable")
        return stream

    monkeypatch.setattr(socket, "getaddrinfo", lambda *_args, **_kwargs: _answers("93.184.216.34", "2606:4700::1111"))
    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", connect)

    assert PublicOnlyBackend().connect_tcp("tracker.example", 443) is stream
    assert connected == ["93.184.216.34", "2606:4700::1111"]  # numbers only, never the name again


def test_pinned_connect_reports_the_last_failure_when_no_address_answers(monkeypatch):
    connected = []

    def connect(_self, host, port, *_args):
        connected.append(host)
        raise httpcore.ConnectError(f"refused by {host}")

    monkeypatch.setattr(socket, "getaddrinfo", lambda *_args, **_kwargs: _answers("93.184.216.34", "2606:4700::1111"))
    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", connect)

    with pytest.raises(httpcore.ConnectError, match="refused by 2606:4700::1111"):
        PublicOnlyBackend().connect_tcp("tracker.example", 443)
    assert connected == ["93.184.216.34", "2606:4700::1111"]


def test_pinned_connect_never_opens_a_socket_for_a_refused_answer(monkeypatch):
    def connect(*_args, **_kwargs):
        raise AssertionError("a socket was opened to a checked-out address")

    monkeypatch.setattr(socket, "getaddrinfo", lambda *_args, **_kwargs: _answers("93.184.216.34", "192.168.1.20"))
    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", connect)

    with pytest.raises(httpcore.ConnectError, match="non-public"):
        PublicOnlyBackend().connect_tcp("tracker.example", 443)


def test_an_answer_that_is_not_an_address_fails_closed(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_args, **_kwargs: _answers("not-an-address"))
    with pytest.raises(httpcore.ConnectError, match="invalid address"):
        public_addresses("tracker.example", 443)


def test_the_transport_refuses_to_run_without_the_checked_backend(monkeypatch):
    closed = []
    monkeypatch.setattr(httpx.HTTPTransport, "__init__", lambda self, **_kwargs: setattr(self, "_pool", object()))
    monkeypatch.setattr(httpx.HTTPTransport, "close", lambda self: closed.append(self))

    with pytest.raises(TypeError, match="public-address policy"):
        PublicOnlyTransport()
    assert len(closed) == 1


@pytest.mark.parametrize(
    ("url", "answers", "allowed"),
    [
        ("https://tracker.example/announce", ("93.184.216.34",), True),
        ("udp://tracker.example:6969/announce", ("93.184.216.34",), True),
        ("http://tracker.example/announce", ("192.168.1.20",), False),
        ("ftp://tracker.example/announce", ("93.184.216.34",), False),
        ("https:///announce", ("93.184.216.34",), False),
        ("https://tracker.example:99999/announce", ("93.184.216.34",), False),
    ],
)
def test_tracker_addresses_follow_the_same_public_only_rule(monkeypatch, url, answers, allowed):
    from tow.net_guard import tracker_address

    monkeypatch.setattr(socket, "getaddrinfo", lambda *_args, **_kwargs: _answers(*answers))
    assert tracker_address(url) is allowed
    if not allowed and answers == ("192.168.1.20",):
        assert tracker_address(url, public_only=False) is True  # a LAN tracker only when asked for


@pytest.mark.parametrize(
    "addresses",
    [(), ("127.0.0.1",), ("93.184.216.34", "10.0.0.2"), ("::1", "93.184.216.34")],
)
def test_missing_private_and_mixed_answers_fail_closed(monkeypatch, addresses):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_args, **_kwargs: _answers(*addresses))
    with pytest.raises(httpcore.ConnectError):
        public_addresses("tracker.example", 443)


@pytest.mark.parametrize(
    "address",
    [
        "::127.0.0.1",
        "::192.168.1.20",
        "::ffff:10.0.0.2",
        "64:ff9b::127.0.0.1",
        "64:ff9b::c0a8:114",
        "64:ff9b::100.64.0.1",
        "::ffff:0:c0a8:101",  # IPv4-translated 192.168.1.1 (audit 08.10.2026)
        "::ffff:0:127.0.0.1",
        "::ffff:0:a00:2",
    ],
)
def test_ipv6_forms_of_a_private_ipv4_address_fail_closed(monkeypatch, address):
    # IPv4-compatible, IPv4-translated and NAT64 addresses count as "global" for ipaddress; their
    # IPv4 part is not.
    assert is_public(address) is False
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_args, **_kwargs: _answers(address))
    with pytest.raises(httpcore.ConnectError):
        public_addresses("tracker.example", 443)


@pytest.mark.parametrize(
    "address", ["93.184.216.34", "64:ff9b::93.184.216.34", "::ffff:0:93.184.216.34", "2606:4700::1111"]
)
def test_public_addresses_stay_public_in_every_form(address):
    assert is_public(address) is True


@pytest.mark.parametrize(
    "address", ["224.0.0.1", "224.0.1.1", "239.255.255.250", "ff02::1", "ff0e::1", "::ffff:224.0.1.1"]
)
def test_multicast_groups_are_not_public_addresses(monkeypatch, address):
    # Audit 08.10.2026: ipaddress counts most multicast groups as global; a site name answering
    # with one would have sent requests (and a tracker announce) to the local network.
    assert is_public(address) is False
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_args, **_kwargs: _answers(address))
    with pytest.raises(httpcore.ConnectError):
        public_addresses("tracker.example", 443)


def test_a_site_address_in_an_ipv6_form_of_the_home_network_is_internal():
    from tow.web.site_form import internal_host

    assert internal_host("[::192.168.1.1]") is True
    assert internal_host("[64:ff9b::7f00:1]") is True


def test_dns_error_fails_closed(monkeypatch):
    def broken(*_args, **_kwargs):
        raise socket.gaierror("DNS unavailable")

    monkeypatch.setattr(socket, "getaddrinfo", broken)
    with pytest.raises(httpcore.ConnectError, match="resolved safely"):
        public_addresses("tracker.example", 443)


def test_http_transport_fails_closed_if_backend_cannot_be_installed():
    with PublicOnlyTransport() as transport:
        assert isinstance(transport._pool._network_backend, PublicOnlyBackend)


@pytest.mark.allow_system
def test_http_request_reaches_the_public_only_backend(monkeypatch):
    called = []

    def no_socket(*_args, **_kwargs):
        raise AssertionError("a real socket was attempted")

    monkeypatch.setattr(socket.socket, "connect", no_socket)

    def refuse(_self, host, port, *_args, **_kwargs):
        called.append((host, port))
        raise httpcore.ConnectError("checked backend reached")

    monkeypatch.setattr(PublicOnlyBackend, "connect_tcp", refuse)
    with (
        httpx.Client(transport=PublicOnlyTransport(), trust_env=False) as client,
        pytest.raises(httpx.ConnectError, match="checked backend reached"),
    ):
        client.get("https://tracker.example/topic")
    assert called == [("tracker.example", 443)]


def test_tracker_and_notifier_clients_use_the_pinned_transport_by_default(monkeypatch):
    from tow.http import client as tracker_client
    from tow.notifiers.base import http_client as notifier_client

    monkeypatch.setattr("tow.config.load_config", dict)
    with tracker_client(public_only=True) as tracker, notifier_client() as notifier:
        assert isinstance(tracker._transport, PublicOnlyTransport)
        assert isinstance(notifier._transport, PublicOnlyTransport)


@pytest.mark.parametrize("address", ["fe80::1%eth0", "fe80::1%12", "::1%lo"])
def test_a_zoned_link_local_or_loopback_address_is_not_public(address):
    assert is_public(address) is False


def test_a_zoned_public_address_is_judged_without_its_zone():
    assert is_public("2606:4700::1111%eth0") is True


@pytest.mark.parametrize("address", ["64:ff9b::c0a8:0101", "64:ff9b::1", "::ffff:0:0.0.0.1", "64:ff9b::7f00:1"])
def test_nat64_forms_are_judged_by_every_bit_of_their_ipv4_part(address):
    # 64:ff9b::1 is 0.0.0.1 behind a NAT64 gateway: the last bit decides it is not 64:ff9b::0.
    assert is_public(address) is False
