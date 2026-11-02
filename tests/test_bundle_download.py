"""The build's pinned uv GET recovers from outages without weakening integrity checks."""

from __future__ import annotations

import errno
import hashlib
import io
import socket
import ssl
import zipfile
from datetime import UTC, datetime
from email.utils import format_datetime
from http.client import IncompleteRead
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest
from test_bundle import bundle


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def time(self):
        return 1_700_000_000 + self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class Response:
    def __init__(self, body=b"complete", *, chunks=None, length=True, url=None):
        self.chunks = list(chunks if chunks is not None else [body])
        self.headers = {"Content-Length": str(len(body))} if length else {}
        self.url = url or bundle.UV_URL
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True

    def geturl(self):
        return self.url

    def read1(self, size):
        assert size > 0
        result = self.chunks.pop(0) if self.chunks else b""
        if isinstance(result, Exception):
            raise result
        return result


class HTTP:
    def __init__(self):
        self.steps = []
        self.requests = []

    def open(self, request, *, timeout):
        self.requests.append((request.full_url, request.get_method(), timeout))
        step = self.steps.pop(0)
        if callable(step):
            step = step()
        if isinstance(step, Exception):
            raise step
        return step


@pytest.fixture
def transport(monkeypatch):
    http, clock = HTTP(), Clock()
    monkeypatch.setattr(bundle, "build_opener", lambda *_handlers: http)
    monkeypatch.setattr(bundle, "time", clock)
    return http, clock


def http_error(code, headers=None):
    return HTTPError(bundle.UV_URL, code, "synthetic", headers or {}, io.BytesIO(b"error body"))


@pytest.mark.parametrize("code", [408, 500, 502, 503, 504])
def test_transient_http_restarts_the_same_pinned_get_and_closes_the_error(transport, code):
    http, clock = transport
    failure, response = http_error(code), Response()
    http.steps = [failure, response]
    assert bundle.download(bundle.UV_URL) == b"complete"
    assert http.requests == [(bundle.UV_URL, "GET", 60.0)] * 2
    assert failure.fp.closed
    assert response.closed
    assert clock.sleeps == [1]


def test_transient_failures_exhaust_exactly_four_attempts(transport):
    http, clock = transport
    failures = [http_error(504) for _ in range(4)]
    http.steps = failures.copy()
    with pytest.raises(HTTPError) as caught:
        bundle.download(bundle.UV_URL)
    assert caught.value is failures[-1]
    assert len(http.requests) == 4
    assert all(failure.fp.closed for failure in failures)
    assert clock.sleeps == [1, 2, 4]


@pytest.mark.parametrize("code", [400, 401, 403, 404, 407, 501])
def test_other_http_failures_are_not_retried(transport, code):
    http, clock = transport
    failure = http_error(code, {"Retry-After": "1"})
    http.steps = [failure]
    with pytest.raises(HTTPError) as caught:
        bundle.download(bundle.UV_URL)
    assert caught.value is failure
    assert len(http.requests) == 1
    assert failure.fp.closed
    assert clock.sleeps == []


@pytest.mark.parametrize("failure", [ConnectionResetError("reset"), IncompleteRead(b"part", 10)])
def test_a_read_failure_discards_all_partial_bytes_before_the_retry(transport, failure):
    http, clock = transport
    partial = Response(chunks=[b"discard me", failure], length=False)
    complete = Response()
    http.steps = [partial, complete]
    assert bundle.download(bundle.UV_URL) == b"complete"
    assert partial.closed
    assert complete.closed
    assert len(http.requests) == 2
    assert clock.sleeps == [1]


def test_an_early_eof_is_not_accepted_as_a_complete_response(transport):
    http, _clock = transport
    partial = Response(body=b"expected length", chunks=[b"short"])
    http.steps = [partial, Response()]
    assert bundle.download(bundle.UV_URL) == b"complete"
    assert partial.closed
    assert len(http.requests) == 2


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError("timeout"),
        URLError(ConnectionResetError("reset")),
        URLError(socket.gaierror(socket.EAI_AGAIN, "temporary DNS failure")),
    ],
)
def test_transient_connection_failures_are_retried(transport, failure):
    http, _clock = transport
    http.steps = [failure, Response()]
    assert bundle.download(bundle.UV_URL) == b"complete"
    assert len(http.requests) == 2


@pytest.mark.parametrize(
    "failure",
    [
        URLError(ssl.SSLCertVerificationError(1, "certificate verification failed")),
        URLError(socket.gaierror(socket.EAI_NONAME, "unknown host")),
        URLError("invalid proxy configuration"),
        PermissionError(errno.EACCES, "access denied"),
        ssl.SSLError(1, "unsupported TLS protocol"),
    ],
)
def test_certificate_configuration_and_permanent_transport_errors_are_not_retried(transport, failure):
    http, clock = transport
    http.steps = [failure]
    with pytest.raises(type(failure)):
        bundle.download(bundle.UV_URL)
    assert len(http.requests) == 1
    assert clock.sleeps == []


@pytest.mark.parametrize("value", ["5", format_datetime(datetime.fromtimestamp(1_700_000_005, UTC), usegmt=True)])
def test_retry_after_seconds_and_http_dates_are_minimum_delays(transport, value):
    http, clock = transport
    http.steps = [http_error(503, {"Retry-After": value}), Response()]
    assert bundle.download(bundle.UV_URL) == b"complete"
    assert clock.sleeps == [5]


@pytest.mark.parametrize("value", ["invalid", "-1", "0", "Thu, 01 Jan 1970 00:00:00 GMT"])
def test_invalid_or_past_retry_after_keeps_the_backoff(transport, value):
    http, clock = transport
    http.steps = [http_error(503, {"Retry-After": value}), Response()]
    assert bundle.download(bundle.UV_URL) == b"complete"
    assert clock.sleeps == [1]


@pytest.mark.parametrize("value", ["5", format_datetime(datetime.fromtimestamp(1_700_000_005, UTC), usegmt=True)])
def test_github_rate_limit_retries_only_after_the_servers_valid_pause(transport, value):
    http, clock = transport
    failure = http_error(429, {"Retry-After": value})
    http.steps = [failure, Response()]
    assert bundle.download(bundle.UV_URL) == b"complete"
    assert clock.sleeps == [5]
    assert http.requests == [(bundle.UV_URL, "GET", 60.0)] * 2
    assert failure.fp.closed


@pytest.mark.parametrize("value", [None, "", "invalid", "-1", "1.5", "Wed, 15 Nov 2023 00:00:00"])
def test_a_rate_limit_without_a_valid_server_pause_fails_once(transport, value):
    http, clock = transport
    failure = http_error(429, {"Retry-After": value} if value is not None else {})
    http.steps = [failure]
    with pytest.raises(HTTPError) as caught:
        bundle.download(bundle.UV_URL)
    assert caught.value is failure
    assert failure.fp.closed
    assert len(http.requests) == 1
    assert clock.sleeps == []


@pytest.mark.parametrize("code", [429, 503])
@pytest.mark.parametrize("value", ["300", "9999999999999999999999999999999999999999999"])
def test_retry_after_that_cannot_fit_the_budget_is_not_shortened(transport, code, value):
    http, clock = transport
    http.steps = [http_error(code, {"Retry-After": value})]
    with pytest.raises(HTTPError):
        bundle.download(bundle.UV_URL)
    assert len(http.requests) == 1
    assert clock.sleeps == []


def test_a_slow_failure_cannot_start_another_attempt_after_the_budget(transport):
    http, clock = transport

    def expired():
        clock.now = 300
        return http_error(504)

    http.steps = [expired]
    with pytest.raises(HTTPError):
        bundle.download(bundle.UV_URL)
    assert len(http.requests) == 1
    assert clock.sleeps == []


def test_the_next_timeout_uses_only_the_remaining_budget(transport, monkeypatch):
    http, clock = transport
    monkeypatch.setattr(bundle, "DOWNLOAD_BUDGET", 2)
    http.steps = [http_error(504), Response()]
    assert bundle.download(bundle.UV_URL) == b"complete"
    assert [timeout for _url, _method, timeout in http.requests] == [2, 1]
    assert clock.sleeps == [1]


def test_a_dribbling_body_cannot_finish_or_start_a_retry_after_the_budget(transport):
    http, clock = transport

    class SlowResponse(Response):
        def read1(self, size):
            result = super().read1(size)
            clock.now += 301
            return result

    response = SlowResponse()
    http.steps = [response]
    with pytest.raises(TimeoutError, match="time budget exhausted"):
        bundle.download(bundle.UV_URL)
    assert response.closed
    assert len(http.requests) == 1
    assert clock.sleeps == []


@pytest.mark.parametrize("length", [True, False])
def test_oversized_downloads_fail_without_retry(transport, length):
    http, clock = transport
    response = Response(body=b"too large", length=length)
    http.steps = [response]
    with pytest.raises(ValueError, match="size limit"):
        bundle.download(bundle.UV_URL, max_bytes=3)
    assert response.closed
    assert len(http.requests) == 1
    assert clock.sleeps == []


@pytest.mark.parametrize(
    "target",
    ["http://github.com/x", "https://example.test/x", "https://github.com:444/x", "https://user@github.com/x"],
)
def test_redirects_cannot_leave_github_https(target):
    with pytest.raises(ValueError, match="outside GitHub HTTPS"):
        bundle._GitHubDownload().redirect_request(Request(bundle.UV_URL), None, 302, "redirect", {}, target)


def test_official_asset_redirects_keep_get_and_never_change_the_initial_publisher(transport):
    http, clock = transport
    target = "https://release-assets.githubusercontent.com/github-production-release-asset/synthetic?token=private"
    request = bundle._GitHubDownload().redirect_request(Request(bundle.UV_URL), None, 302, "redirect", {}, target)
    assert request.full_url == target
    assert request.get_method() == "GET"
    with pytest.raises(ValueError, match="pinned uv release"):
        bundle.download("https://github.com/another/publisher/releases/download/v1/uv.zip")
    assert http.requests == []
    assert clock.sleeps == []


def test_retry_messages_do_not_expose_signed_redirects_or_exception_details(transport, capsys):
    http, _clock = transport
    signed = "https://release-assets.githubusercontent.com/synthetic?token=synthetic-private"
    partial = Response(chunks=[ConnectionResetError("synthetic-private")], length=False, url=signed)
    http.steps = [partial, Response()]
    assert bundle.download(bundle.UV_URL) == b"complete"
    message = capsys.readouterr().out
    assert "ConnectionResetError" in message
    assert "synthetic-private" not in message
    assert "release-assets" not in message


@pytest.mark.parametrize("checksum", [b"", b"invalid", b"0" * 64])
def test_checksum_failures_never_retry_or_install_the_executable(transport, tmp_path, checksum):
    http, clock = transport
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("uv.exe", b"synthetic executable")
    data = payload.getvalue()
    assert checksum != hashlib.sha256(data).hexdigest().encode()
    http.steps = [Response(data), Response(checksum)]
    target = tmp_path / "bin"
    with pytest.raises(SystemExit, match=r"\.sha256"):
        bundle.fetch_uv(target)
    assert [url for url, _method, _timeout in http.requests] == [bundle.UV_URL, bundle.UV_URL + ".sha256"]
    assert clock.sleeps == []
    assert not target.exists()


def test_checksum_get_can_recover_before_the_verified_executable_is_installed(transport, tmp_path):
    http, clock = transport
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("uv.exe", b"synthetic executable")
    data = payload.getvalue()
    digest = hashlib.sha256(data).hexdigest().encode() + b"  uv.zip\n"
    http.steps = [Response(data), http_error(504), Response(digest)]
    executable = bundle.fetch_uv(tmp_path / "bin")
    assert executable.read_bytes() == b"synthetic executable"
    assert [url for url, _method, _timeout in http.requests] == [bundle.UV_URL, *[bundle.UV_URL + ".sha256"] * 2]
    assert clock.sleeps == [1]
