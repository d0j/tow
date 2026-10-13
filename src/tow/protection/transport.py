"""An absolute request deadline layered over the client's existing network policy.

The delegate remains responsible for address pinning and TLS policy. Socket operations,
headers, bodies and TLS share one deadline. An OS DNS lookup is synchronous and cannot
be interrupted here; its result is refused if it returns after the deadline.
"""

from __future__ import annotations

import math
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import Any

import httpcore
import httpx

from tow import http as thttp


def remaining(deadline: float, timeout: float | None = None) -> float:
    """Time left, never an extension of the caller's or delegate's timeout."""
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline):
        raise ValueError("invalid request deadline")
    left = deadline - time.monotonic()
    if left <= 0:
        raise httpcore.ReadTimeout("tracker recovery time budget exhausted")
    return left if timeout is None else min(left, timeout)


class DeadlineStream(httpcore.NetworkStream):
    """A delegate's stream with the remaining budget applied to every operation."""

    def __init__(self, wrapped: httpcore.NetworkStream, deadline: float) -> None:
        self.wrapped = wrapped
        self.deadline = deadline

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        value = self.wrapped.read(max_bytes, timeout=remaining(self.deadline, timeout))
        remaining(self.deadline)
        return value

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self.wrapped.write(buffer, timeout=remaining(self.deadline, timeout))
        remaining(self.deadline)

    def close(self) -> None:
        self.wrapped.close()

    def start_tls(
        self, ssl_context: Any, server_hostname: str | None = None, timeout: float | None = None
    ) -> httpcore.NetworkStream:
        wrapped = self.wrapped.start_tls(ssl_context, server_hostname, timeout=remaining(self.deadline, timeout))
        try:
            remaining(self.deadline)
        except httpcore.ReadTimeout:
            wrapped.close()
            raise
        return DeadlineStream(wrapped, self.deadline)

    def get_extra_info(self, info: str) -> Any:
        return self.wrapped.get_extra_info(info)


class DeadlineBackend(httpcore.SyncBackend):
    """Delegate connection creation rather than replace its DNS/address restrictions."""

    def __init__(self, wrapped: httpcore.NetworkBackend, deadline: float) -> None:
        self.wrapped = wrapped
        self.deadline = deadline

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.NetworkStream:
        stream = self.wrapped.connect_tcp(host, port, remaining(self.deadline, timeout), local_address, socket_options)
        try:
            remaining(self.deadline)
        except httpcore.ReadTimeout:
            stream.close()
            raise
        return DeadlineStream(stream, self.deadline)

    def connect_unix_socket(
        self, path: str, timeout: float | None = None, socket_options: Iterable[Any] | None = None
    ) -> httpcore.NetworkStream:
        raise httpcore.ConnectError("tracker recovery does not use Unix sockets")


@contextmanager
def with_deadline(client: httpx.Client, deadline: float) -> Iterator[httpx.Client]:
    """A fresh client only: an existing connection could bypass backend wrapping.

    Callers own the client and must not share it concurrently. MockTransport is supported
    for isolated tests; arbitrary transports fail closed. Created connections are closed
    before restoring the backend so none keep a deadline from an earlier request.
    """
    budget = remaining(deadline)
    transport = getattr(client, "_transport", None)
    if any(value is not None for value in getattr(client, "_mounts", {}).values()):
        raise TypeError("tracker deadline cannot cover mounted transports")
    old_timeout = client.timeout
    bounded_timeout = httpx.Timeout(
        **{name: budget if value is None else min(budget, value) for name, value in old_timeout.as_dict().items()}
    )
    if isinstance(transport, httpx.MockTransport):
        client.timeout = bounded_timeout
        try:
            yield client
        finally:
            client.timeout = old_timeout
        return
    if not isinstance(transport, httpx.HTTPTransport):
        raise TypeError("tracker transport cannot enforce deadline")
    pool = getattr(transport, "_pool", None)
    if not isinstance(pool, httpcore.ConnectionPool) or pool.connections:
        raise TypeError("tracker deadline requires a fresh connection pool")
    backend = pool._network_backend
    pool._network_backend = DeadlineBackend(backend, deadline)
    client.timeout = bounded_timeout
    try:
        yield client
    finally:
        try:
            pool.close()
        finally:
            pool._network_backend = backend
            client.timeout = old_timeout


def bounded_get(client: httpx.Client, url: str, *, deadline: float, max_bytes: int) -> httpx.Response:
    """A bounded tracker GET without changing its public-address or redirect policy."""
    with with_deadline(client, deadline):
        return thttp.get_limited(client, url, max_bytes=max_bytes, deadline=deadline)
