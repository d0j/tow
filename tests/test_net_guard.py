"""Outbound tracker and notifier sockets use a checked numeric DNS answer."""

from __future__ import annotations

import socket

import httpcore
import httpx
import pytest

from tow.net_guard import PublicOnlyBackend, PublicOnlyTransport, public_addresses


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


@pytest.mark.parametrize(
    "addresses",
    [(), ("127.0.0.1",), ("93.184.216.34", "10.0.0.2"), ("::1", "93.184.216.34")],
)
def test_missing_private_and_mixed_answers_fail_closed(monkeypatch, addresses):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_args, **_kwargs: _answers(*addresses))
    with pytest.raises(httpcore.ConnectError):
        public_addresses("tracker.example", 443)


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
