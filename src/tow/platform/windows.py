"""Windows: facts Windows already keeps, and starting/stopping processes without a console.

- boot time and the time spent asleep since boot (kernel32: GetTickCount64 counts sleep,
  QueryUnbiasedInterruptTime does not), and when the owner signed in (wtsapi32);
- why the PC went down: the System event log (1074 - who restarted or powered off and why,
  41 / 6008 - it went down without a clean shutdown);
- processes: hidden, detached starts; the whole tree stopped with taskkill;
- Edge or Chrome for the browser sign-in; Windows, program and AppData folders;
- keys/ and data/ for this account, SYSTEM and Administrators only (icacls, read back).
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import ntpath
import os
import re
import shutil
import struct
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tow.platform import _common

FILETIME_UNIX_EPOCH = 116444736000000000
_EVENT_NS = "{http://schemas.microsoft.com/win/2004/08/events/event}"

CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NEW_CONSOLE = 0x00000010
CREATE_NO_WINDOW = 0x08000000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5


def _run(args: list[str], timeout: float = 20) -> str:
    return _common.run_quiet(args, timeout=timeout, creationflags=CREATE_NO_WINDOW)


def _missing(*_args: Any, **_kwargs: Any) -> Any:
    raise AttributeError("not available on this system")


def _dll(name: str) -> Any:
    """A Windows system library (AttributeError where there is none: Linux, macOS)."""
    loader = getattr(ctypes, "windll", None)
    if loader is None:
        raise AttributeError("ctypes.windll")
    return getattr(loader, name)


# --- the machine --------------------------------------------------------------------------------


def uptime() -> tuple[float, float] | None:
    """(seconds since boot, seconds awake since boot), or None."""
    try:
        kernel32 = _dll("kernel32")
        kernel32.GetTickCount64.restype = ctypes.c_ulonglong
        since_boot = kernel32.GetTickCount64() / 1000
        unbiased = ctypes.c_ulonglong()
        if not kernel32.QueryUnbiasedInterruptTime(ctypes.byref(unbiased)):
            return None
        awake = unbiased.value / 1e7
    except AttributeError, OSError, ValueError:
        return None
    return since_boot, awake


def ui_language() -> str | None:
    """The language of this user's Windows display ("ru-RU"), or None."""
    try:
        kernel32 = _dll("kernel32")
        buffer = ctypes.create_unicode_buffer(85)
        if not kernel32.LCIDToLocaleName(kernel32.GetUserDefaultUILanguage(), buffer, len(buffer), 0):
            return None
    except AttributeError, OSError, ValueError:
        return None
    return buffer.value or None


def logon_time() -> float | None:
    """When the current Windows session signed in (unix time), or None."""
    try:
        from ctypes import wintypes

        wts = _dll("wtsapi32")
        buffer = ctypes.c_void_p()
        size = wintypes.DWORD()
        # WTS_CURRENT_SERVER_HANDLE, WTS_CURRENT_SESSION, WTSSessionInfo
        if not wts.WTSQuerySessionInformationW(None, 0xFFFFFFFF, 24, ctypes.byref(buffer), ctypes.byref(size)):
            return None
        try:
            raw = ctypes.string_at(buffer, size.value)
        finally:
            wts.WTSFreeMemory(buffer)
        # WTSINFOW: 8 DWORDs, three WCHAR names (32+17+21), then 5 LARGE_INTEGERs (LogonTime is the 4th).
        logon = struct.unpack_from("<5q", raw, 176)[3]
    except AttributeError, OSError, ValueError, ImportError:
        return None
    return (logon - FILETIME_UNIX_EPOCH) / 1e7 if logon > FILETIME_UNIX_EPOCH else None


def shutdown_events(since_ts: float, until_ts: float) -> list[dict[str, Any]]:
    """Shutdown records of the System event log between two moments, oldest first."""
    since = datetime.fromtimestamp(since_ts, UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    until = datetime.fromtimestamp(until_ts, UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    query = (
        "*[System[(EventID=1074 or EventID=41 or EventID=6008)"
        f" and TimeCreated[@SystemTime>='{since}' and @SystemTime<='{until}']]]"
    )
    raw = _run(["wevtutil", "qe", "System", f"/q:{query}", "/c:20", "/rd:true", "/f:xml"])
    return parse_events(raw)


def parse_events(raw: str) -> list[dict[str, Any]]:
    """wevtutil's XML records as {id, ts, process, reason, action}, oldest first."""
    try:
        root = ET.fromstring(f"<events>{raw}</events>")
    except ET.ParseError:
        return []
    events = []
    for event in root:
        system = event.find(f"{_EVENT_NS}System")
        if system is None:
            continue
        created = system.find(f"{_EVENT_NS}TimeCreated")
        try:
            event_id = int(system.findtext(f"{_EVENT_NS}EventID") or 0)
            ts = datetime.fromisoformat(
                str((created.get("SystemTime") if created is not None else "") or "")
            ).timestamp()
        except ValueError:
            continue
        data = {item.get("Name") or "": item.text or "" for item in event.iter(f"{_EVENT_NS}Data")}
        events.append(
            {
                "id": event_id,
                "ts": ts,
                "process": data.get("param1", ""),
                "reason": data.get("param3", ""),
                "action": data.get("param5", ""),
            }
        )
    return sorted(events, key=lambda item: item["ts"])


# --- processes ----------------------------------------------------------------------------------


def port_owner(port: int) -> dict[str, Any] | None:
    """The process listening on the port: pid, command line and its parent's."""
    script = " ".join(
        [
            f"$c = Get-NetTCPConnection -State Listen -LocalPort {int(port)} -ErrorAction SilentlyContinue",
            "| Select-Object -First 1;",
            'if ($c) { $p = Get-CimInstance Win32_Process -Filter "ProcessId=$($c.OwningProcess)";',
            '$q = Get-CimInstance Win32_Process -Filter "ProcessId=$($p.ParentProcessId)";',
            "@{pid=$p.ProcessId; cmd=$p.CommandLine; parent=$p.ParentProcessId; parent_cmd=$q.CommandLine}",
            "| ConvertTo-Json -Compress }",
        ]
    )
    raw = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script]).strip()
    try:
        value = json.loads(raw) if raw else None
    except ValueError:
        return None
    return value if isinstance(value, dict) and value.get("pid") else None


def process_alive(pid: int) -> bool:
    """Whether ``pid`` is running (never ``os.kill``: on Windows that terminates the process)."""
    try:
        pid = int(pid)
        kernel32 = _dll("kernel32")
    except AttributeError, TypeError, ValueError:
        return False
    if pid <= 0:
        return False
    with contextlib.suppress(AttributeError):
        kernel32.OpenProcess.restype = ctypes.c_void_p  # a HANDLE, not a C int
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # Another user's process cannot be opened, but it exists.
        return bool(kernel32.GetLastError() == _ERROR_ACCESS_DENIED)
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code)):
            return False
        return code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def process_command(pid: int) -> str | None:
    """Read a process command line; missing rights or an exited process mean unknown."""
    try:
        pid = int(pid)
    except TypeError, ValueError:
        return None
    if pid <= 0:
        return None
    script = (
        f'Get-CimInstance Win32_Process -Filter "ProcessId={pid}" -ErrorAction SilentlyContinue '
        "| Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
    )
    raw = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], timeout=5)
    try:
        info = json.loads(raw) if raw else None
    except ValueError:
        return None
    if not isinstance(info, dict) or info.get("ProcessId") != pid:
        return None
    command = info.get("CommandLine")
    return command if isinstance(command, str) and command else None


def terminate(pid: int, timeout: float = 10.0) -> bool:
    """Stop ``pid`` and every process it started (taskkill /T /F); True when they are gone."""
    try:
        pid = int(pid)
    except TypeError, ValueError:
        return False
    if pid <= 0:
        return False
    try:
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            check=False,
            timeout=max(1.0, timeout),
            creationflags=CREATE_NO_WINDOW,
        )
    except OSError, subprocess.SubprocessError, ValueError:
        return False
    return result.returncode == 0 and _common.wait_gone(process_alive, pid, timeout)


# A job object that ends every process of this one when it ends, however it ends.
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
# spawn_detached's CREATE_BREAKAWAY_FROM_JOB still leaves it (what must outlive this process).
_JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x0800
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_job_lock = threading.Lock()
_job: int | None = None


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits),
        ("IoInfo", ctypes.c_ulonglong * 6),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def bind_children() -> bool:
    """Put this process into a job object that ends with it (best effort, once).

    Every process it starts afterwards (and theirs: the venv launcher's real Python) is in the
    job too, and Windows ends them all when the last handle - this process's - closes: a
    supervisor that crashes or is killed no longer leaves its web server holding the port.
    """
    global _job
    with _job_lock:
        if _job is not None:
            return True
        try:
            kernel32 = _dll("kernel32")
            kernel32.CreateJobObjectW.restype = ctypes.c_void_p
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            job = kernel32.CreateJobObjectW(None, None)
            if not job:
                return False
            limits = _ExtendedLimits()
            flags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | _JOB_OBJECT_LIMIT_BREAKAWAY_OK
            limits.BasicLimitInformation.LimitFlags = flags
            ok = kernel32.SetInformationJobObject(
                ctypes.c_void_p(job),
                _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(limits),
                ctypes.sizeof(limits),
            ) and kernel32.AssignProcessToJobObject(ctypes.c_void_p(job), ctypes.c_void_p(kernel32.GetCurrentProcess()))
        except AttributeError, OSError, TypeError, ValueError:
            return False
        if not ok:
            kernel32.CloseHandle(ctypes.c_void_p(job))
            return False
        _job = int(job)  # kept open for the life of this process, on purpose
        return True


def bring_to_front(process_id: int) -> None:
    """Show the process's window on the interactive desktop (a browser started hidden behind others)."""
    try:
        user32 = _dll("user32")
        enum_proc = getattr(ctypes, "WINFUNCTYPE", _missing)(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        handles: list[int] = []

        @enum_proc
        def collect(hwnd: int, _lparam: int) -> bool:
            owner = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == process_id and user32.IsWindow(hwnd):
                handles.append(int(hwnd))
            return True

        for _ in range(20):
            handles.clear()
            user32.EnumWindows(collect, 0)
            if handles:
                hwnd = handles[0]
                user32.ShowWindow(hwnd, 5)
                user32.SetForegroundWindow(hwnd)
                return
            time.sleep(0.25)
    except AttributeError, OSError:
        return


# --- browsers and folders -----------------------------------------------------------------------

_BROWSER_DIRS = ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")
_BROWSER_FILES = ("Microsoft/Edge/Application/msedge.exe", "Google/Chrome/Application/chrome.exe")
_BROWSER_NAMES = ("msedge.exe", "chrome.exe", "chromium.exe")


def browser_executables() -> list[str]:
    """Edge, then Chrome (the usual install folders, then PATH)."""
    found: list[str] = []
    for relative in _BROWSER_FILES:
        for variable in _BROWSER_DIRS:
            base = os.environ.get(variable, "")
            candidate = Path(base) / relative if base else None
            if candidate is not None and candidate.is_file():
                found.append(str(candidate))
    for name in _BROWSER_NAMES:
        on_path = shutil.which(name)
        if on_path:
            found.append(on_path)
    return list(dict.fromkeys(found))


_PROTECTED_ENV: tuple[str, ...] = (
    "SystemRoot",
    "ProgramFiles",
    "ProgramFiles(x86)",
    "ProgramW6432",
    "ProgramData",
    "APPDATA",
)
_PROTECTED_ENV += ("LOCALAPPDATA",)
# Also when TOW itself runs on Linux or macOS and the client is a Windows PC.
_DEFAULT_PROTECTED = ("Windows", "Program Files", "Program Files (x86)", "ProgramData")


# AppData of any profile on a Windows client - C:\Users\<name>\AppData, or through a share - so
# a client on another PC is protected without TOW knowing its profiles (normcased: lower case).
_PROFILE_APPDATA = re.compile(r"^(?:[a-z]:|\\\\[^\\]+\\[^\\]+)\\users\\[^\\]+\\appdata(?:\\|$)")


def is_profile_appdata(path: str) -> bool:
    """``path`` (a Windows path) is in the AppData folder of some user's profile."""
    return bool(_PROFILE_APPDATA.match(ntpath.normcase(ntpath.normpath(path))))


def protected_folders() -> list[str]:
    """Windows and program folders, AppData (incl. the Startup folder): never a download target."""
    roots = [os.environ.get(name, "") for name in _PROTECTED_ENV]
    profile = os.environ.get("USERPROFILE", "")
    if profile:
        roots.append(ntpath.join(profile, "AppData"))
    drive = os.environ.get("SYSTEMDRIVE", "") or "C:"
    roots += [ntpath.join(drive + "\\", name) for name in _DEFAULT_PROTECTED]
    return list(dict.fromkeys(root for root in roots if root))


# --- folders for this account only --------------------------------------------------------------

_SE_FILE_OBJECT = 1
_OWNER_AND_DACL = 0x1 | 0x4  # OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION
_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1
_SYSTEM_SID = "S-1-5-18"
_ADMINISTRATORS_SID = "S-1-5-32-544"
# Who besides this account may be granted a private folder: SYSTEM and Administrators, and the
# placeholders Windows resolves to the owner itself (CREATOR OWNER, OWNER RIGHTS). SDDL aliases.
_PRIVATE_TRUSTEES = frozenset({"SY", "BA", "CO", "OW", _SYSTEM_SID, _ADMINISTRATORS_SID})
_ALLOW_ACES = frozenset({"A", "OA", "XA", "ZA"})


def _local_string(pointer: Any) -> str | None:
    """The text of a string Windows allocated for us, freed afterwards."""
    try:
        return str(pointer.value) if pointer.value else None
    finally:
        _dll("kernel32").LocalFree(pointer)


def user_sid() -> str | None:
    """This process's account as a SID string (``S-1-5-21-…``), or None."""
    try:
        from ctypes import wintypes

        advapi32, kernel32 = _dll("advapi32"), _dll("kernel32")
        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(
            ctypes.c_void_p(kernel32.GetCurrentProcess()), _TOKEN_QUERY, ctypes.byref(token)
        ):
            return None
        try:
            size = wintypes.DWORD()
            advapi32.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(size))
            buffer = ctypes.create_string_buffer(size.value)
            if not size.value or not advapi32.GetTokenInformation(token, _TOKEN_USER, buffer, size, ctypes.byref(size)):
                return None
            sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]  # TOKEN_USER.User.Sid
            text = wintypes.LPWSTR()
            if not advapi32.ConvertSidToStringSidW(ctypes.c_void_p(sid), ctypes.byref(text)):
                return None
            return _local_string(text)
        finally:
            kernel32.CloseHandle(token)
    except AttributeError, OSError, ValueError, ImportError:
        return None


def folder_security(path: Path) -> str | None:
    """The owner and permissions of ``path`` in SDDL (``O:<sid>D:<flags>(ace)…``), or None."""
    try:
        from ctypes import wintypes

        advapi32, kernel32 = _dll("advapi32"), _dll("kernel32")
        descriptor = ctypes.c_void_p()
        if advapi32.GetNamedSecurityInfoW(
            str(path), _SE_FILE_OBJECT, _OWNER_AND_DACL, None, None, None, None, ctypes.byref(descriptor)
        ):
            return None
        try:
            text = wintypes.LPWSTR()
            if not advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
                descriptor, 1, _OWNER_AND_DACL, ctypes.byref(text), None
            ):
                return None
            return _local_string(text)
        finally:
            kernel32.LocalFree(descriptor)
    except AttributeError, OSError, ValueError, ImportError:
        return None


_SDDL = re.compile(r"O:(?P<owner>[^():]+?)D:(?P<flags>[A-Z_]*)(?P<aces>\(.*\))?")


def _aces(text: str) -> list[list[str]] | None:
    """The fields of each ``(type;flags;rights;object;inherited object;sid…)``; None when malformed."""
    aces: list[list[str]] = []
    depth, start = 0, 0
    for index, char in enumerate(text):
        if char == "(":
            depth += 1
            if depth == 1:
                start = index + 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                return None
            if depth == 0:
                fields = text[start:index].split(";", 6)
                if len(fields) < 6:
                    return None
                aces.append(fields)
    return aces if depth == 0 else None


def sddl_owner(sddl: str) -> str | None:
    match = _SDDL.fullmatch(sddl)
    return match.group("owner") if match else None


def sddl_others(sddl: str, user: str) -> bool | None:
    """Whether the permissions let an account other than ``user``, SYSTEM or Administrators in."""
    match = _SDDL.fullmatch(sddl)
    if match is None:
        return None
    if "NO_ACCESS_CONTROL" in match.group("flags"):
        return True  # no permissions at all: everyone may do everything
    aces = _aces(match.group("aces") or "")
    if aces is None:
        return None
    return any(ace[0] in _ALLOW_ACES and ace[5] not in _PRIVATE_TRUSTEES and ace[5] != user for ace in aces)


def elevated() -> bool:
    """This process runs with the Administrators group enabled (an elevated session)."""
    try:
        return bool(_dll("shell32").IsUserAnAdmin())
    except AttributeError, OSError:
        return False


def folder_shared(path: Path) -> bool | None:
    """Other accounts of this computer may open ``path`` (None: unknown)."""
    user, sddl = user_sid(), folder_security(path)
    if user is None or sddl is None:
        return None
    return sddl_others(sddl, user)


def make_private(path: Path) -> bool:
    """Only this account, SYSTEM and Administrators may open ``path``: the inherited permissions
    (``Authenticated Users: modify`` of a drive root) are replaced, the change goes on to what
    the folder holds. Only a folder this account owns is changed (an elevated process creates
    folders owned by Administrators: those too, while elevated). True when read back private."""
    user, sddl = user_sid(), folder_security(path)
    owner = sddl_owner(sddl) if sddl is not None else None
    if user is None or owner is None or not (owner == user or (owner in {"BA", _ADMINISTRATORS_SID} and elevated())):
        return False
    icacls = ntpath.join(os.environ.get("SYSTEMROOT") or r"C:\Windows", "System32", "icacls.exe")
    grants = [f"*{sid}:(OI)(CI)F" for sid in (user, _SYSTEM_SID, _ADMINISTRATORS_SID)]
    _run([icacls, str(path), "/inheritance:r", "/grant:r", *grants, "/Q"], timeout=120)
    return folder_shared(path) is False


class WindowsBackend:
    name = "windows"

    def boot_time(self, now: float | None = None) -> float | None:
        times = uptime()
        if times is None:
            return None
        return (time.time() if now is None else now) - times[0]

    def asleep_seconds(self) -> float | None:
        times = uptime()
        return None if times is None else max(0.0, times[0] - times[1])

    def logon_time(self) -> float | None:
        return logon_time()

    def shutdown_reasons(self, since: float, until: float) -> list[dict[str, Any]]:
        return shutdown_events(since, until)

    def popen_options(self, *, new_group: bool = False, hidden: bool = True) -> dict[str, Any]:
        flags = (CREATE_NO_WINDOW if hidden else 0) | (CREATE_NEW_PROCESS_GROUP if new_group else 0)
        return {"creationflags": flags, "close_fds": True}

    def spawn_detached(
        self,
        argv: Sequence[str],
        *,
        hidden: bool = True,
        log_path: Path | None = None,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        require_breakaway: bool = False,
    ) -> int:
        """Start ``argv`` so it outlives this process (and the task or console that started it)."""
        flags = CREATE_NEW_PROCESS_GROUP | (CREATE_NO_WINDOW if hidden else CREATE_NEW_CONSOLE)
        with _common.output_to(log_path) as output:
            for extra in (CREATE_BREAKAWAY_FROM_JOB, 0):  # a job that forbids breaking away refuses it
                try:
                    process = subprocess.Popen(
                        list(argv),
                        cwd=str(cwd) if cwd else None,
                        env=env,
                        stdin=subprocess.DEVNULL,
                        stdout=output,
                        stderr=output,
                        creationflags=flags | extra,
                        close_fds=True,
                    )
                except PermissionError:
                    if extra and not require_breakaway:
                        continue
                    raise
                return int(process.pid)
        raise OSError("process could not start")  # pragma: no cover - the loop returns or raises

    def process_alive(self, pid: int) -> bool:
        return process_alive(pid)

    def process_command(self, pid: int) -> str | None:
        return process_command(pid)

    def publish_exclusive(self, source: Path, destination: Path) -> None:
        """Windows rename refuses an existing destination, including on non-NTFS volumes."""
        os.rename(source, destination)

    def bind_children(self) -> bool:
        return bind_children()

    def die_with_parent(self, parent_pid: int) -> bool:
        return False  # the parent's job object ends this process (bind_children)

    def terminate(self, pid: int, timeout: float = 10.0) -> bool:
        return terminate(pid, timeout)

    def port_owner(self, port: int) -> dict[str, Any] | None:
        return port_owner(port)

    def browser_executables(self) -> list[str]:
        return browser_executables()

    def browser_launch(self, executable: str, home: Path) -> dict[str, Any]:
        """Nothing to add on Windows: --user-data-dir keeps the session in its folder."""
        del executable, home
        return {"args": [], "env": None, "confined": False}

    def bring_to_front(self, pid: int) -> None:
        bring_to_front(pid)

    def protected_folders(self) -> list[str]:
        return protected_folders()

    def folder_shared(self, path: Path) -> bool | None:
        return folder_shared(path)

    def make_private(self, path: Path) -> bool:
        return make_private(path)

    def open_url(self, url: str) -> bool:
        return _common.open_url(url)

    def ui_language(self) -> str | None:
        return ui_language()
