"""tow.platform: the Windows, Linux and macOS backends, each tested on any machine.

The system is faked at the module seams (ctypes, subprocess, the posix ``_read``/``_run``/
``_clock``/``_signal`` helpers): nothing here starts a process, signals one or opens a socket.
"""

from __future__ import annotations

import ctypes
import json
import os
import signal
import stat
import struct
import subprocess
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

from tow import platform
from tow.platform import _common, posix, windows
from tow.platform.posix import PosixBackend
from tow.platform.windows import WindowsBackend

T0 = 1_790_000_000.0
HOUR = 3600


# --- choosing the backend ----------------------------------------------------------------------


@pytest.mark.parametrize(("sys_platform", "name"), [("win32", "windows"), ("darwin", "macos"), ("linux", "linux")])
def test_this_os_names_the_system(monkeypatch, sys_platform, name):
    monkeypatch.setattr(platform.sys, "platform", sys_platform)
    assert platform.this_os() == name
    assert platform.backend_for(name).name == name


def test_an_unknown_name_is_refused():
    with pytest.raises(ValueError, match="unknown platform"):
        platform.backend_for("plan9")
    with pytest.raises(ValueError, match="not a POSIX"):
        PosixBackend("windows")


def test_a_backend_is_injected_and_put_back():
    native = platform.current()
    assert native.name == platform.this_os()
    linux = PosixBackend("linux", home="/home/user")
    with platform.use(linux):
        assert platform.current() is linux
        with platform.use(WindowsBackend()) as inner:
            assert platform.current() is inner
        assert platform.current() is linux
    assert platform.current() is native
    platform.set_backend(linux)
    try:
        assert platform.current() is linux
    finally:
        platform.set_backend(None)
    assert platform.current() is native


@pytest.mark.parametrize(
    ("mode", "attributes", "plain_file", "plain_dir", "link"),
    [
        (stat.S_IFREG, 0, True, False, False),
        (stat.S_IFDIR, 0, False, True, False),
        (stat.S_IFLNK, 0, False, False, True),
        (stat.S_IFIFO, 0, False, False, False),
        # A Windows junction or a file symlink reads as a directory or a file with the reparse bit.
        (stat.S_IFDIR, stat.FILE_ATTRIBUTE_REPARSE_POINT, False, False, True),
        (stat.S_IFREG, stat.FILE_ATTRIBUTE_REPARSE_POINT, False, False, True),
    ],
)
def test_links_and_reparse_points_are_never_plain_files_or_folders(mode, attributes, plain_file, plain_dir, link):
    info = SimpleNamespace(st_mode=mode, st_file_attributes=attributes)
    assert platform.is_plain_file(info) is plain_file  # type: ignore[arg-type]
    assert platform.is_plain_dir(info) is plain_dir  # type: ignore[arg-type]
    assert platform.is_link_like(info) is link  # type: ignore[arg-type]
    # A POSIX stat result has no attributes at all.
    assert platform.is_plain_file(SimpleNamespace(st_mode=mode)) is (mode == stat.S_IFREG)  # type: ignore[arg-type]


def test_a_real_file_folder_and_link_are_told_apart(tmp_path):
    (tmp_path / "file").write_text("x", encoding="utf-8")
    (tmp_path / "folder").mkdir()
    assert platform.is_plain_file((tmp_path / "file").lstat())
    assert platform.is_plain_dir((tmp_path / "folder").lstat())
    assert not platform.is_link_like((tmp_path / "folder").lstat())
    try:
        (tmp_path / "link").symlink_to(tmp_path / "file")
    except OSError:
        return  # creating links needs a privilege on Windows: the bits are checked above
    assert platform.is_link_like((tmp_path / "link").lstat())
    assert not platform.is_plain_file((tmp_path / "link").lstat())


@pytest.mark.parametrize(("system", "masked"), [("linux", [0o077]), ("macos", [0o077]), ("windows", [])])
def test_files_of_a_tow_process_are_private_on_linux_and_macos(monkeypatch, system, masked):
    set_to: list[int] = []
    monkeypatch.setattr(platform, "this_os", lambda: system)
    monkeypatch.setattr(os, "umask", lambda mask: set_to.append(mask) or 0o022)
    assert platform.use_private_files() is bool(masked)
    assert set_to == masked


def test_every_tow_command_starts_with_private_files(monkeypatch, capsys):
    from tow import cli

    called: list[bool] = []
    monkeypatch.setattr(platform, "use_private_files", lambda: called.append(True) or True)
    assert cli.main(["version"]) == 0
    assert called == [True]


# --- Windows: the machine -----------------------------------------------------------------------


def _kernel32(*, since_boot_ms: int, awake_sec: float, ok: bool = True) -> SimpleNamespace:
    def tick_count():
        return since_boot_ms

    def unbiased(ref):
        ref._obj.value = int(awake_sec * 1e7)
        return 1 if ok else 0

    return SimpleNamespace(GetTickCount64=tick_count, QueryUnbiasedInterruptTime=unbiased)


def _wtsapi32(logon_filetime: int | None, keep: list[Any]) -> SimpleNamespace:
    freed: list[int] = []

    def query(_server, _session, info_class, buffer_ref, size_ref):
        assert info_class == 24  # WTSSessionInfo
        if logon_filetime is None:
            return 0
        raw = bytes(176) + struct.pack("<5q", 0, 0, 0, logon_filetime, 0)
        block = ctypes.create_string_buffer(raw, len(raw))
        keep.append(block)
        buffer_ref._obj.value = ctypes.addressof(block)
        size_ref._obj.value = len(raw)
        return 1

    return SimpleNamespace(WTSQuerySessionInformationW=query, WTSFreeMemory=lambda buf: freed.append(buf), freed=freed)


@pytest.mark.skipif(not hasattr(ctypes, "WinDLL"), reason="ctypes.wintypes needs Windows")
def test_windows_reads_boot_sleep_and_logon(monkeypatch):
    keep: list[Any] = []
    logon_unix = T0 - 2 * HOUR
    filetime = int(logon_unix * 1e7) + windows.FILETIME_UNIX_EPOCH
    wts = _wtsapi32(filetime, keep)
    kernel32 = _kernel32(since_boot_ms=10 * HOUR * 1000, awake_sec=8 * HOUR)
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(kernel32=kernel32, wtsapi32=wts), raising=False)
    backend = WindowsBackend()

    assert backend.boot_time(T0) == pytest.approx(T0 - 10 * HOUR)
    assert backend.asleep_seconds() == pytest.approx(2 * HOUR)
    assert backend.logon_time() == pytest.approx(logon_unix)
    assert len(wts.freed) == 1  # the WTS buffer is always freed


def test_windows_without_unbiased_time_or_win32_knows_nothing(monkeypatch):
    kernel32 = _kernel32(since_boot_ms=1000, awake_sec=1, ok=False)
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(kernel32=kernel32), raising=False)
    assert WindowsBackend().boot_time(T0) is None
    assert WindowsBackend().asleep_seconds() is None
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(), raising=False)  # no kernel32
    assert windows.uptime() is None
    monkeypatch.delattr(ctypes, "windll", raising=False)  # Linux, macOS
    assert windows.uptime() is None
    assert windows.logon_time() is None
    assert windows.process_alive(4) is False


def test_windows_never_reports_negative_sleep(monkeypatch):
    kernel32 = _kernel32(since_boot_ms=1000, awake_sec=5)
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(kernel32=kernel32, wtsapi32=SimpleNamespace()), raising=False)
    assert WindowsBackend().asleep_seconds() == 0.0
    assert WindowsBackend().logon_time() is None  # wtsapi32 unavailable: unknown, not an error


@pytest.mark.skipif(not hasattr(ctypes, "WinDLL"), reason="ctypes.wintypes needs Windows")
@pytest.mark.parametrize("filetime", [None, 0, windows.FILETIME_UNIX_EPOCH])
def test_windows_logon_time_unknown_when_windows_does_not_say(monkeypatch, filetime):
    keep: list[Any] = []
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(wtsapi32=_wtsapi32(filetime, keep)), raising=False)
    assert windows.logon_time() is None


# --- Windows: event log, commands ---------------------------------------------------------------

_NS = "xmlns='http://schemas.microsoft.com/win/2004/08/events/event'"


def test_run_quiet_returns_output_only_for_a_clean_exit(monkeypatch):
    seen: list[dict[str, Any]] = []

    def run(args, **kwargs):
        seen.append({"args": args, **kwargs})
        return subprocess.CompletedProcess(args, 0 if args[0] == "ok" else 1, stdout="text", stderr="")

    monkeypatch.setattr(_common.subprocess, "run", run)
    assert windows._run(["ok"], timeout=3) == "text"
    assert windows._run(["fails"]) == ""
    assert seen[0]["timeout"] == 3
    assert seen[0]["check"] is False
    assert seen[0]["creationflags"] == windows.CREATE_NO_WINDOW
    assert posix._run(["ok"]) == "text"
    assert seen[-1]["creationflags"] == 0

    def missing(*_a, **_k):
        raise FileNotFoundError("wevtutil")

    monkeypatch.setattr(_common.subprocess, "run", missing)
    assert windows._run(["wevtutil"]) == ""

    def slow(args, **_k):
        raise subprocess.TimeoutExpired(args, 20)

    monkeypatch.setattr(_common.subprocess, "run", slow)
    assert windows._run(["wevtutil"]) == ""


def test_windows_shutdown_reasons_query_the_system_log_for_the_window(monkeypatch):
    asked: list[list[str]] = []
    raw = (
        f"<Event {_NS}><System><EventID>6008</EventID>"
        "<TimeCreated SystemTime='2026-09-12T11:02:11.2667912Z'/></System></Event>"
    )
    monkeypatch.setattr(windows, "_run", lambda args, timeout=20: asked.append(args) or raw)

    events = WindowsBackend().shutdown_reasons(T0, T0 + HOUR)

    assert [e["id"] for e in events] == [6008]
    args = asked[0]
    assert args[:3] == ["wevtutil", "qe", "System"]
    query = next(a for a in args if a.startswith("/q:"))
    assert "EventID=1074 or EventID=41 or EventID=6008" in query
    assert "2026-09-21T" in query  # T0 in UTC
    assert "/f:xml" in args


def test_damaged_event_records_are_skipped():
    raw = (
        f"<Event {_NS}><Other/></Event>"  # no System element
        f"<Event {_NS}><System><EventID>abc</EventID>"
        "<TimeCreated SystemTime='2026-09-12T11:02:11Z'/></System></Event>"
        f"<Event {_NS}><System><EventID>41</EventID></System></Event>"  # no TimeCreated
        f"<Event {_NS}><System><EventID>41</EventID>"
        "<TimeCreated SystemTime='2026-09-12T11:02:11Z'/></System></Event>"
    )
    events = windows.parse_events(raw)
    assert [(e["id"], e["process"], e["reason"], e["action"]) for e in events] == [(41, "", "", "")]
    assert windows.parse_events("<broken") == []


def test_a_recorded_shutdown_event_is_read_with_its_details():
    raw = (
        f"<Event {_NS}><System><EventID>1074</EventID>"
        "<TimeCreated SystemTime='2026-09-12T11:02:11.0000000Z'/></System><EventData>"
        "<Data Name='param1'>C:\\WINDOWS\\system32\\shutdown.exe (PC)</Data>"
        "<Data Name='param3'>Other (Unplanned)</Data><Data Name='param5'>restart</Data>"
        "</EventData></Event>"
    )
    (event,) = windows.parse_events(raw)
    assert event["process"].endswith("shutdown.exe (PC)")
    assert (event["reason"], event["action"]) == ("Other (Unplanned)", "restart")


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (json.dumps({"pid": 7, "cmd": "tow serve", "parent": 3, "parent_cmd": "tow.exe"}), "dict"),
        ("", None),
        ("   ", None),
        ("{broken", None),
        (json.dumps({"pid": None, "cmd": ""}), None),
        (json.dumps([1, 2]), None),
    ],
)
def test_windows_port_owner_reads_powershell_json(monkeypatch, output, expected):
    scripts: list[list[str]] = []
    monkeypatch.setattr(windows, "_run", lambda args, timeout=20: scripts.append(args) or output)
    found = WindowsBackend().port_owner(8787)
    assert found == (json.loads(output) if expected == "dict" else None)
    args = scripts[0]
    assert args[:4] == ["powershell", "-NoProfile", "-NonInteractive", "-Command"]
    assert "-LocalPort 8787 " in args[4]


# --- Windows: processes -------------------------------------------------------------------------


def test_windows_terminate_stops_the_whole_tree(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(
        windows.subprocess, "run", lambda args, **_k: calls.append(args) or SimpleNamespace(returncode=0)
    )
    monkeypatch.setattr(windows, "process_alive", lambda _pid: False)
    assert WindowsBackend().terminate(60, timeout=1) is True
    assert calls == [["taskkill", "/PID", "60", "/T", "/F"]]

    monkeypatch.setattr(windows.subprocess, "run", lambda args, **_k: SimpleNamespace(returncode=128))
    assert WindowsBackend().terminate(60) is False

    def boom(*_a, **_k):
        raise OSError("access denied")

    monkeypatch.setattr(windows.subprocess, "run", boom)
    assert WindowsBackend().terminate(60) is False
    monkeypatch.setattr(windows.subprocess, "run", lambda *_a, **_k: pytest.fail("bad pid reached taskkill"))
    assert WindowsBackend().terminate("not-a-pid") is False  # type: ignore[arg-type]
    assert WindowsBackend().terminate(0) is False


def test_windows_terminate_waits_for_the_process_to_go(monkeypatch):
    monkeypatch.setattr(windows.subprocess, "run", lambda *_a, **_k: SimpleNamespace(returncode=0))
    monkeypatch.setattr(windows, "process_alive", lambda _pid: True)  # survives taskkill
    monkeypatch.setattr(_common.time, "sleep", lambda _s: None)
    assert WindowsBackend().terminate(60, timeout=0) is False


class _Kernel32Processes:
    def __init__(self, *, handle: int, last_error: int = 0, code: int = 259, ok: bool = True) -> None:
        self.handle, self.last_error, self.code, self.ok = handle, last_error, code, ok
        self.closed: list[int] = []

    def OpenProcess(self, access, inherit, pid):
        assert access == 0x1000
        assert inherit is False
        return self.handle

    def GetLastError(self):
        return self.last_error

    def GetExitCodeProcess(self, handle, ref):
        ref._obj.value = self.code
        return 1 if self.ok else 0

    def CloseHandle(self, handle):
        self.closed.append(getattr(handle, "value", handle))


@pytest.mark.parametrize(
    ("kernel", "alive"),
    [
        (_Kernel32Processes(handle=5), True),
        (_Kernel32Processes(handle=5, code=0), False),  # exited
        (_Kernel32Processes(handle=5, ok=False), False),
        (_Kernel32Processes(handle=0, last_error=87), False),  # no such process
        (_Kernel32Processes(handle=0, last_error=5), True),  # another user's
    ],
)
def test_windows_process_alive_asks_the_kernel_never_kills(monkeypatch, kernel, alive):
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(kernel32=kernel), raising=False)
    monkeypatch.setattr(windows.os, "kill", lambda *_a: pytest.fail("os.kill terminates on Windows"))
    assert WindowsBackend().process_alive(1234) is alive
    assert kernel.closed == ([5] if kernel.handle else [])
    assert WindowsBackend().process_alive(0) is False


class _FakePopen:
    calls: ClassVar[list[dict[str, Any]]] = []
    refuse_breakaway = False

    def __init__(self, args, **kwargs):
        if self.refuse_breakaway and kwargs["creationflags"] & windows.CREATE_BREAKAWAY_FROM_JOB:
            raise PermissionError("job forbids breakaway")
        type(self).calls.append({"args": args, **kwargs})
        self.pid = 4321


def test_windows_spawn_detached_is_hidden_and_leaves_the_job(monkeypatch, tmp_path):
    _FakePopen.calls = []
    _FakePopen.refuse_breakaway = False
    monkeypatch.setattr(windows.subprocess, "Popen", _FakePopen)
    log = tmp_path / "logs" / "run.log"

    assert WindowsBackend().spawn_detached(["tow.exe", "serve"], log_path=log, cwd=tmp_path) == 4321

    call = _FakePopen.calls[0]
    assert call["args"] == ["tow.exe", "serve"]
    flags = call["creationflags"]
    assert flags & windows.CREATE_NO_WINDOW
    assert flags & windows.CREATE_NEW_PROCESS_GROUP
    assert flags & windows.CREATE_BREAKAWAY_FROM_JOB
    assert call["stdin"] is subprocess.DEVNULL
    assert call["close_fds"] is True
    assert call["cwd"] == str(tmp_path)
    assert log.parent.is_dir()

    _FakePopen.calls = []
    _FakePopen.refuse_breakaway = True
    assert WindowsBackend().spawn_detached(["tow.exe"], hidden=False) == 4321
    flags = _FakePopen.calls[0]["creationflags"]
    assert flags & windows.CREATE_NEW_CONSOLE
    assert not flags & windows.CREATE_BREAKAWAY_FROM_JOB
    assert _FakePopen.calls[0]["stdout"] is subprocess.DEVNULL


def test_windows_popen_options():
    assert WindowsBackend().popen_options() == {"creationflags": windows.CREATE_NO_WINDOW, "close_fds": True}
    options = WindowsBackend().popen_options(new_group=True, hidden=False)
    assert options["creationflags"] == windows.CREATE_NEW_PROCESS_GROUP


# --- Windows: the browser window ----------------------------------------------------------------


class FakeUser32:
    def __init__(self, windows_by_handle: dict[int, int]) -> None:
        self.windows = windows_by_handle  # hwnd -> owning process id
        self.shown: list[tuple] = []

    def EnumWindows(self, callback, _lparam):
        for hwnd in self.windows:
            callback(hwnd, 0)

    def GetWindowThreadProcessId(self, hwnd, owner):
        owner.value = self.windows[hwnd]

    def IsWindow(self, _hwnd):
        return True

    def ShowWindow(self, hwnd, how):
        self.shown.append(("show", hwnd, how))

    def SetForegroundWindow(self, hwnd):
        self.shown.append(("front", hwnd))


def _fake_ctypes(user32: Any) -> SimpleNamespace:
    class Ulong:
        def __init__(self):
            self.value = 0

    return SimpleNamespace(
        windll=SimpleNamespace(user32=user32),
        WINFUNCTYPE=lambda *_types: lambda fn: fn,
        c_bool=bool,
        c_void_p=int,
        c_ulong=Ulong,
        byref=lambda value: value,
    )


def test_the_browser_window_is_brought_to_the_front(monkeypatch):
    user32 = FakeUser32({11: 1, 22: 4242})
    monkeypatch.setattr(windows, "ctypes", _fake_ctypes(user32))
    WindowsBackend().bring_to_front(4242)
    assert user32.shown == [("show", 22, 5), ("front", 22)]


def test_a_window_that_never_appears_is_given_up_quietly(monkeypatch):
    user32 = FakeUser32({11: 1})
    sleeps: list[float] = []
    monkeypatch.setattr(windows, "ctypes", _fake_ctypes(user32))
    monkeypatch.setattr(windows, "time", SimpleNamespace(sleep=sleeps.append))
    WindowsBackend().bring_to_front(4242)
    assert user32.shown == []
    assert len(sleeps) == 20


def test_window_handling_without_win32_does_nothing(monkeypatch):
    monkeypatch.setattr(windows, "ctypes", SimpleNamespace())  # no windll, no WINFUNCTYPE
    WindowsBackend().bring_to_front(1)
    PosixBackend("linux").bring_to_front(1)


# --- Windows: browsers and folders --------------------------------------------------------------


def test_windows_finds_edge_then_chrome_in_their_install_folders(monkeypatch, tmp_path):
    for name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        monkeypatch.setenv(name, str(tmp_path / name.lower()))
    edge = tmp_path / "localappdata" / "Microsoft/Edge/Application/msedge.exe"
    chrome = tmp_path / "programfiles" / "Google/Chrome/Application/chrome.exe"
    for exe in (edge, chrome):
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"")
    monkeypatch.setattr(windows.shutil, "which", lambda name: r"C:\bin\chrome.exe" if name == "chrome.exe" else None)

    assert WindowsBackend().browser_executables() == [str(edge), str(chrome), r"C:\bin\chrome.exe"]


def test_windows_protected_folders_come_from_the_profile_and_the_defaults(monkeypatch):
    for name in windows._PROTECTED_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("USERPROFILE", r"D:\Users\user")
    monkeypatch.setenv("SYSTEMDRIVE", "D:")
    monkeypatch.setenv("APPDATA", r"D:\Users\user\AppData\Roaming")
    roots = WindowsBackend().protected_folders()
    assert r"D:\Users\user\AppData" in roots
    assert r"D:\Users\user\AppData\Roaming" in roots
    assert r"D:\Windows" in roots
    assert r"D:\Program Files (x86)" in roots


# --- Linux and macOS: the machine ---------------------------------------------------------------


def test_linux_boot_time_comes_from_proc_uptime(monkeypatch):
    monkeypatch.setattr(posix, "_read", lambda path: "36000.25 70000.00\n" if path == "/proc/uptime" else "")
    assert PosixBackend("linux").boot_time(T0) == pytest.approx(T0 - 36000.25)
    monkeypatch.setattr(posix, "_read", lambda _path: "")
    assert PosixBackend("linux").boot_time(T0) is None


def test_macos_boot_time_comes_from_sysctl(monkeypatch):
    asked: list[list[str]] = []
    out = "{ sec = 1789990000, usec = 512345 } Fri Sep 21 12:00:00 2026\n"
    monkeypatch.setattr(posix, "_run", lambda args, timeout=20: asked.append(args) or out)
    assert PosixBackend("macos").boot_time() == 1789990000.0
    assert asked == [["sysctl", "-n", "kern.boottime"]]
    monkeypatch.setattr(posix, "_run", lambda *_a, **_k: "")
    assert PosixBackend("macos").boot_time() is None


@pytest.mark.parametrize(
    ("name", "clocks", "asleep"),
    [
        ("linux", {"CLOCK_BOOTTIME": 5000.0, "CLOCK_MONOTONIC": 4000.0}, 1000.0),
        ("macos", {"CLOCK_MONOTONIC": 5000.0, "CLOCK_UPTIME_RAW": 4500.0}, 500.0),
        ("linux", {"CLOCK_BOOTTIME": 4000.0, "CLOCK_MONOTONIC": 4000.5}, 0.0),  # never negative
        ("linux", {"CLOCK_MONOTONIC": 4000.0}, None),  # an old kernel without CLOCK_BOOTTIME
    ],
)
def test_time_asleep_is_the_difference_of_two_clocks(monkeypatch, name, clocks, asleep):
    monkeypatch.setattr(posix, "_clock", clocks.get)
    assert PosixBackend(name).asleep_seconds() == asleep


def test_logon_time_is_unknown_on_linux_and_macos():
    assert PosixBackend("linux").logon_time() is None
    assert PosixBackend("macos").logon_time() is None


def test_linux_shutdown_reasons_are_the_ends_of_earlier_boots(monkeypatch):
    boots = [
        {"index": -2, "boot_id": "a", "first_entry": 1, "last_entry": int((T0 - 5 * HOUR) * 1e6)},
        {"index": -1, "boot_id": "b", "first_entry": 2, "last_entry": int((T0 + HOUR) * 1e6)},
        {"index": 0, "boot_id": "c", "first_entry": 3, "last_entry": int((T0 + 2 * HOUR) * 1e6)},
        {"index": -3, "boot_id": "d"},  # damaged
        "junk",
    ]
    asked: list[list[str]] = []
    monkeypatch.setattr(posix, "_run", lambda args, timeout=20: asked.append(args) or json.dumps(boots))

    events = PosixBackend("linux").shutdown_reasons(T0, T0 + 3 * HOUR)

    assert asked[0][:3] == ["journalctl", "--list-boots", "-o"]
    assert events == [{"id": "stopped", "ts": T0 + HOUR, "process": "", "reason": "", "action": ""}]
    for raw in ("", "not json", "{}"):
        assert posix.linux_boot_ends(raw, T0, T0 + HOUR) == []
    monkeypatch.setattr(posix, "_run", lambda *_a, **_k: pytest.fail("macOS has no journal to ask"))
    assert PosixBackend("macos").shutdown_reasons(T0, T0 + HOUR) == []


# --- Linux and macOS: processes -----------------------------------------------------------------


class _Signals:
    """A fake process table: which pids live, what signals they got, how they react."""

    def __init__(self, alive: set[int], *, groups: dict[int, int] | None = None, stubborn: bool = False) -> None:
        self.alive, self.groups, self.stubborn = alive, groups or {}, stubborn
        self.sent: list[tuple[str, int, int]] = []

    def install(self, monkeypatch) -> None:
        monkeypatch.setattr(posix, "_signal", self.signal)
        monkeypatch.setattr(posix, "_signal_group", self.signal_group)
        monkeypatch.setattr(posix, "_group_of", lambda pid: self.groups.get(pid))
        monkeypatch.setattr(posix, "_reap", lambda _pid: False)
        monkeypatch.setattr(_common.time, "sleep", lambda _s: None)

    def signal(self, pid: int, sig: int) -> None:
        if pid not in self.alive:
            raise ProcessLookupError(pid)
        if sig == 0:
            return
        self.sent.append(("pid", pid, sig))
        self._hit(pid, sig)

    def signal_group(self, pgid: int, sig: int) -> None:
        self.sent.append(("group", pgid, sig))
        self._hit(pgid, sig)

    def _hit(self, pid: int, sig: int) -> None:
        if not self.stubborn or sig == posix._KILL:
            self.alive.discard(pid)


def test_posix_process_alive_uses_signal_zero(monkeypatch):
    table = _Signals({10})
    table.install(monkeypatch)
    backend = PosixBackend("linux")
    assert backend.process_alive(10) is True
    assert backend.process_alive(11) is False
    assert backend.process_alive(0) is False

    def denied(_pid, _sig):
        raise PermissionError

    monkeypatch.setattr(posix, "_signal", denied)
    assert backend.process_alive(12) is True  # exists, owned by someone else
    monkeypatch.setattr(posix, "_reap", lambda _pid: True)
    assert backend.process_alive(12) is False  # our child, exited and collected


def test_posix_terminate_signals_the_group_it_leads(monkeypatch):
    table = _Signals({20}, groups={20: 20})
    table.install(monkeypatch)
    assert PosixBackend("linux").terminate(20, timeout=1) is True
    assert table.sent == [("group", 20, signal.SIGTERM)]


def test_posix_terminate_escalates_to_kill(monkeypatch):
    table = _Signals({30}, groups={30: 1}, stubborn=True)
    table.install(monkeypatch)
    assert PosixBackend("macos").terminate(30, timeout=0) is True
    assert table.sent == [("pid", 30, signal.SIGTERM), ("pid", 30, posix._KILL)]


def test_posix_terminate_of_a_gone_or_bad_pid(monkeypatch):
    _Signals(set()).install(monkeypatch)
    assert PosixBackend("linux").terminate(40) is True  # already gone
    assert PosixBackend("linux").terminate(-1) is False

    def refused(_pid, _sig):
        raise PermissionError

    monkeypatch.setattr(posix, "_signal", refused)
    assert PosixBackend("linux").terminate(41) is False


def test_posix_spawn_detached_starts_a_new_session(monkeypatch, tmp_path):
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        posix.subprocess,
        "Popen",
        lambda args, **kwargs: calls.append({"args": args, **kwargs}) or SimpleNamespace(pid=77),
    )
    log = tmp_path / "logs" / "run.log"
    assert PosixBackend("linux").spawn_detached(["tow", "serve"], log_path=log, env={"A": "1"}) == 77
    call = calls[0]
    assert call["start_new_session"] is True
    assert call["close_fds"] is True
    assert call["stdin"] is subprocess.DEVNULL
    assert call["env"] == {"A": "1"}
    assert "creationflags" not in call
    assert PosixBackend("macos").popen_options(new_group=True) == {"start_new_session": True, "close_fds": True}


def test_posix_port_owner_reads_lsof_and_ps(monkeypatch):
    outputs = {
        "lsof": "p4321\nf7\n",
        "ps 4321": "  100 /srv/TOW/app/.venv/bin/python /srv/TOW/app/.venv/bin/tow serve\n",
        "ps 100": "    1 /srv/TOW/app/.venv/bin/python -m tow run\n",
    }

    def run(args, timeout=20):
        key = "lsof" if args[0] == "lsof" else f"ps {args[-1]}"
        return outputs.get(key, "")

    monkeypatch.setattr(posix, "_run", run)
    monkeypatch.setattr(posix, "_which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(posix, "_read", lambda _path: "")  # never this machine's /proc
    assert PosixBackend("linux").port_owner(8787) == {
        "pid": 4321,
        "cmd": "/srv/TOW/app/.venv/bin/python /srv/TOW/app/.venv/bin/tow serve",
        "parent": 100,
        "parent_cmd": "/srv/TOW/app/.venv/bin/python -m tow run",
    }
    outputs["ps 4321"] = ""
    assert PosixBackend("linux").port_owner(8787) == {"pid": 4321, "cmd": "", "parent": None, "parent_cmd": ""}
    outputs["lsof"] = ""
    assert PosixBackend("linux").port_owner(8787) is None
    assert posix.parse_ps("abc def") is None


def test_without_lsof_linux_asks_ss(monkeypatch):
    """Minimal Debian and Fedora have no lsof; iproute2's ss is there."""
    asked: list[list[str]] = []
    listing = (
        'LISTEN 0      2048       127.0.0.1:87870      0.0.0.0:*    users:(("other",pid=1,fd=3))\n'
        'LISTEN 0      2048       127.0.0.1:8787       0.0.0.0:*    users:(("tow",pid=4321,fd=6))\n'
    )

    def run(args, timeout=20):
        asked.append(args)
        if args[0] == "ss":
            return listing
        return "  100 /srv/TOW/app/.venv/bin/tow serve\n" if args[-1] == "4321" else "    1 tow run\n"

    monkeypatch.setattr(posix, "_run", run)
    monkeypatch.setattr(posix, "_which", lambda name: "/usr/sbin/ss" if name == "ss" else None)
    owner = PosixBackend("linux").port_owner(8787)
    assert owner == {"pid": 4321, "cmd": "/srv/TOW/app/.venv/bin/tow serve", "parent": 100, "parent_cmd": "tow run"}
    assert asked[0] == ["ss", "-Hltnp", "sport", "=", ":8787"]
    assert posix.parse_ss_pid("LISTEN 0 4096 *:8787 *:*\n", 8787) is None  # someone else's: ss does not say
    assert PosixBackend("macos").port_owner(8787) is None  # macOS always has lsof; nothing else is guessed


def test_without_lsof_and_ss_linux_reads_proc(monkeypatch):
    tcp = (
        "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
        "   0: 0100007F:2253 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 55501 1\n"
        "   1: 0100007F:2253 0100007F:D3F2 01 00000000:00000000 00:00000000 00000000  1000        0 55600 1\n"
    )
    files = {
        "/proc/net/tcp": tcp,
        "/proc/4321/stat": "4321 (tow serve) S 100 4321 4321 0 -1",
        "/proc/4321/cmdline": "/srv/TOW/app/.venv/bin/python\0-m\0tow\0serve\0",
        "/proc/100/stat": "100 (python) S 1 100 100 0 -1",
        "/proc/100/cmdline": "python\0-m\0tow\0run\0",
    }
    folders = {"/proc": ["self", "1", "4321"], "/proc/1/fd": ["0"], "/proc/4321/fd": ["0", "6"]}
    links = {"/proc/1/fd/0": "/dev/null", "/proc/4321/fd/6": "socket:[55501]"}
    monkeypatch.setattr(posix, "_which", lambda _name: None)
    monkeypatch.setattr(posix, "_run", lambda *_a, **_k: "")  # no ps either
    monkeypatch.setattr(posix, "_read", lambda path: files.get(path, ""))
    monkeypatch.setattr(posix, "_listdir", lambda path: folders.get(path, []))
    monkeypatch.setattr(posix, "_readlink", lambda path: links.get(path, ""))

    assert PosixBackend("linux").port_owner(8787) == {  # 0x2253
        "pid": 4321,
        "cmd": "/srv/TOW/app/.venv/bin/python -m tow serve",
        "parent": 100,
        "parent_cmd": "python -m tow run",
    }
    assert PosixBackend("linux").port_owner(8788) is None
    assert posix.proc_listening_inodes(tcp, 8787) == {"55501"}  # the connected socket is not a listener


# --- Linux and macOS: browsers and folders ------------------------------------------------------


def test_linux_browsers_come_from_path_in_order(monkeypatch):
    asked: list[str] = []
    found = {"chromium": "/usr/bin/chromium", "microsoft-edge": "/usr/bin/microsoft-edge"}
    monkeypatch.setattr(posix, "_which", lambda name: asked.append(name) or found.get(name))
    assert PosixBackend("linux").browser_executables() == ["/usr/bin/chromium", "/usr/bin/microsoft-edge"]
    assert asked[:4] == ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"]


def test_macos_browsers_come_from_the_applications_folders(monkeypatch):
    present = {
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Users/user/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    }
    monkeypatch.setattr(posix, "_is_file", present.__contains__)
    monkeypatch.setattr(posix, "_which", lambda _name: None)
    assert PosixBackend("macos", home="/Users/user").browser_executables() == [
        "/Users/user/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ]


def test_a_snap_chromium_comes_after_any_other_browser(monkeypatch):
    found = {"chromium-browser": "/usr/bin/chromium-browser", "microsoft-edge": "/usr/bin/microsoft-edge"}
    monkeypatch.setattr(posix, "_which", found.get)
    monkeypatch.setattr(posix, "_realpath", lambda path: path)
    scripts = {"/usr/bin/chromium-browser": b'#! /bin/sh\nexec /snap/bin/chromium "$@"\n'}
    monkeypatch.setattr(posix, "_head", lambda path, size=4096: scripts.get(path, b"\x7fELF"))
    assert PosixBackend("linux").browser_executables() == ["/usr/bin/microsoft-edge", "/usr/bin/chromium-browser"]
    assert posix.is_snap_browser("/snap/bin/chromium")
    assert posix.is_snap_browser("/usr/bin/chromium-browser")  # Ubuntu's script that starts the snap
    assert not posix.is_snap_browser("/usr/bin/microsoft-edge")
    monkeypatch.setattr(posix, "_realpath", lambda path: "/snap/chromium/3000/usr/lib/chromium-browser/chrome")
    assert posix.is_snap_browser("/usr/bin/chromium")  # a link into the snap


def test_the_sign_in_browser_keeps_home_and_keyring_in_its_session_folder(monkeypatch, tmp_path):
    asked: list[list[str]] = []
    session = "DISPLAY=:0\nXAUTHORITY=/run/user/1000/gdm/Xauthority\nPATH=/usr/bin\nLANG=C\n"
    monkeypatch.setattr(posix, "_run", lambda args, timeout=20: asked.append(args) or session)
    monkeypatch.setattr(posix, "_head", lambda _path, size=4096: b"")
    for name in ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", "/home/x/.config")
    home = tmp_path / "op" / "home"

    launch = PosixBackend("linux", home="/home/x").browser_launch("/usr/bin/google-chrome", home)

    assert launch["args"] == ["--password-store=basic", "--use-mock-keychain"]
    assert launch["confined"] is False
    env = launch["env"]
    assert env["HOME"] == str(home)
    assert env["XDG_CONFIG_HOME"] == str(home / ".config")  # not the owner's ~/.config
    assert env["XDG_CACHE_HOME"] == str(home / ".cache")  # fontconfig's cache
    assert (env["DISPLAY"], env["XAUTHORITY"]) == (":0", "/run/user/1000/gdm/Xauthority")  # from the user manager
    assert env["PATH"] == os.environ.get("PATH")  # only the display variables are taken
    assert asked == [["systemctl", "--user", "show-environment"]]

    monkeypatch.setenv("DISPLAY", ":1")  # a desktop session: nothing is asked, nothing replaced
    monkeypatch.setattr(posix, "_is_file", lambda path: path == "/home/x/.Xauthority")
    asked.clear()
    env = PosixBackend("linux", home="/home/x").browser_launch("/usr/bin/google-chrome", home)["env"]
    assert asked == []
    assert env["DISPLAY"] == ":1"
    assert env["XAUTHORITY"] == "/home/x/.Xauthority"  # X11's cookie stays where it is, not in the new HOME

    mac = PosixBackend("macos", home="/Users/x").browser_launch("/Applications/Google Chrome.app/x", home)
    assert asked == []
    assert mac["args"] == ["--password-store=basic", "--use-mock-keychain"]
    assert mac["env"]["HOME"] == str(home)
    assert WindowsBackend().browser_launch("msedge.exe", home) == {"args": [], "env": None, "confined": False}


def test_posix_protected_folders_include_the_home_folders_programs_start_from(monkeypatch):
    roots = PosixBackend("linux", home="/home/user").protected_folders()
    for folder in ("/etc", "/usr", "/bin", "/sbin", "/boot", "/proc", "/dev", "/System", "/Library", "/var/spool"):
        assert folder in roots
    for folder in ("/home/user/.ssh", "/home/user/.config", "/home/user/Library"):
        assert folder in roots
    assert "/var" not in roots  # download folders live under /var (transmission-daemon)
    monkeypatch.setenv("HOME", "relative/home")
    assert not any(root.startswith("relative") for root in PosixBackend("macos").protected_folders())


def test_open_url_uses_the_default_browser(monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(_common.webbrowser, "open", lambda url: opened.append(url) or True)
    assert WindowsBackend().open_url("http://127.0.0.1:8787") is True
    assert PosixBackend("macos").open_url("http://127.0.0.1:8787") is True
    assert opened == ["http://127.0.0.1:8787"] * 2

    def broken(_url):
        raise _common.webbrowser.Error("no browser")

    monkeypatch.setattr(_common.webbrowser, "open", broken)
    monkeypatch.setenv("DISPLAY", ":0")
    assert PosixBackend("linux").open_url("http://127.0.0.1:8787") is False


def test_linux_without_a_desktop_opens_no_text_browser(monkeypatch):
    # `tow start` over SSH: webbrowser would run lynx or w3m in the terminal and hold it.
    opened: list[str] = []
    monkeypatch.setattr(_common.webbrowser, "open", lambda url: opened.append(url) or True)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert PosixBackend("linux").open_url("http://127.0.0.1:8787") is False
    assert opened == []
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert PosixBackend("linux").open_url("http://127.0.0.1:8787") is True
    assert PosixBackend("macos").open_url("http://127.0.0.1:8787") is True  # macOS always has `open`


# --- children end with their parent (1.21) -----------------------------------------------------


def test_linux_children_get_the_parent_death_signal(monkeypatch):
    signals = []
    monkeypatch.setattr(posix, "_set_parent_death_signal", lambda sig: signals.append(sig) or True)
    monkeypatch.setattr(posix, "_parent_pid", lambda: 4242)
    linux = posix.PosixBackend("linux")
    assert linux.die_with_parent(4242) is True
    assert signals == [signal.SIGTERM]
    assert linux.die_with_parent(1) is False  # the parent was gone already (re-parented)
    assert linux.bind_children() is False  # nothing to do on the parent's side
    assert posix.PosixBackend("macos").die_with_parent(4242) is False  # no prctl: the thread watches
    monkeypatch.setattr(posix, "_set_parent_death_signal", lambda sig: False)
    assert linux.die_with_parent(4242) is False


class _Fn:
    """A kernel32 function: callable, and ``restype`` can be set on it."""

    def __init__(self, body):
        self.body = body

    def __call__(self, *args):
        return self.body(*args)


class _JobKernel:
    def __init__(self, *, assign=True):
        self.assign = assign
        self.calls = []
        self.CreateJobObjectW = _Fn(self._create)
        self.GetCurrentProcess = _Fn(lambda: 0xFFFF)
        self.SetInformationJobObject = _Fn(self._set)
        self.AssignProcessToJobObject = _Fn(self._assign)
        self.CloseHandle = _Fn(lambda handle: self.calls.append(("close",)))

    def _create(self, *_args):
        self.calls.append(("create",))
        return 0x1234

    def _set(self, job, info_class, info, size):
        flags = info._obj.BasicLimitInformation.LimitFlags
        self.calls.append(("set", info_class, flags, size))
        return 1

    def _assign(self, job, process):
        self.calls.append(("assign",))
        return int(self.assign)


def test_windows_children_end_with_the_supervisor_through_a_job_object(monkeypatch):
    kernel = _JobKernel()
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(kernel32=kernel), raising=False)
    monkeypatch.setattr(windows, "_job", None)
    assert windows.bind_children() is True
    _create, setting, assign = kernel.calls
    assert setting[:3] == ("set", 9, 0x2000 | 0x0800)  # kill on close; breakaway still allowed
    assert assign == ("assign",)
    assert windows.bind_children() is True  # once
    assert len(kernel.calls) == 3
    assert windows.WindowsBackend().die_with_parent(1) is False  # the job does it


def test_a_job_object_that_cannot_be_used_is_closed_and_said(monkeypatch):
    kernel = _JobKernel(assign=False)
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(kernel32=kernel), raising=False)
    monkeypatch.setattr(windows, "_job", None)
    assert windows.bind_children() is False
    assert kernel.calls[-1] == ("close",)
    monkeypatch.delattr(ctypes, "windll", raising=False)  # Linux, macOS
    assert windows.bind_children() is False


# --- the rest of TOW asks tow.platform ---------------------------------------------------------


def test_is_windows_follows_the_backend_in_use():
    with platform.use(WindowsBackend()):
        assert platform.is_windows() is True
    with platform.use(PosixBackend("linux")):
        assert platform.is_windows() is False


def test_pages_and_the_cli_show_the_commands_of_the_backend_in_use(capsys):
    from tow.cli import _cmd_update
    from tow.web.templating import TEMPLATES

    launcher = TEMPLATES.env.globals["os_launcher"]
    with platform.use(WindowsBackend()):
        assert launcher() == "scripts\\tow.cmd"
        _cmd_update(SimpleNamespace(ref="v9.9.9"))
        assert "deploy.ps1" in capsys.readouterr().out
    with platform.use(PosixBackend("linux")):
        assert launcher() == "scripts/tow"
        _cmd_update(SimpleNamespace(ref="v9.9.9"))
        assert "deploy.ps1" not in capsys.readouterr().out


def test_a_lock_file_byte_is_held_until_unlocked(tmp_path):
    from tow.platform import locks
    from tow.store import init_lock_file

    path = tmp_path / "x.lock"
    with path.open("a+b") as first, path.open("a+b") as second:
        init_lock_file(first)
        init_lock_file(second)
        assert locks.lock(first, wait=False) is True
        assert locks.lock(second, wait=False) is False
        locks.unlock(first)
        assert locks.lock(second, wait=False) is True
        locks.unlock(second)


# Modules that still ask the OS themselves (none since 1.21: the five-task code is gone and
# autostart.platform_name() asks platform.this_os()).
_OS_CHECK_EXCEPTIONS: set[str] = set()


def test_no_module_outside_tow_platform_checks_the_os_itself():
    import ast
    from pathlib import Path

    src = Path(platform.__file__).parents[1]
    found: list[str] = []
    for path in sorted(src.rglob("*.py")):
        rel = path.relative_to(src).as_posix()
        if rel.startswith("platform/") or rel in _OS_CHECK_EXCEPTIONS:
            continue
        found.extend(
            f"{rel}:{node.lineno}"
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and (node.value.id, node.attr) in {("os", "name"), ("sys", "platform")}
        )
    assert found == [], f"ask tow.platform (current(), is_windows()) instead: {found}"


@pytest.mark.parametrize("system", ["linux", "macos"])
@pytest.mark.parametrize("command", ["python synthetic-worker", "", None])
def test_posix_process_command_reads_identity_without_signals(monkeypatch, system, command):
    from tow.platform.posix import PosixBackend

    backend = PosixBackend(system)
    seen = []
    monkeypatch.setattr(
        backend, "_process", lambda pid: seen.append(pid) or (None if command is None else (1, command))
    )
    assert backend.process_command(123) == (command or None)
    assert seen == [123]
    for invalid in (0, -1, "invalid", None):
        assert backend.process_command(invalid) is None
    assert seen == [123]


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ('{"ProcessId":123,"CommandLine":"python synthetic-worker"}', "python synthetic-worker"),
        ('{"ProcessId":456,"CommandLine":"foreign"}', None),
        ('{"ProcessId":123,"CommandLine":null}', None),
        ('{"ProcessId":123,"CommandLine":[]}', None),
        ("[]", None),
        ("invalid", None),
        ("", None),
    ],
)
def test_windows_process_command_is_a_bounded_read_only_probe(monkeypatch, reply, expected):
    from tow.platform import windows

    calls = []
    monkeypatch.setattr(windows, "_run", lambda args, **kwargs: calls.append((args, kwargs)) or reply)
    assert windows.WindowsBackend().process_command(123) == expected
    assert calls[0][0][:4] == ["powershell", "-NoProfile", "-NonInteractive", "-Command"]
    assert "ProcessId=123" in calls[0][0][4]
    assert calls[0][1]["timeout"] == 5
    for invalid in (0, -1, "invalid", None):
        assert windows.process_command(invalid) is None
    assert len(calls) == 1


@pytest.mark.parametrize("system", ["linux", "macos"])
def test_posix_identity_probe_never_truncates_long_process_arguments(monkeypatch, system):
    from tow.platform import posix

    calls = []
    command = "python " + "a" * 500 + " synthetic-worker"
    monkeypatch.setattr(posix, "_run", lambda args, **kwargs: calls.append(args) or f"1 {command}")
    assert posix.PosixBackend(system).process_command(123) == command
    assert calls == [["ps", "-ww", "-o", "ppid=,command=", "-p", "123"]]
