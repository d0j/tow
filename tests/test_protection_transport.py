"""Recovery deadlines preserve public DNS pinning and cap every socket phase."""

from __future__ import annotations

import time

import httpcore
import httpx
import pytest

from tow import http as thttp
from tow import net_guard
from tow.protection import transport


class Stream(httpcore.NetworkStream):
    def __init__(self):
        self.calls = []
        self.closed = False

    def read(self, max_bytes, timeout=None):
        self.calls.append(("read", timeout))
        return b"answer"

    def write(self, buffer, timeout=None):
        self.calls.append(("write", timeout))

    def close(self):
        self.closed = True

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self.calls.append(("tls", timeout, server_hostname))
        return self

    def get_extra_info(self, info):
        return info


@pytest.fixture
def clock(monkeypatch):
    current = [10.0]
    monkeypatch.setattr(time, "monotonic", lambda: current[0])
    return current


def test_every_stream_phase_consumes_the_same_deadline(clock):
    raw = Stream()
    stream = transport.DeadlineStream(raw, 20)
    assert stream.read(32, 50) == b"answer"
    clock[0] = 12
    stream.write(b"request", 3)
    clock[0] = 15
    secure = stream.start_tls(None, "tracker.example", 30)
    assert isinstance(secure, transport.DeadlineStream)
    clock[0] = 19
    secure.read(32, 50)
    assert raw.calls == [("read", 10), ("write", 3), ("tls", 5, "tracker.example"), ("read", 1)]
    assert secure.get_extra_info("ssl") == "ssl"
    clock[0] = 20
    with pytest.raises(httpcore.ReadTimeout):
        secure.read(32)
    assert len(raw.calls) == 4
    secure.close()
    assert raw.closed


def test_delegate_public_backend_is_preserved_and_connects_to_checked_ip(clock, monkeypatch):
    raw = Stream()
    connected = []
    monkeypatch.setattr(net_guard, "public_addresses", lambda host, port: ["93.184.216.34"])

    def connect(self, host, port, timeout=None, local_address=None, socket_options=None):
        connected.append((host, port, timeout, local_address, socket_options))
        return raw

    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", connect)
    delegate = net_guard.PublicOnlyBackend()
    backend = transport.DeadlineBackend(delegate, 20)
    stream = backend.connect_tcp("tracker.example", 443, 30)
    assert backend.wrapped is delegate
    assert isinstance(stream, transport.DeadlineStream)
    assert connected == [("93.184.216.34", 443, 10, None, None)]


def test_delegate_non_public_dns_is_refused_before_connect(clock, monkeypatch):
    monkeypatch.setattr(net_guard.socket, "getaddrinfo", lambda *a, **kw: [(2, 1, 6, "", ("127.0.0.1", 443))])
    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", lambda *a, **kw: pytest.fail("unsafe connect"))
    with pytest.raises(httpcore.ConnectError, match="non-public"):
        transport.DeadlineBackend(net_guard.PublicOnlyBackend(), 20).connect_tcp("tracker.example", 443)


def test_late_dns_or_connection_result_is_closed_and_refused(clock, monkeypatch):
    raw = Stream()

    def connect(self, *args, **kwargs):
        clock[0] = 21
        return raw

    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", connect)
    with pytest.raises(httpcore.ReadTimeout):
        transport.DeadlineBackend(httpcore.SyncBackend(), 20).connect_tcp("tracker.example", 443)
    assert raw.closed


def test_late_tls_result_is_closed_and_refused(clock, monkeypatch):
    raw = Stream()

    def tls(*args, **kwargs):
        clock[0] = 21
        return raw

    monkeypatch.setattr(raw, "start_tls", tls)
    with pytest.raises(httpcore.ReadTimeout):
        transport.DeadlineStream(raw, 20).start_tls(None, "tracker.example")
    assert raw.closed


def test_wrapper_restores_existing_policy_and_timeouts(clock):
    with thttp.client(public_only=True, follow_redirects=False) as client:
        pool = client._transport._pool
        original = pool._network_backend
        timeout = client.timeout
        with transport.with_deadline(client, 20):
            assert isinstance(pool._network_backend, transport.DeadlineBackend)
            assert pool._network_backend.wrapped is original
            assert client.timeout.read == 10
        assert pool._network_backend is original
        # Every tracker client holds each request to its own deadline (tow.http) over the
        # public-only backend; the recovery's deadline goes on top of both.
        assert isinstance(original, thttp.RequestDeadlineBackend)
        assert isinstance(original.wrapped, net_guard.PublicOnlyBackend)
        assert client.timeout == timeout


def test_existing_connections_and_mounted_transports_fail_closed(clock):
    with thttp.client(public_only=True) as client:
        pool = client._transport._pool
        pool._connections.append(object())
        try:
            with pytest.raises(TypeError, match="fresh"), transport.with_deadline(client, 20):
                pytest.fail("reused connection allowed")
        finally:
            pool._connections.clear()
    mounted = httpx.MockTransport(lambda request: pytest.fail("mounted request escaped"))
    with (
        httpx.Client(mounts={"https://": mounted}, trust_env=False) as client,
        pytest.raises(TypeError, match="mounted"),
        transport.with_deadline(client, 20),
    ):
        pytest.fail("mounted transport allowed")


def test_arbitrary_transports_are_refused(clock):
    class Unsafe(httpx.BaseTransport):
        def handle_request(self, request):
            pytest.fail("arbitrary transport reached")

    with (
        httpx.Client(transport=Unsafe(), trust_env=False) as client,
        pytest.raises(TypeError, match="cannot enforce"),
        transport.with_deadline(client, 20),
    ):
        pytest.fail("unknown transport allowed")


@pytest.mark.parametrize("deadline", [True, float("nan"), float("inf"), float("-inf")])
def test_invalid_deadline_refused_before_request(clock, deadline):
    with (
        httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail("network called"))) as client,
        pytest.raises(ValueError, match="invalid request deadline"),
    ):
        transport.bounded_get(client, "https://tracker.example/", deadline=deadline, max_bytes=100)


def test_expired_deadline_refused_before_request(clock):
    with (
        httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail("network called"))) as client,
        pytest.raises(httpcore.ReadTimeout),
    ):
        transport.bounded_get(client, "https://tracker.example/", deadline=10, max_bytes=100)


def test_mock_get_uses_remaining_timeout_and_preserves_limits(clock):
    def answer(request):
        assert request.extensions["timeout"] == {"connect": 10, "read": 10, "write": 10, "pool": 10}
        return httpx.Response(200, content=b"answer")

    with httpx.Client(transport=httpx.MockTransport(answer), trust_env=False, timeout=50) as client:
        assert transport.bounded_get(client, "https://tracker.example/", deadline=20, max_bytes=6).content == b"answer"
        with pytest.raises(thttp.ResponseTooLargeError):
            transport.bounded_get(client, "https://tracker.example/", deadline=20, max_bytes=5)


def test_late_headers_are_not_replayed_or_accepted(clock):
    def answer(request):
        clock[0] = 21
        return httpx.Response(200, content=b"")

    with (
        httpx.Client(transport=httpx.MockTransport(answer), trust_env=False) as client,
        pytest.raises(thttp.ResponseTooSlowError),
    ):
        transport.bounded_get(client, "https://tracker.example/", deadline=20, max_bytes=10)


def test_late_body_is_not_replayed_or_accepted(clock):
    class Slow(httpx.SyncByteStream):
        def __iter__(self):
            yield b"first"
            clock[0] = 21
            yield b"late"

    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=Slow())), trust_env=False
        ) as client,
        pytest.raises(thttp.ResponseTooSlowError),
    ):
        transport.bounded_get(client, "https://tracker.example/", deadline=20, max_bytes=100)
