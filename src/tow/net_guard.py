"""Pin outbound public-only HTTP connections to a checked DNS answer."""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit

import httpcore
import httpx


def public_addresses(host: str, port: int) -> list[str]:
    """All DNS answers must be global; an absent or mixed answer fails closed."""
    try:
        answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError, ValueError) as exc:
        raise httpcore.ConnectError("the host could not be resolved safely") from exc
    addresses = list(dict.fromkeys(str(answer[4][0]) for answer in answers))
    if not addresses:
        raise httpcore.ConnectError("the host has no usable address")
    try:
        if any(not ipaddress.ip_address(value.split("%", 1)[0]).is_global for value in addresses):
            raise httpcore.ConnectError("the host resolves to a non-public address")
    except ValueError as exc:
        raise httpcore.ConnectError("the host has an invalid address") from exc
    return addresses


_TRACKER_SCHEMES = {"http": 80, "https": 443, "udp": 80}


def tracker_address(url: str, *, public_only: bool = True) -> bool:
    """An http(s) or udp announce address a client may be told about. With ``public_only``
    its host must resolve to public addresses only (as for site requests); an unresolvable
    host or a LAN, loopback or link-local one is refused."""
    try:
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()
        if scheme not in _TRACKER_SCHEMES or not parsed.hostname:
            return False
        port = parsed.port or _TRACKER_SCHEMES[scheme]
        if public_only:
            public_addresses(parsed.hostname, port)
    except ValueError, httpcore.ConnectError:
        return False
    return True


class PublicOnlyBackend(httpcore.SyncBackend):
    """Resolve once, check every answer, then connect to a numeric address.

    The pool keeps the original hostname for HTTP Host and TLS SNI. Connecting to the
    numeric address means a second DNS lookup cannot redirect the socket to a LAN host.
    """

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.NetworkStream:
        last_error: httpcore.ConnectError | httpcore.ConnectTimeout | None = None
        for address in public_addresses(host, port):
            try:
                return super().connect_tcp(address, port, timeout, local_address, socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc
        assert last_error is not None
        raise last_error


class PublicOnlyTransport(httpx.HTTPTransport):
    """The locked HTTPX version's pool with a checked, IP-pinning network backend."""

    def __init__(self) -> None:
        super().__init__(trust_env=False)
        pool = getattr(self, "_pool", None)
        if not isinstance(pool, httpcore.ConnectionPool):
            self.close()
            raise TypeError("the HTTP transport cannot enforce the public-address policy")
        pool._network_backend = PublicOnlyBackend()
