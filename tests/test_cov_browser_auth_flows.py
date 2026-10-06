"""Browser login sessions (tow.browser_auth) with every system seam faked.

No browser is looked up on this machine or started, no socket is opened: the process,
the DevTools HTTP endpoint (``urlopen``) and the CDP websocket are fakes at the module seams.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tow import browser_auth as ba
from tow import platform
from tow.i18n import t
from tow.log import read_events
from tow.paths import data_dir
from tow.platform import posix, windows
from tow.platform.posix import PosixBackend
from tow.platform.windows import WindowsBackend

TOPIC = "https://nnmclub.to/forum/viewtopic.php?t=1"
PAGE_TARGET = {"type": "page", "url": TOPIC, "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/1"}


# The real lookup, taken at import (collection), before tests/conftest.py replaces it with a
# refusal for every test.
_REAL_FIND_BROWSER_EXECUTABLE = ba.find_browser_executable


def _real_find_browser_executable(_monkeypatch):
    return _REAL_FIND_BROWSER_EXECUTABLE


class FakeProcess:
    def __init__(self, pid: int = 4242, *, exit_code: int | None = None) -> None:
        self.pid = pid
        self.exit_code = exit_code
        self.terminated = False
        self.killed = False
        self.wait_raises: BaseException | None = None

    def poll(self):
        return self.exit_code

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        if self.wait_raises is not None:
            raise self.wait_raises
        self.exit_code = 0
        return 0

    def kill(self):
        self.killed = True


class SyncThread:
    """threading.Thread stand-in that runs the session in the calling thread."""

    def __init__(self, target, args=(), name=None, daemon=None):
        self.target, self.args, self.name = target, args, name

    def start(self):
        self.target(*self.args)


class Clock:
    """A fake monotonic clock that moves on by ``step`` at every read."""

    def __init__(self, step: float) -> None:
        self.now = 0.0
        self.step = step

    def monotonic(self) -> float:
        self.now += self.step
        return self.now


def _fake_time(monkeypatch, step: float) -> Clock:
    import time as real_time

    clock = Clock(step)
    monkeypatch.setattr(
        ba, "time", SimpleNamespace(monotonic=clock.monotonic, time=real_time.time, sleep=lambda _s: None)
    )
    return clock


def _no_waiting(monkeypatch):
    real_sleep = asyncio.sleep
    monkeypatch.setattr(ba.asyncio, "sleep", lambda _s: real_sleep(0))


@pytest.fixture
def launched(monkeypatch):
    """Session plumbing: a fake browser process, a fixed port, synchronous threads."""
    started: list[list[str]] = []
    process = FakeProcess()
    stopped: list[int] = []

    def popen(args, **_kwargs):
        started.append([str(a) for a in args])
        return process

    monkeypatch.setattr(ba, "find_browser_executable", lambda: "msedge.exe")
    monkeypatch.setattr(ba, "_free_port", lambda: 9333)
    monkeypatch.setattr(ba, "_show_browser_window", lambda _pid: None)
    # Linux without a desktop in the environment asks systemd for it: no program in tests.
    monkeypatch.setattr(posix, "_run", lambda *_a, **_k: "")
    monkeypatch.setattr(ba.subprocess, "Popen", popen)
    monkeypatch.setattr(ba, "_terminate_process", lambda p: stopped.append(p.pid))
    # Only browser_auth sees the synchronous Thread: asyncio.to_thread keeps real worker threads.
    monkeypatch.setattr(ba, "threading", SimpleNamespace(Thread=SyncThread, RLock=threading.RLock))
    return SimpleNamespace(started=started, process=process, stopped=stopped)


def _start(manager: ba.BrowserAuthManager, on_success, topic_id: str = "t1") -> dict[str, Any]:
    return manager.start(
        topic_id=topic_id,
        tracker_name="nnmclub",
        topic_url=TOPIC,
        start_url="https://nnmclub.to/forum/login.php",
        on_success=on_success,
        timeout_sec=5,
    )


def _wait_returns(monkeypatch, value=None, error: BaseException | None = None):
    seen: dict[str, Any] = {}

    async def wait(**kwargs):
        seen.update(kwargs)
        if error is not None:
            raise error
        return value

    monkeypatch.setattr(ba, "_wait_for_authenticated_topic", wait)
    return seen


# --- the manager ----------------------------------------------------------------------------


def test_a_session_signs_in_confirms_and_cleans_up(monkeypatch, launched):
    seen = _wait_returns(monkeypatch, ({"phpbb2mysql_4_sid": "abc"}, "Edge/140"))
    confirmed: list[tuple] = []
    manager = ba.BrowserAuthManager()

    first = _start(manager, lambda cookies, ua: confirmed.append((cookies, ua)) or {"message": "проверено"})

    assert first["status"] == "succeeded"  # the session ran synchronously
    assert confirmed == [({"phpbb2mysql_4_sid": "abc"}, "Edge/140")]
    assert seen["port"] == 9333
    assert seen["timeout_sec"] == 30  # never shorter than 30 s
    args = launched.started[0]
    assert args[0] == "msedge.exe"
    assert "--remote-debugging-port=9333" in args
    assert args[-2:] == ["--", "https://nnmclub.to/forum/login.php"]  # never read as a switch
    profile_arg = next(a for a in args if a.startswith("--user-data-dir="))
    assert not Path(profile_arg.split("=", 1)[1]).parent.exists()  # the profile is gone
    assert launched.stopped == [4242]
    status = manager.status("t1", first["operation_id"])
    assert (status["status"], status["message"]) == ("succeeded", "проверено")
    assert manager.status("t1")["status"] == "idle"  # no longer the topic's active session
    assert any(e["kind"] == "browser_auth_succeeded" for e in read_events(limit=5))


def test_a_session_without_a_message_says_it_was_checked(monkeypatch, launched):
    _wait_returns(monkeypatch, ({"sid": "1"}, "UA"))
    manager = ba.BrowserAuthManager()
    result = _start(manager, lambda *_: None)
    assert manager.status("t1", result["operation_id"])["message"] == t("browser_auth.ok_checked")


@pytest.mark.parametrize(
    ("answer", "message"),
    [({"ok": False}, None), ({"ok": False, "message": "куки не подошли"}, "куки не подошли")],
)
def test_a_session_whose_check_fails_is_failed(monkeypatch, launched, answer, message):
    _wait_returns(monkeypatch, ({"sid": "1"}, "UA"))
    manager = ba.BrowserAuthManager()
    result = _start(manager, lambda *_: answer)
    status = manager.status("t1", result["operation_id"])
    assert status["status"] == "failed"
    assert status["message"] == (message or t("browser_auth.ok_check_failed"))


def test_an_unexpected_error_is_a_technical_failure_without_details(monkeypatch, launched):
    _wait_returns(monkeypatch, ({"sid": "1"}, "UA"))
    manager = ba.BrowserAuthManager()

    def broken(*_):
        raise KeyError("secret detail")

    result = _start(manager, broken)
    status = manager.status("t1", result["operation_id"])
    assert (status["status"], status["message"]) == ("failed", t("browser_auth.technical_error"))
    event = next(e for e in read_events(limit=5) if e["kind"] == "browser_auth_failed")
    assert (event["phase"], event["error_type"]) == ("confirm", "KeyError")
    assert "secret detail" not in json.dumps(event, ensure_ascii=False)


def test_no_browser_on_this_machine_fails_before_anything_starts(monkeypatch, launched):
    def none():
        raise ba.BrowserAuthError(t("browser_auth.no_browser"))

    monkeypatch.setattr(ba, "find_browser_executable", none)
    manager = ba.BrowserAuthManager()
    result = _start(manager, lambda *_: pytest.fail("no confirmation without a browser"))
    status = manager.status("t1", result["operation_id"])
    assert (status["status"], status["message"]) == ("failed", t("browser_auth.no_browser"))
    assert launched.started == []
    assert launched.stopped == []  # no process to stop


def test_a_second_start_while_waiting_returns_the_running_session(monkeypatch):
    never_runs = SimpleNamespace(
        Thread=lambda **_k: SimpleNamespace(start=lambda: None),
        RLock=threading.RLock,
    )
    monkeypatch.setattr(ba, "threading", never_runs)
    manager = ba.BrowserAuthManager()
    first = _start(manager, lambda *_: {})
    second = _start(manager, lambda *_: {})
    assert second == first
    assert first["status"] == "starting"
    assert first["message"] == t("browser_auth.starting")
    other = _start(manager, lambda *_: {}, topic_id="t2")
    assert other["operation_id"] != first["operation_id"]
    # Another topic cannot read this one's session.
    assert manager.status("t2", first["operation_id"]) == {"status": "idle", "topic_id": "t2"}


def test_a_stale_active_pointer_does_not_block_a_new_session(monkeypatch, launched):
    _wait_returns(monkeypatch, ({"sid": "1"}, "UA"))
    manager = ba.BrowserAuthManager()
    manager._active_by_topic["t1"] = "browser-auth-vanished"  # no such session any more
    result = _start(manager, lambda *_: {})
    assert result["operation_id"] != "browser-auth-vanished"
    assert result["status"] == "succeeded"


def test_a_finished_session_of_a_replaced_operation_keeps_the_new_one_active(monkeypatch, launched):
    _wait_returns(monkeypatch, ({"sid": "1"}, "UA"))
    manager = ba.BrowserAuthManager()
    record = {
        "operation_id": "old",
        "topic_id": "t1",
        "tracker": "nnmclub",
        "status": "starting",
        "profile": str(data_dir() / "browser-auth" / "old" / "profile"),
    }
    manager._sessions["old"] = record
    manager._active_by_topic["t1"] = "newer"
    manager._run(record, TOPIC, "https://nnmclub.to/", lambda *_: {}, 30)
    assert manager._active_by_topic == {"t1": "newer"}
    assert record["status"] == "succeeded"


def test_a_logging_failure_never_breaks_the_session(monkeypatch, launched):
    _wait_returns(monkeypatch, ({"sid": "1"}, "UA"))
    monkeypatch.setattr(ba, "log_event", lambda *_a, **_k: (_ for _ in ()).throw(OSError("disk full")))
    manager = ba.BrowserAuthManager()
    assert _start(manager, lambda *_: {})["status"] == "succeeded"


@pytest.mark.parametrize(
    ("backend", "expected"),
    [
        (WindowsBackend(), {"creationflags": windows.CREATE_NEW_PROCESS_GROUP, "close_fds": True}),
        (PosixBackend("linux"), {"start_new_session": True, "close_fds": True}),
    ],
)
def test_the_browser_gets_its_own_process_group_on_every_system(monkeypatch, launched, backend, expected):
    _wait_returns(monkeypatch, ({"sid": "1"}, "UA"))
    options: list[dict] = []

    def popen(_args, **kw):
        options.append({key: kw[key] for key in expected})
        return FakeProcess()

    monkeypatch.setattr(ba.subprocess, "Popen", popen)
    with platform.use(backend):
        _start(ba.BrowserAuthManager(), lambda *_: {})
    assert options == [expected]


def test_on_linux_and_macos_the_browser_writes_only_into_its_session_folder(monkeypatch, launched):
    _wait_returns(monkeypatch, ({"sid": "1"}, "UA"))
    seen: list[dict] = []

    def popen(args, **kw):
        seen.append({"args": [str(a) for a in args], "env": kw.get("env")})
        assert Path(kw["env"]["HOME"]).is_dir()  # created before the browser starts
        return FakeProcess()

    monkeypatch.setattr(ba.subprocess, "Popen", popen)
    with platform.use(PosixBackend("macos", home="/Users/x")):
        _start(ba.BrowserAuthManager(), lambda *_: {})
    (launch,) = seen
    assert {"--password-store=basic", "--use-mock-keychain"} <= set(launch["args"])
    assert launch["args"][-1] == "https://nnmclub.to/forum/login.php"  # the page stays last
    home = Path(launch["env"]["HOME"])
    profile = next(a for a in launch["args"] if a.startswith("--user-data-dir=")).split("=", 1)[1]
    assert home.parent == Path(profile).parent  # one session folder, removed as a whole
    assert not home.parent.exists()


def test_a_snap_chromium_is_refused_with_the_reason(monkeypatch, launched):
    monkeypatch.setattr(ba, "find_browser_executable", lambda: "/snap/bin/chromium")
    with platform.use(PosixBackend("linux", home="/home/x")):
        result = _start(ba.BrowserAuthManager(), lambda *_: {})
    assert result["status"] == "failed"
    assert result["message"] == t("browser_auth.snap_browser")
    assert launched.started == []  # never started


# --- finding the browser -----------------------------------------------------------------------


class BrowserBackend:
    """The platform as browser_auth sees it (tests/test_platform.py covers the real lookups)."""

    name = "linux"

    def __init__(self, found: list[str] | None = None, *, stopped: bool = True) -> None:
        self.found = found or []
        self.stopped = stopped
        self.calls: list[tuple] = []

    def browser_executables(self):
        return self.found

    def bring_to_front(self, pid):
        self.calls.append(("front", pid))

    def terminate(self, pid, timeout=10.0):
        self.calls.append(("terminate", pid, timeout))
        return self.stopped


def test_a_configured_browser_must_exist(monkeypatch, tmp_path):
    find = _real_find_browser_executable(monkeypatch)
    exe = tmp_path / "chrome.exe"
    exe.write_bytes(b"")
    monkeypatch.setenv("TOW_BROWSER_EXECUTABLE", f"  {exe}  ")
    assert find() == str(exe)
    monkeypatch.setenv("TOW_BROWSER_EXECUTABLE", str(tmp_path / "missing.exe"))
    with pytest.raises(ba.BrowserAuthError, match=t("browser_auth.exe_missing")):
        find()


def test_the_first_browser_the_platform_finds_is_used_and_none_is_an_error(monkeypatch):
    find = _real_find_browser_executable(monkeypatch)
    monkeypatch.delenv("TOW_BROWSER_EXECUTABLE", raising=False)
    with platform.use(BrowserBackend(["/usr/bin/chromium", "/usr/bin/microsoft-edge"])):
        assert find() == "/usr/bin/chromium"
    with platform.use(BrowserBackend([])), pytest.raises(ba.BrowserAuthError, match=t("browser_auth.no_browser")):
        find()


# --- the window ------------------------------------------------------------------------------


def test_the_browser_window_is_brought_to_the_front_by_the_platform():
    backend = BrowserBackend()
    with platform.use(backend):
        ba._show_browser_window(4242)
    assert backend.calls == [("front", 4242)]


# --- DevTools endpoint ------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self):
        return self.body


def test_json_endpoint_is_read_and_its_failures_are_plain(monkeypatch):
    urls: list[str] = []

    def urlopen(url, timeout):
        urls.append(url)
        return FakeResponse(json.dumps([PAGE_TARGET]).encode())

    monkeypatch.setattr(ba.urllib.request, "urlopen", urlopen)
    assert ba._json_get(9222, "/json/list") == [PAGE_TARGET]
    assert urls == ["http://127.0.0.1:9222/json/list"]

    monkeypatch.setattr(ba.urllib.request, "urlopen", lambda *_a, **_k: FakeResponse(b"not json"))
    with pytest.raises(ba.BrowserAuthError, match=t("browser_auth.cdp_unavailable")):
        ba._json_get(9222, "/json/list")

    def refused(*_a, **_k):
        raise ConnectionRefusedError

    monkeypatch.setattr(ba.urllib.request, "urlopen", refused)
    with pytest.raises(ba.BrowserAuthError, match=t("browser_auth.cdp_unavailable")):
        ba._json_get(9222, "/json/list")


def test_the_topic_page_target_is_found_after_the_endpoint_comes_up(monkeypatch):
    _no_waiting(monkeypatch)
    _fake_time(monkeypatch, 0.1)
    answers: list[Any] = [
        ba.BrowserAuthError("not yet"),
        {"unexpected": "shape"},
        [
            "garbage",
            {"type": "service_worker", "url": TOPIC, "webSocketDebuggerUrl": "ws://sw"},
            {"type": "page", "url": "https://evil.example/", "webSocketDebuggerUrl": "ws://evil"},
            {"type": "page", "url": TOPIC},
            PAGE_TARGET,
        ],
    ]

    def json_get(_port, path):
        assert path == "/json/list"
        answer = answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(ba, "_json_get", json_get)
    origins = {"https://nnmclub.to:443"}
    found = asyncio.run(ba._wait_target(9222, FakeProcess(), 60, origins))
    assert found == PAGE_TARGET["webSocketDebuggerUrl"]


def test_waiting_for_the_target_stops_when_the_browser_closes_or_times_out(monkeypatch):
    _no_waiting(monkeypatch)
    _fake_time(monkeypatch, 4)
    monkeypatch.setattr(ba, "_json_get", lambda *_a: [])
    origins = {"https://nnmclub.to:443"}
    with pytest.raises(ba.BrowserAuthError, match=t("browser_auth.closed_before_login")):
        asyncio.run(ba._wait_target(9222, FakeProcess(exit_code=0), 60, origins))
    with pytest.raises(ba.BrowserAuthError, match=t("browser_auth.cannot_connect")):
        asyncio.run(ba._wait_target(9222, FakeProcess(), 60, origins))


# --- the CDP conversation ---------------------------------------------------------------------


class FakeCdp:
    """A CDP websocket; ``pages`` are the successive Runtime.evaluate values."""

    def __init__(self, pages: list[dict[str, Any]], cookies: list[Any] | None = None) -> None:
        self.pages = pages
        self.cookies = cookies if cookies is not None else [{"domain": ".nnmclub.to", "name": "sid", "value": "x"}]
        self.outbox: list[dict[str, Any]] = []
        self.methods: list[str] = []
        self.refuse: set[str] = set()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def send(self, raw: str) -> None:
        request = json.loads(raw)
        method, ident = request["method"], request["id"]
        self.methods.append(method)
        self.outbox.append({"method": "Network.requestWillBeSent", "params": {}})  # an event, no id
        if method in self.refuse:
            self.outbox.append({"id": ident, "error": {"message": "refused"}})
        elif method == "Browser.getVersion":
            self.outbox.append({"id": ident, "result": {"userAgent": "Edge/140"}})
        elif method == "Runtime.evaluate":
            value = self.pages.pop(0) if len(self.pages) > 1 else self.pages[0]
            self.outbox.append({"id": ident, "result": {"result": {"value": value}}})
        elif method == "Network.getAllCookies":
            self.outbox.append({"id": ident, "result": {"cookies": self.cookies}})
        else:
            self.outbox.append({"id": ident, "result": {}})

    async def recv(self) -> str:
        return json.dumps(self.outbox.pop(0))


def _cdp(monkeypatch, fake: FakeCdp) -> list[dict[str, Any]]:
    import websockets.asyncio.client as ws_client

    opened: list[dict[str, Any]] = []

    def connect(url, **kwargs):
        opened.append({"url": url, **kwargs})
        return fake

    monkeypatch.setattr(ws_client, "connect", connect)

    async def target(*_a):
        return PAGE_TARGET["webSocketDebuggerUrl"]

    monkeypatch.setattr(ba, "_wait_target", target)
    _no_waiting(monkeypatch)
    return opened


def _signed_in(**extra: Any) -> dict[str, Any]:
    return {"href": TOPIC, "host": "nnmclub.to", "hasDownload": True, **extra}


def test_cookies_of_the_tracker_host_are_collected_once_the_download_link_shows(monkeypatch):
    fake = FakeCdp(
        [
            {"href": "https://nnmclub.to/forum/login.php", "host": "nnmclub.to", "hasDownload": False},
            {"href": "https://evil.example/x", "host": "nnmclub.to", "hasDownload": True},
            _signed_in(),
        ],
        cookies=[
            "not a cookie",
            {"domain": ".nnmclub.to", "name": "phpbb2mysql_4_sid", "value": "s"},
            {"domain": "nnmclub.to", "name": "empty", "value": ""},
            {"domain": ".other.example", "name": "foreign", "value": "f"},
            {"domain": "NNMCLUB.TO", "name": "uid", "value": "7"},
        ],
    )
    opened = _cdp(monkeypatch, fake)
    _fake_time(monkeypatch, 0.001)
    cookies, user_agent = asyncio.run(
        ba._wait_for_authenticated_topic(port=9222, topic_url=TOPIC, timeout_sec=60, process=FakeProcess())
    )
    assert cookies == {"phpbb2mysql_4_sid": "s", "uid": "7"}
    assert user_agent == "Edge/140"
    assert opened[0]["url"] == PAGE_TARGET["webSocketDebuggerUrl"]
    assert fake.methods[:3] == ["Network.enable", "Page.bringToFront", "Browser.getVersion"]
    assert fake.methods.count("Network.getAllCookies") == 1


def test_a_signed_in_page_without_usable_cookies_is_not_confirmed(monkeypatch):
    fake = FakeCdp([_signed_in()], cookies=[{"domain": ".other.example", "name": "x", "value": "1"}])
    _cdp(monkeypatch, fake)
    _fake_time(monkeypatch, 7)
    with pytest.raises(ba.BrowserAuthError, match=t("browser_auth.not_confirmed")):
        asyncio.run(ba._wait_for_authenticated_topic(port=9222, topic_url=TOPIC, timeout_sec=30, process=FakeProcess()))


def test_a_closed_browser_ends_the_wait(monkeypatch):
    _cdp(monkeypatch, FakeCdp([_signed_in()]))
    _fake_time(monkeypatch, 0.001)
    with pytest.raises(ba.BrowserAuthError, match=t("browser_auth.closed_before_confirm")):
        asyncio.run(
            ba._wait_for_authenticated_topic(
                port=9222, topic_url=TOPIC, timeout_sec=30, process=FakeProcess(exit_code=0)
            )
        )


def test_a_refused_cdp_command_is_a_plain_error(monkeypatch):
    fake = FakeCdp([_signed_in()])
    fake.refuse.add("Network.enable")
    _cdp(monkeypatch, fake)
    with pytest.raises(ba.BrowserAuthError, match=t("browser_auth.cdp_refused")):
        asyncio.run(ba._wait_for_authenticated_topic(port=9222, topic_url=TOPIC, timeout_sec=30, process=FakeProcess()))


def test_a_bad_topic_url_and_a_missing_websockets_library_are_plain_errors(monkeypatch):
    with pytest.raises(ba.BrowserAuthError, match=t("browser_auth.bad_url")):
        asyncio.run(ba._wait_for_authenticated_topic(port=1, topic_url="not a url", timeout_sec=30, process=None))
    monkeypatch.setitem(sys.modules, "websockets.asyncio.client", None)
    with pytest.raises(ba.BrowserAuthError, match=t("browser_auth.needs_websockets")):
        asyncio.run(ba._wait_for_authenticated_topic(port=1, topic_url=TOPIC, timeout_sec=30, process=None))


def test_full_session_through_the_devtools_seams(monkeypatch, launched):
    """start() -> browser -> /json/list -> CDP -> cookies -> on_success, with only the seams faked."""
    monkeypatch.setattr(
        ba.urllib.request, "urlopen", lambda *_a, **_k: FakeResponse(json.dumps([PAGE_TARGET]).encode())
    )
    import websockets.asyncio.client as ws_client

    fake = FakeCdp([_signed_in()])
    monkeypatch.setattr(ws_client, "connect", lambda *_a, **_k: fake)
    _no_waiting(monkeypatch)
    received: list[tuple] = []
    manager = ba.BrowserAuthManager()

    result = _start(manager, lambda cookies, ua: received.append((cookies, ua)) or {"ok": True})

    assert result["status"] == "succeeded"
    assert received == [({"sid": "x"}, "Edge/140")]
    assert launched.stopped == [4242]


# --- stopping the browser and removing the profile -------------------------------------------


def test_a_browser_that_survives_its_tree_being_stopped_is_terminated_then_killed():
    backend = BrowserBackend(stopped=False)
    with platform.use(backend):
        process = FakeProcess()
        ba._terminate_process(process)
        assert (process.terminated, process.killed) == (True, False)

        hung = FakeProcess()
        hung.wait_raises = subprocess.TimeoutExpired("msedge", 5)
        ba._terminate_process(hung)
        assert (hung.terminated, hung.killed) == (True, True)
    assert backend.calls == [("terminate", 4242, 5), ("terminate", 4242, 5)]


def test_a_stopped_tree_needs_nothing_more():
    backend = BrowserBackend()
    process = FakeProcess(pid=9, exit_code=1)
    with platform.use(backend):
        ba._terminate_process(process)
    assert backend.calls == [("terminate", 9, 5)]
    assert not process.terminated


def test_a_profile_that_cannot_be_removed_is_reported(monkeypatch, tmp_path):
    session = tmp_path / "op" / "profile"
    session.mkdir(parents=True)
    sleeps: list[float] = []
    monkeypatch.setattr(ba.shutil, "rmtree", lambda *_a, **_k: None)
    monkeypatch.setattr(ba, "time", SimpleNamespace(sleep=sleeps.append))
    assert ba._remove_profile(session, attempts=3) is False
    assert sleeps == [0.5, 0.5, 0.5]
    assert session.exists()


def test_the_sweep_runs_once_and_keeps_active_sessions(monkeypatch):
    root = data_dir() / "browser-auth"
    active = root / "browser-auth-live" / "profile"
    active.mkdir(parents=True)
    (root / "stray.txt").write_text("not a session", encoding="utf-8")
    manager = ba.BrowserAuthManager()
    manager._sessions["browser-auth-live"] = {"status": "waiting", "topic_id": "t9"}
    manager._sweep_stale_profiles_unlocked()
    assert active.exists()
    assert (root / "stray.txt").exists()
    leftover = root / "browser-auth-dead" / "profile"
    leftover.mkdir(parents=True)
    manager._sweep_stale_profiles_unlocked()  # already swept: a no-op
    assert leftover.exists()
