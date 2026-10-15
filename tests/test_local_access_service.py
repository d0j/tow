"""Demand-only service lifecycle with fake processes, HTTP, sockets and a private module."""

from __future__ import annotations

import hashlib
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tow.platform import _local_service_worker as worker
from tow.protection import local_service

PROVIDER = {"kind": "flaresolverr", "url": "http://127.0.0.1:8191/v1", "enabled": True}
PAYLOAD = {
    "cmd": "request.get",
    "url": "https://site.example/login.php",
    "maxTimeout": 60000,
    "returnOnlyCookies": True,
}


@pytest.fixture
def installed(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    folder = runtime / "services" / "flaresolverr"
    (folder / "bin").mkdir(parents=True)
    binary = folder / "bin" / "flaresolverr.exe"
    binary.write_bytes(b"synthetic-service")
    manifest = {
        "format": "tow-local-service/v1",
        "kind": "flaresolverr",
        "url": PROVIDER["url"],
        "executable": "bin/flaresolverr.exe",
        "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "version": "3.5.2",
        "idle_sec": 300,
    }
    (folder / "manifest.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(local_service, "runtime_dir", lambda: runtime)
    monkeypatch.setattr(local_service, "current", lambda: SimpleNamespace(name="windows"))
    return folder, manifest


def test_absent_module_never_starts_or_contacts_anything(tmp_path, monkeypatch):
    monkeypatch.setattr(local_service, "runtime_dir", lambda: tmp_path / "absent")
    monkeypatch.setattr(local_service, "current", lambda: SimpleNamespace(name="windows"))
    monkeypatch.setattr(local_service, "listening", lambda *a: pytest.fail("network used"))
    assert local_service.ensure(PROVIDER, 0) is False


@pytest.mark.parametrize("provider", [dict(PROVIDER, enabled=False), dict(PROVIDER, kind="byparr")])
def test_disabled_or_external_module_never_launches(installed, monkeypatch, provider):
    monkeypatch.setattr(local_service, "listening", lambda *a: pytest.fail("network used"))
    assert local_service.ensure(provider, 0) is False


def test_different_service_address_and_platform_are_not_managed(installed, monkeypatch):
    assert local_service.managed(dict(PROVIDER, url="http://192.168.1.2:8191/v1")) is False
    monkeypatch.setattr(local_service, "current", lambda: SimpleNamespace(name="linux"))
    assert local_service.managed(PROVIDER) is False


def test_existing_listener_is_reused_without_process_ownership(installed, monkeypatch):
    monkeypatch.setattr(local_service, "listening", lambda *a: True)
    assert local_service.ensure(PROVIDER, local_service.time.monotonic() + 1) is True
    assert not (installed[0] / "worker.py").exists()


def test_cold_launch_has_one_copied_worker_and_no_inherited_secrets(installed, monkeypatch):
    calls = []
    responses = iter([False, False, True])
    monkeypatch.setattr(local_service, "listening", lambda *a: next(responses))
    monkeypatch.setattr(
        local_service,
        "current",
        lambda: SimpleNamespace(name="windows", spawn_detached=lambda *a, **k: calls.append((a, k))),
    )
    monkeypatch.setenv("TOW_MASTER_KEY_FILE", "private")
    monkeypatch.setenv("GH_TOKEN", "private")
    monkeypatch.setenv("HTTPS_PROXY", "private")
    assert local_service.ensure(PROVIDER, local_service.time.monotonic() + 1) is True
    assert len(calls) == 1
    argv, options = calls[0]
    assert argv[0][-1] == str(installed[0] / "worker.py")
    assert argv[0][1] == "-I"
    assert options["hidden"] is True
    assert not {"TOW_MASTER_KEY_FILE", "GH_TOKEN", "HTTPS_PROXY"} & options["env"].keys()
    assert (installed[0] / "worker.py").read_bytes().startswith(b'"""Standalone')


def test_launch_and_readiness_share_the_callers_deadline(installed, monkeypatch):
    clock = [0.0]
    calls = []
    monkeypatch.setattr(local_service.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(local_service.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr(local_service, "listening", lambda *a: False)
    monkeypatch.setattr(
        local_service,
        "current",
        lambda: SimpleNamespace(name="windows", spawn_detached=lambda *a, **k: calls.append(a)),
    )
    with pytest.raises(TimeoutError, match="become ready"):
        local_service.ensure(PROVIDER, 0.1)
    assert len(calls) == 1
    with pytest.raises(TimeoutError, match="budget"):
        local_service.ensure(PROVIDER, 0)
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("executable", "../other.exe"),
        ("url", "http://0.0.0.0:8191/v1"),
        ("url", "http://127.0.0.1:8191/v1?secret=1"),
        ("url", None),
        ("sha256", "0" * 64),
        ("idle_sec", True),
        ("idle_sec", 0),
        ("version", "latest"),
    ],
)
def test_invalid_manifest_cannot_launch(installed, field, value):
    folder, manifest = installed
    manifest[field] = value
    (folder / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match=r"invalid|must be|changed"):
        worker.read_manifest(folder)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("cookies", []),
        ("proxy", {}),
        ("cmd", "sessions.create"),
        ("returnOnlyCookies", False),
        ("maxTimeout", True),
        ("maxTimeout", 0),
        ("url", "https://user:pass@site.example/login.php"),
        ("url", "https://site.example/download.php?id=1"),
        ("url", "https://site.example/login.php?m=5"),
        ("url", "https://site.example/login.php#fragment"),
    ],
)
def test_only_bounded_anonymous_requests_can_reach_browser(field, value):
    with pytest.raises(ValueError, match=r"invalid|only anonymous"):
        worker.validate_request(dict(PAYLOAD, **{field: value}))


@pytest.mark.parametrize(
    "address", ["127.0.0.1", "192.168.1.2", "fec0::1", "ff00::1", "64:ff9b::192.168.1.2", "::ffff:127.0.0.1"]
)
def test_browser_targets_cannot_resolve_to_private_or_multicast_addresses(monkeypatch, address):
    monkeypatch.setattr(worker.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", (address, 443))])
    with pytest.raises(ValueError, match="non-public"):
        worker.public_target(PAYLOAD["url"])


def test_active_request_prevents_idle_shutdown_and_idle_controller_stops(installed, monkeypatch):
    service = worker.NativeService(*installed)
    monkeypatch.setattr(worker.time, "monotonic", lambda: service.last_used + 301)
    service.lock.acquire()
    assert service.idle() is False
    service.lock.release()
    stopped = []
    monkeypatch.setattr(service, "stop", lambda: stopped.append(True) or True)
    assert service.idle() is True
    assert service.closing is True
    assert stopped == [True]


def test_reaped_child_is_never_terminated_using_a_reused_pid(installed, monkeypatch):
    service = worker.NativeService(*installed)
    service.child = SimpleNamespace(pid=42, poll=lambda: 0)
    monkeypatch.setattr(worker.subprocess, "run", lambda *a, **k: pytest.fail("foreign process touched"))
    assert service.stop() is True
    assert service.child is None


def test_failed_stop_retains_ownership_for_retry(installed, monkeypatch):
    service = worker.NativeService(*installed)
    child = SimpleNamespace(
        pid=42, poll=lambda: None, wait=Mock(side_effect=[subprocess.TimeoutExpired("synthetic", 10), 0])
    )
    service.child = child
    monkeypatch.setattr(worker, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(worker.subprocess, "run", lambda *a, **k: None)
    assert service.stop() is False
    assert service.child is child
    assert service.stop() is True


@pytest.mark.parametrize("failure", ["create", "configure", "assign", None])
def test_windows_process_containment_fails_closed(monkeypatch, failure):
    kernel = SimpleNamespace(
        CreateJobObjectW=Mock(return_value=0 if failure == "create" else 123),
        GetCurrentProcess=Mock(return_value=456),
        SetInformationJobObject=Mock(return_value=failure != "configure"),
        AssignProcessToJobObject=Mock(return_value=failure != "assign"),
        CloseHandle=Mock(),
    )
    monkeypatch.setattr(worker, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(worker.ctypes, "WinDLL", lambda *a, **k: kernel, raising=False)
    monkeypatch.setattr(worker, "JOB_HANDLE", None)
    if failure:
        with pytest.raises(OSError, match=r"job|containment"):
            worker.bind_owned_children()
        assert worker.JOB_HANDLE is None
        assert kernel.CloseHandle.call_count == (failure != "create")
    else:
        worker.bind_owned_children()
        assert worker.JOB_HANDLE == 123


def test_worker_is_refused_on_platform_without_owned_process_containment(monkeypatch):
    monkeypatch.setattr(worker, "sys", SimpleNamespace(platform="linux"))
    with pytest.raises(OSError, match="not installed"):
        worker.bind_owned_children()


def test_closing_service_retries_stop_without_renewed_idle_delay(installed, monkeypatch):
    service = worker.NativeService(*installed)
    now = [service.last_used + 301]
    monkeypatch.setattr(worker.time, "monotonic", lambda: now[0])
    stops = Mock(side_effect=[False, True])
    monkeypatch.setattr(service, "stop", stops)
    assert service.idle() is False
    service.last_used = now[0]  # even a refused request must not defer retirement
    now[0] += 1
    assert service.idle() is False
    assert stops.call_count == 1
    now[0] += 10
    assert service.idle() is True
    assert stops.call_count == 2


def test_expired_startup_cannot_create_native_process(installed, monkeypatch):
    service = worker.NativeService(*installed)
    monkeypatch.setattr(worker.subprocess, "Popen", lambda *a, **k: pytest.fail("expired launch"))
    with pytest.raises(TimeoutError, match="budget"):
        service.start(worker.time.monotonic() - 1)


@pytest.mark.parametrize("failure", ["oversize", "deadline", None])
def test_forward_bounds_response_and_retires_after_failure(installed, monkeypatch, failure):
    service = worker.NativeService(*installed)
    clock = [0.0]
    monkeypatch.setattr(worker.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(worker, "public_target", lambda url: None)
    monkeypatch.setattr(service, "start", lambda deadline: None)
    stopped = Mock(return_value=True)
    monkeypatch.setattr(service, "stop", stopped)
    pieces = iter([b"x" * 16384] * 65 if failure == "oversize" else [b"ok", b""])

    def read(size):
        if failure == "deadline":
            clock[0] += 61
        return next(pieces)

    network_socket = Mock()
    connection = Mock(sock=network_socket)

    def getresponse():
        connection.sock = None  # HTTP/1.0 transfers the socket to its response
        return SimpleNamespace(status=200, read1=read)

    connection.getresponse.side_effect = getresponse
    monkeypatch.setattr(worker.http.client, "HTTPConnection", lambda *a, **k: connection)
    if failure:
        with pytest.raises((TimeoutError, ValueError), match=r"deadline|limit"):
            service.forward(PAYLOAD)
        assert service.closing is True
        stopped.assert_called_once()
    else:
        assert service.forward(PAYLOAD) == (200, b"ok")
        assert service.closing is False
        stopped.assert_not_called()
    assert network_socket.settimeout.called


def test_broker_caps_request_threads_and_releases_completed_slots(monkeypatch):
    broker = worker.Broker.__new__(worker.Broker)
    broker.request_slots = worker.threading.BoundedSemaphore(4)
    monkeypatch.setattr(worker.ThreadingHTTPServer, "process_request", lambda *a: None)
    closed = Mock()
    monkeypatch.setattr(broker, "shutdown_request", closed)
    request = Mock()
    for _ in range(4):
        broker.process_request(request, ("127.0.0.1", 1))
    broker.process_request(request, ("127.0.0.1", 1))
    closed.assert_called_once_with(request)
    monkeypatch.setattr(worker.ThreadingHTTPServer, "process_request_thread", lambda *a: None)
    broker.process_request_thread(request, ("127.0.0.1", 1))
    broker.process_request(request, ("127.0.0.1", 1))
    assert closed.call_count == 1


def test_broken_module_does_not_block_independent_manual_listener(installed, monkeypatch):
    (installed[0] / "bin" / "flaresolverr.exe").write_bytes(b"owner-updated-binary")
    monkeypatch.setattr(local_service, "listening", lambda *a: True)
    assert local_service.ensure(PROVIDER, local_service.time.monotonic() + 1) is False
    assert not (installed[0] / "worker.py").exists()
    monkeypatch.setattr(local_service, "listening", lambda *a: False)
    with pytest.raises(ValueError, match="changed"):
        local_service.ensure(PROVIDER, local_service.time.monotonic() + 1)


def test_slow_cold_start_survives_one_request_then_reuses_same_child(installed, monkeypatch):
    service = worker.NativeService(*installed)
    clock = [0.0]
    monkeypatch.setattr(worker.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(worker.time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    child = SimpleNamespace(poll=lambda: None)
    launches = []

    def launch(deadline):
        launches.append(deadline)
        service.child, service.start_deadline = child, 120

    monkeypatch.setattr(service, "launch", launch)
    connection = Mock()
    connection.getresponse.side_effect = OSError("warming")
    monkeypatch.setattr(worker.http.client, "HTTPConnection", lambda *a, **k: connection)
    with pytest.raises(worker.StartupPending, match="warming"):
        service.start(2)
    assert service.child is child
    assert service.closing is False
    assert service.ready is False
    connection.getresponse.side_effect = None
    connection.getresponse.return_value = SimpleNamespace(status=200, read=lambda size: b'{"version":"3.5.2"}')
    service.start(10)
    assert service.ready is True
    assert launches == [2]


def test_abandoned_warmup_has_independent_cap(installed, monkeypatch):
    service = worker.NativeService(*installed)
    service.child = SimpleNamespace(poll=lambda: None)
    service.start_deadline = service.last_used + 120
    monkeypatch.setattr(worker.time, "monotonic", lambda: service.start_deadline - 1)
    stops = Mock(return_value=True)
    monkeypatch.setattr(service, "stop", stops)
    assert service.idle() is False
    stops.assert_not_called()
    monkeypatch.setattr(worker.time, "monotonic", lambda: service.start_deadline + 1)
    assert service.idle() is True
    stops.assert_called_once()


def test_forward_pending_start_is_not_terminated(installed, monkeypatch):
    service = worker.NativeService(*installed)
    monkeypatch.setattr(worker, "public_target", lambda url: None)
    monkeypatch.setattr(service, "start", Mock(side_effect=worker.StartupPending("warming")))
    stops = Mock()
    monkeypatch.setattr(service, "stop", stops)
    with pytest.raises(worker.StartupPending, match="warming"):
        service.forward(PAYLOAD)
    stops.assert_not_called()
    assert service.closing is False


@pytest.mark.parametrize("body", [b"[]", b'{"version":"wrong"}', b"{bad json"])
def test_readiness_requires_expected_native_version(installed, monkeypatch, body):
    service = worker.NativeService(*installed)
    service.child = SimpleNamespace(poll=lambda: None)
    service.start_deadline = 120
    clock = [0.0]
    monkeypatch.setattr(worker.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(worker.time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    connection = Mock()
    connection.getresponse.return_value = SimpleNamespace(status=200, read=lambda size: body)
    monkeypatch.setattr(worker.http.client, "HTTPConnection", lambda *a, **k: connection)
    with pytest.raises(worker.StartupPending, match="warming"):
        service.start(0.2)
    assert service.ready is False


@pytest.mark.parametrize(("error", "expected"), [(OSError("missing"), "unavailable"), (ValueError("bad"), "protocol")])
def test_provider_maps_local_launch_errors_to_service_failure(monkeypatch, error, expected):
    from tow.protection import providers
    from tow.protection.spec import ProviderUnavailableError

    monkeypatch.setattr(local_service, "ensure", Mock(side_effect=error))
    with pytest.raises(ProviderUnavailableError) as caught:
        providers.cookie_api(dict(PROVIDER, id="local-test"), "https://site.example", 30)
    assert caught.value.reason == expected


@pytest.mark.parametrize(
    ("peer", "host", "origin"),
    [
        ("192.168.1.2", "127.0.0.1:8191", None),
        ("127.0.0.1", "attacker.example:8191", None),
        ("127.0.0.1", "127.0.0.1:8191", "https://attacker.example"),
        ("127.0.0.1", "127.0.0.1:8191", None),
    ],
)
def test_handler_loopback_host_and_origin_boundary(peer, host, origin):
    handler = worker.Handler.__new__(worker.Handler)
    handler.server = SimpleNamespace(api_port=8191)
    handler.client_address = (peer, 1)
    handler.headers = {"Host": host, "Origin": origin}
    replies = []
    handler.reply = lambda *args: replies.append(args)
    expected = peer == "127.0.0.1" and host == "127.0.0.1:8191" and origin is None
    assert handler.local() is expected
    assert bool(replies) is not expected


@pytest.mark.parametrize("error", [ValueError("private address"), OSError("DNS unavailable")])
def test_bad_mirror_does_not_retire_warm_service(installed, monkeypatch, error):
    service = worker.NativeService(*installed)
    stops = Mock()
    monkeypatch.setattr(service, "stop", stops)
    monkeypatch.setattr(worker, "public_target", Mock(side_effect=error))
    with pytest.raises(type(error), match=r"private|DNS"):
        service.forward(PAYLOAD)
    stops.assert_not_called()
    assert service.closing is False


def test_last_second_readiness_is_preserved_for_later_request(installed, monkeypatch):
    service = worker.NativeService(*installed)
    clock = [0.0]
    monkeypatch.setattr(worker.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(worker, "public_target", lambda url: None)

    def start(deadline):
        clock[0], service.ready = deadline - 0.5, True

    stops = Mock()
    monkeypatch.setattr(service, "start", start)
    monkeypatch.setattr(service, "stop", stops)
    with pytest.raises(worker.StartupPending, match="became ready"):
        service.forward(PAYLOAD)
    assert service.ready is True
    assert service.closing is False
    stops.assert_not_called()


def test_failed_stop_exits_broker_when_windows_job_guarantees_cleanup(installed, monkeypatch):
    service = worker.NativeService(*installed)
    service.closing = True
    monkeypatch.setattr(worker, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(worker, "JOB_HANDLE", 123)
    monkeypatch.setattr(service, "stop", lambda: False)
    assert service.idle() is True


def test_native_work_area_is_cleaned_without_removing_neighbor_files(installed):
    folder, _ = installed
    work = folder / "temp" / "native"
    work.mkdir(parents=True)
    (work / "old-browser-profile").mkdir()
    (work / "old-browser-profile" / "disposable").write_bytes(b"temporary")
    neighbor = folder / "temp" / "keep"
    neighbor.write_bytes(b"not native work")
    env = worker.native_environment(folder)
    assert list(work.iterdir()) == []
    assert neighbor.read_bytes() == b"not native work"
    assert env["TEMP"] == str(work)


def test_deep_manifest_is_refused_as_protocol_error(installed):
    (installed[0] / "manifest.json").write_text("[" * 1100 + "0" + "]" * 1100)
    with pytest.raises(ValueError, match="manifest"):
        worker.read_manifest(installed[0])


def test_handler_warming_is_retryable_and_does_not_renew_closing_lifetime():
    import io

    handler = worker.Handler.__new__(worker.Handler)
    native = SimpleNamespace(
        lock=worker.threading.Lock(),
        closing=False,
        last_used=0,
        forward=Mock(side_effect=worker.StartupPending("warming")),
    )
    handler.server = SimpleNamespace(native=native)
    handler.local = lambda: True
    handler.path = "/v1"
    body = json.dumps(PAYLOAD).encode()
    handler.headers = {"Content-Type": "application/json", "Content-Length": str(len(body))}
    handler.rfile = io.BytesIO(body)
    replies = []
    handler.reply = lambda *args: replies.append(args)
    handler.do_POST()
    assert replies[0][0] == 503
    assert not native.lock.locked()
    headers = []
    handler.send_response = lambda status: None
    handler.send_header = lambda *args: headers.append(args)
    handler.end_headers = lambda: None
    handler.wfile = io.BytesIO()
    worker.Handler.reply(handler, 503, b"{}")
    assert ("Retry-After", "5") in headers


def test_settings_identify_module_without_launch_and_report_damage(installed, monkeypatch):
    from tow.web import services

    monkeypatch.setattr(local_service, "listening", lambda *a: pytest.fail("settings contacted module"))
    cfg = {"protection": {"providers": [dict(PROVIDER, id="local-test")]}}
    view = services.protection_settings_view(cfg)
    assert view["cards"][0]["on_demand"] is True
    assert view["cards"][0]["local_runtime_refused"] is False
    (installed[0] / "manifest.json").write_text("{}")
    view = services.protection_settings_view(cfg)
    assert view["cards"][0]["on_demand"] is False
    assert view["cards"][0]["local_runtime_refused"] is True
    assert not (installed[0] / "worker.py").exists()


# --- the launcher digest: cached for display, always fresh before the module runs ---------------


@pytest.fixture
def hashes(monkeypatch):
    import hashlib

    count = [0]
    real = hashlib.file_digest

    def counted(*args, **kwargs):
        count[0] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(hashlib, "file_digest", counted)
    return count


def test_settings_and_provider_calls_do_not_rehash_an_unchanged_module(installed, monkeypatch, hashes):
    from tow.web import services

    cfg = {"protection": {"providers": [dict(PROVIDER, id="local-test")]}}
    for _ in range(3):
        assert services.protection_settings_view(cfg)["cards"][0]["on_demand"] is True
    monkeypatch.setattr(local_service, "listening", lambda *a: True)
    for _ in range(2):
        assert local_service.ensure(PROVIDER, local_service.time.monotonic() + 1) is True
    assert hashes[0] == 1


def test_a_replaced_module_is_hashed_again_and_refused(installed, hashes):
    folder, _manifest = installed
    assert local_service.managed(PROVIDER) is True
    (folder / "bin" / "flaresolverr.exe").write_bytes(b"replaced-service-binary")
    with pytest.raises(ValueError, match="changed"):
        local_service.managed(PROVIDER)
    assert hashes[0] == 2


def test_a_cached_digest_never_lets_a_changed_module_run(installed, monkeypatch):
    import os

    folder, manifest = installed
    binary = folder / "bin" / "flaresolverr.exe"
    assert local_service.managed(PROVIDER) is True
    before = binary.stat()
    binary.write_bytes(b"synthetic-servicf")  # same size ...
    os.utime(binary, ns=(before.st_atime_ns, before.st_mtime_ns))  # ... and time: the display cache matches
    assert local_service.managed(PROVIDER) is True
    with pytest.raises(ValueError, match="changed"):
        worker.read_manifest(folder)
    monkeypatch.setattr(worker.subprocess, "Popen", lambda *a, **k: pytest.fail("changed module started"))
    with pytest.raises(ValueError, match="changed"):
        worker.NativeService(folder, manifest).launch(worker.time.monotonic() + 10)


@pytest.mark.parametrize(
    ("root", "expected"),
    [
        ("D:\\Windows", "D:\\Windows\\System32\\taskkill.exe"),
        (None, "C:\\Windows\\System32\\taskkill.exe"),
        ("Windows", "C:\\Windows\\System32\\taskkill.exe"),  # a relative root would search the work folder
    ],
)
def test_stopping_the_module_runs_the_systems_own_taskkill(installed, monkeypatch, root, expected):
    service = worker.NativeService(*installed)
    service.child = Mock(pid=4321, poll=Mock(return_value=None), wait=Mock(return_value=0))
    calls = []
    monkeypatch.setattr(worker, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(worker.subprocess, "run", lambda argv, **kwargs: calls.append(argv))
    monkeypatch.delenv("SYSTEMROOT", raising=False)
    if root is not None:
        monkeypatch.setenv("SYSTEMROOT", root)
    assert service.stop() is True
    assert calls == [[expected, "/PID", "4321", "/T", "/F"]]
