"""Windows: facts Windows already keeps, and starting/stopping processes without a console.

- boot time and the time spent asleep since boot (kernel32: GetTickCount64 counts sleep,
  QueryUnbiasedInterruptTime does not), and when the owner signed in (wtsapi32);
- why the PC went down: the System event log (1074 - who restarted or powered off and why,
  41 / 6008 - it went down without a clean shutdown);
- processes: hidden, detached starts; the whole tree stopped with taskkill;
- Edge or Chrome for the browser sign-in; Windows, program and AppData folders;
- the install folder, keys/ and data/ for this account, SYSTEM and Administrators only (icacls,
  read back).
"""

from __future__ import annotations

import contextlib
import ctypes
import functools
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

from tow.platform import _common, powershell_program, windows_program
from tow.platform._common import Volume

FILETIME_UNIX_EPOCH = 116444736000000000
_EVENT_NS = "{http://schemas.microsoft.com/win/2004/08/events/event}"

CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NEW_CONSOLE = 0x00000010
CREATE_NO_WINDOW = 0x08000000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
_SYNCHRONIZE = 0x00100000  # enough to wait for a process: the least any account is refused
_WAIT_OBJECT_0 = 0
_ERROR_ACCESS_DENIED = 5


def _run(args: list[str], timeout: float = 20) -> str:
    return _common.run_quiet(args, timeout=timeout, creationflags=CREATE_NO_WINDOW)


def _powershell(script: str, timeout: float = 20) -> str:
    """A PowerShell script's output, written as UTF-8: without it Windows PowerShell writes the
    ANSI code page, and a Cyrillic folder in a command line came back as U+FFFD. Constrained
    Language Mode (an AppLocker or App Control policy) refuses to set it: the script then runs
    on in the code page, so a probe that reads paths answers in ``_ASCII_JSON``."""
    utf8 = "try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }; "
    return _run([powershell_program(), "-NoProfile", "-NonInteractive", "-Command", utf8 + script], timeout=timeout)


# A pipeline's value as JSON in ASCII, every other character a \u escape, with no .NET call: it
# reads the same in any code page, also in Constrained Language Mode (measured there: setting
# the UTF-8 output fails, and a Cyrillic folder in a command line came back as U+FFFD).
_ASCII_JSON = (
    "| ConvertTo-Json -Compress | ForEach-Object { -join ($_.ToCharArray() | ForEach-Object "
    "{ if ([int]$_ -gt 126) { '\\u{0:x4}' -f [int]$_ } else { [string]$_ } }) }"
)


def _ascii_json(raw: str) -> Any:
    """A probe's ``_ASCII_JSON`` answer; None when there is none, it is no JSON, or it is not
    ASCII: letters some code page guessed at are never taken for a path (unknown, not another)."""
    text = raw.strip()
    if not text or not text.isascii():
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


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


# GetCompressedFileSizeW returns this low DWORD on failure, and for some large files as well.
_INVALID_FILE_SIZE = 0xFFFFFFFF


def file_allocation(low: int, high: int, last_error: int) -> int | None:
    """GetCompressedFileSizeW's answer as one number of bytes: both DWORDs combined. A low DWORD
    of all ones is a failure only when the call set an error (a file of 4 GiB - 1 bytes, or that
    plus any multiple of 4 GiB, legitimately has it); None then."""
    if low == _INVALID_FILE_SIZE and last_error != 0:
        return None
    return (high << 32) | low


def compressed_file_size(path: Path) -> int | None:
    """Bytes ``path`` takes on disk as NTFS says it (GetCompressedFileSizeW): the allocated
    ranges of a sparse file, the compressed size of a compressed one, else its size. None when
    the call fails or the system has no such call."""
    # A library that keeps the call's last error (windll does not): Windows only.
    library = getattr(ctypes, "WinDLL", None)
    set_error, get_error = getattr(ctypes, "set_last_error", None), getattr(ctypes, "get_last_error", None)
    if library is None or set_error is None or get_error is None:
        return None
    try:
        from ctypes import wintypes

        call = library("kernel32", use_last_error=True).GetCompressedFileSizeW
        call.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD))
        call.restype = wintypes.DWORD
        high = wintypes.DWORD(0)
        set_error(0)
        low = int(call(str(path), ctypes.byref(high)))
        return file_allocation(low, int(high.value), int(get_error()))
    except AttributeError, OSError, ValueError:
        return None


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
    """Shutdown records of the System event log between two moments, oldest first: 1074 (who
    shut Windows down, with the kind Kernel-Power recorded for it, ``power_action``), 41 and
    6008 (it went down without one)."""
    since = datetime.fromtimestamp(since_ts, UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    until = datetime.fromtimestamp(until_ts, UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    query = (
        "*[System[(EventID=1074 or EventID=41 or EventID=6008"
        f" or (EventID=109 and Provider[@Name='{_KERNEL_POWER}']))"
        f" and TimeCreated[@SystemTime>='{since}' and @SystemTime<='{until}']]]"
    )
    raw = _run([windows_program("wevtutil.exe"), "qe", "System", f"/q:{query}", "/c:40", "/rd:true", "/f:xml"])
    return with_power_actions(parse_events(raw))


# Kernel-Power event 109 (version 0) says how Windows went down, ShutdownActionType a POWER_ACTION:
# on a Windows 11 PC every 1074 "restart" was followed by a 5 and every "power off" by a 6, 0 to
# 77 s later. Other codes (4, a shutdown, was never seen) and other versions say nothing here.
_KERNEL_POWER = "Microsoft-Windows-Kernel-Power"
_KERNEL_POWER_GUID = "{331c3b3a-2005-44c2-ac5e-77220c37d6b4}"
_POWER_ACTIONS = {"5": "restart", "6": "power_off"}
_SHUTDOWN_SEQUENCE_SEC = 120


def _power_action(provider: str, guid: str, version: str, data: dict[str, str]) -> str | None:
    """The kind a Kernel-Power 109 record names, or None when it is not one this reading knows."""
    if provider != _KERNEL_POWER or (guid and guid.casefold() != _KERNEL_POWER_GUID) or version not in {"", "0"}:
        return None
    return _POWER_ACTIONS.get(data.get("ShutdownActionType", "").strip())


def with_power_actions(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``events`` without the Kernel-Power 109 records, each attached to the 1074 of its shutdown:
    the record just before it (no crash or other record between) and at most two minutes
    earlier. That 1074 gets ``power_action`` ("restart", "power_off"; "unclear" when two of its
    109 records disagree); a 109 that belongs to no 1074, or names no known kind, adds nothing -
    it never becomes the last record and never replaces who shut Windows down."""
    kept: list[dict[str, Any]] = []
    for event in events:
        if event["id"] != 109:
            kept.append(event)
            continue
        owner = kept[-1] if kept else None
        action = event.get("power_action")
        if action is None or owner is None or owner["id"] != 1074:
            continue
        if not 0 <= event["ts"] - owner["ts"] <= _SHUTDOWN_SEQUENCE_SEC:
            continue
        owner["power_action"] = action if owner.get("power_action", action) == action else "unclear"
    return kept


def parse_events(raw: str) -> list[dict[str, Any]]:
    """wevtutil's XML records as {id, ts, process, reason, action}, oldest first; a Kernel-Power
    109 record also carries the kind it names (``power_action``, None when not known)."""
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
        record: dict[str, Any] = {
            "id": event_id,
            "ts": ts,
            "process": data.get("param1", ""),
            "reason": data.get("param3", ""),
            "action": data.get("param5", ""),
        }
        if event_id == 109:
            provider = system.find(f"{_EVENT_NS}Provider")
            record["power_action"] = _power_action(
                str(provider.get("Name") or "") if provider is not None else "",
                str(provider.get("Guid") or "") if provider is not None else "",
                (system.findtext(f"{_EVENT_NS}Version") or "").strip(),
                data,
            )
        events.append(record)
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
            _ASCII_JSON,
            "}",
        ]
    )
    value = _ascii_json(_powershell(script))
    return value if isinstance(value, dict) and value.get("pid") else None


def _kernel32_with_last_error() -> tuple[Any, Any]:
    """kernel32 whose calls keep their own last error, and the reader of that error
    (``ctypes.get_last_error``): through ``windll`` another call in between may already have
    replaced it. AttributeError where there is no Windows (looked up by name, as in
    ``compressed_file_size``: the type stubs know these only on Windows)."""
    library, get_error = getattr(ctypes, "WinDLL", None), getattr(ctypes, "get_last_error", None)
    if getattr(ctypes, "windll", None) is None or library is None or get_error is None:
        raise AttributeError("ctypes.windll")
    return library("kernel32", use_last_error=True), get_error


# GetDriveTypeW: a network drive or share; removable, fixed, CD and RAM disks are this computer's.
_DRIVE_KINDS = {2: "local", 3: "local", 4: "network", 5: "local", 6: "local"}


def _volume_call(name: str, path: str, size: int) -> str | None:
    """GetVolumePathNameW or GetVolumeNameForVolumeMountPointW of ``path``, None when it fails."""
    from ctypes import wintypes

    kernel32, _last_error = _kernel32_with_last_error()
    call = getattr(kernel32, name)
    call.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD)
    call.restype = wintypes.BOOL
    buffer = ctypes.create_unicode_buffer(size)
    return buffer.value if call(path, buffer, size) and buffer.value else None


def _volume_path(path: str) -> str | None:
    """The mount point holding ``path``: a drive (``D:\\``), a folder a volume is mounted on
    (``C:\\mnt\\disk\\``) or a share (``\\\\nas\\tv\\``)."""
    return _volume_call("GetVolumePathNameW", path, 32768)


def _volume_guid(mount_point: str) -> str | None:
    """``\\\\?\\Volume{GUID}\\`` of a local volume's mount point: the same after a reboot or another
    drive letter; None for a share or a substituted drive."""
    return _volume_call("GetVolumeNameForVolumeMountPointW", mount_point, 50)


def _volume_serial(root: str) -> int | None:
    """The serial number GetVolumeInformationW reports for ``root`` (a mount point, a drive or a
    share, ending in a backslash): a new one with every format; for a share what its server says.
    None when it fails."""
    from ctypes import wintypes

    kernel32, _last_error = _kernel32_with_last_error()
    call = kernel32.GetVolumeInformationW
    call.argtypes = (
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPWSTR,
        wintypes.DWORD,
    )
    call.restype = wintypes.BOOL
    serial = wintypes.DWORD(0)
    if not call(root, None, 0, ctypes.byref(serial), None, None, None, 0):
        return None
    return int(serial.value)


def _remote_name(drive: str) -> str | None:
    """The share a mapped network drive (``Z:``) stands for (WNetGetConnectionW), None when it
    is no mapped drive or Windows cannot tell."""
    from ctypes import wintypes

    library = getattr(ctypes, "WinDLL", None)
    if library is None or getattr(ctypes, "windll", None) is None:
        raise AttributeError("ctypes.windll")
    call = library("mpr").WNetGetConnectionW
    call.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    call.restype = wintypes.DWORD
    size = wintypes.DWORD(1024)
    buffer = ctypes.create_unicode_buffer(size.value)
    if call(drive, buffer, ctypes.byref(size)) != 0 or not buffer.value:
        return None
    return buffer.value


def _dos_device(drive: str) -> str | None:
    """What the drive letter ``X:`` stands for (QueryDosDeviceW): ``\\??\\C:\\folder`` for a
    substituted drive, a device name otherwise; None when it fails."""
    from ctypes import wintypes

    kernel32, _last_error = _kernel32_with_last_error()
    call = kernel32.QueryDosDeviceW
    call.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD)
    call.restype = wintypes.DWORD
    buffer = ctypes.create_unicode_buffer(32768)
    if not call(drive, buffer, len(buffer)):
        return None
    return buffer.value or None


def _subst_target(drive: str) -> str | None:
    """The folder a substituted drive (``subst S: C:\\folder``) stands for, None for any other."""
    device = _dos_device(drive)
    if not device or not device.startswith("\\??\\"):
        return None
    target = device[4:]
    if target[:4].upper() == "UNC\\":
        return "\\\\" + target[4:]
    return target


def _drive_type(mount_point: str) -> int:
    from ctypes import wintypes

    kernel32, _last_error = _kernel32_with_last_error()
    kernel32.GetDriveTypeW.argtypes = (wintypes.LPCWSTR,)
    kernel32.GetDriveTypeW.restype = wintypes.UINT
    return int(kernel32.GetDriveTypeW(mount_point))


def _local_identity(guid: str, root: str) -> str | None:
    """A local volume: its GUID and its serial number (a reformat keeps the GUID), both or none."""
    serial = _volume_serial(root)
    return f"win-volume:{guid.casefold()}:{serial:08x}" if serial is not None else None


def _share_identity(share: str, root: str) -> str:
    """A share by its path, and the serial its server reports where it reports one."""
    serial = _volume_serial(root)
    name = "win-share:" + ntpath.normcase(share.rstrip("\\"))
    return name if serial is None else f"{name}:{serial:08x}"


def volume(path: Path, *, _depth: int = 0) -> Volume | None:
    """The volume holding the existing ``path`` (tow.platform.volume_of).

    Its identity names that very file system: a local volume by its GUID and serial number (a
    reformatted or another disk under the same letter or folder differs); a share, or a mapped
    network drive by the share it stands for, by its path and the serial its server reports
    (where it reports one); a substituted drive by the volume of its folder and that folder.
    None when Windows cannot tell (no GUID, no serial, no share name): never the drive letter."""
    text = os.fspath(path)
    try:
        drive = ntpath.splitdrive(text)[0]
        if len(drive) == 2 and drive[1] == ":" and _depth < 4:
            target = _subst_target(drive)
            if target is not None:
                folder = target.rstrip("\\")
                inner = volume(Path(folder + text[2:]), _depth=_depth + 1)
                if inner is None or inner.identity is None:
                    return Volume(None, inner.kind if inner is not None else "unknown")
                return Volume(inner.identity + "|subst:" + ntpath.normcase(folder), inner.kind)
        mount_point = _volume_path(text)
        if not mount_point:
            return None
        if mount_point.startswith("\\\\"):
            return Volume(_share_identity(mount_point, mount_point), "network")
        kind = _DRIVE_KINDS.get(_drive_type(mount_point), "unknown")
        if kind == "network":
            remote = _remote_name(mount_point[:2]) if len(mount_point) == 3 and mount_point[1] == ":" else None
            return Volume(_share_identity(remote, mount_point) if remote else None, kind)
        guid = _volume_guid(mount_point)
        return Volume(_local_identity(guid, mount_point) if guid else None, kind)
    except AttributeError, OSError, ValueError:
        return None


def process_alive(pid: int) -> bool:
    """Whether ``pid`` is running (never ``os.kill``: on Windows that terminates the process).

    A zero-time wait on the process: signalled means it has ended, whatever its exit code (one
    that ended with 259, STILL_ACTIVE, read as running for ever). Another account's process that
    may not be opened exists; a wait that fails proves nothing ended: both count as running."""
    try:
        pid = int(pid)
        kernel32, last_error = _kernel32_with_last_error()
    except AttributeError, OSError, TypeError, ValueError:
        return False
    if pid <= 0 or pid > 0xFFFFFFFF:
        return False
    from ctypes import wintypes

    with contextlib.suppress(AttributeError, TypeError):
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(_SYNCHRONIZE, False, pid)
    if not handle:
        return bool(last_error() == _ERROR_ACCESS_DENIED)  # ERROR_INVALID_PARAMETER: no such process
    try:
        return bool(kernel32.WaitForSingleObject(handle, 0) != _WAIT_OBJECT_0)
    except OSError:
        return True
    finally:
        kernel32.CloseHandle(handle)


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
        "| Select-Object ProcessId,CommandLine " + _ASCII_JSON
    )
    info = _ascii_json(_powershell(script, timeout=5))
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
            [windows_program("taskkill.exe"), "/PID", str(pid), "/T", "/F"],
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


def _system_drive() -> str:
    drive = os.environ.get("SYSTEMDRIVE", "").strip()
    return drive if re.fullmatch(r"[A-Za-z]:", drive) else "C:"


def protected_folders() -> list[str]:
    """Windows and program folders, AppData (incl. the Startup folder): never a download target."""
    roots = [os.environ.get(name, "") for name in _PROTECTED_ENV]
    profile = os.environ.get("USERPROFILE", "")
    if profile:
        roots.append(ntpath.join(profile, "AppData"))
    roots += [ntpath.join(_system_drive() + "\\", name) for name in _DEFAULT_PROTECTED]
    return list(dict.fromkeys(root for root in roots if root))


def on_system_drive(path: str) -> str:
    """A root-relative path (``/Windows/System32``, ``\\Users\\x``) as a program on a Windows PC
    opens it: on its current drive, which for a torrent client is the system drive."""
    return ntpath.join(_system_drive() + "\\", path.lstrip("/\\"))


# --- folders for this account only --------------------------------------------------------------

_SE_FILE_OBJECT = 1
_OWNER_AND_DACL = 0x1 | 0x4  # OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION
_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1
_SYSTEM_SID = "S-1-5-18"
_ADMINISTRATORS_SID = "S-1-5-32-544"
# Who besides this account may be granted a private folder: SYSTEM and Administrators, and the
# placeholders Windows resolves to the owner itself (CREATOR OWNER, OWNER RIGHTS).
_PRIVATE_TRUSTEES = frozenset({_SYSTEM_SID, _ADMINISTRATORS_SID, "S-1-3-0", "S-1-3-4"})
_ALLOW_ACES = frozenset({"A", "OA", "XA", "ZA"})
# Accounts SDDL writes as two letters, the same on every computer. Those of this computer or its
# domain (LA: the built-in Administrator, LG: Guest, DA, DU…) Windows itself resolves.
_SDDL_ALIASES = {
    "AN": "S-1-5-7",
    "AU": "S-1-5-11",
    "BA": _ADMINISTRATORS_SID,
    "BG": "S-1-5-32-546",
    "BU": "S-1-5-32-545",
    "CG": "S-1-3-1",
    "CO": "S-1-3-0",
    "IU": "S-1-5-4",
    "LS": "S-1-5-19",
    "NS": "S-1-5-20",
    "NU": "S-1-5-2",
    "OW": "S-1-3-4",
    "PS": "S-1-5-10",
    "RC": "S-1-5-12",
    "SU": "S-1-5-6",
    "SY": _SYSTEM_SID,
    "WD": "S-1-1-0",
}


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


@functools.lru_cache(maxsize=32)
def _alias_sid(alias: str) -> str | None:
    """What Windows means by an SDDL alias of this computer or domain (``LA`` → ``S-1-5-21-…-500``)."""
    try:
        from ctypes import wintypes

        advapi32 = _dll("advapi32")
        sid = ctypes.c_void_p()
        if not advapi32.ConvertStringSidToSidW(alias, ctypes.byref(sid)):
            return None
        try:
            text = wintypes.LPWSTR()
            if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)):
                return None
            return _local_string(text)
        finally:
            _dll("kernel32").LocalFree(sid)
    except AttributeError, OSError, ValueError, ImportError:
        return None


def sid_text(trustee: str) -> str:
    """An account as SDDL writes it - ``S-1-5-…`` or an alias (``BA``, ``LA``) - as ``S-1-…`` text,
    so the same account compares equal however it was written; an unknown alias stays itself."""
    if trustee[:2].upper() == "S-":
        return trustee.upper()
    return _SDDL_ALIASES.get(trustee) or _alias_sid(trustee) or trustee


def sddl_owner(sddl: str) -> str | None:
    """The owner of the permissions as ``S-1-…`` text (aliases resolved)."""
    match = _SDDL.fullmatch(sddl)
    return sid_text(match.group("owner")) if match else None


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
    allowed = _PRIVATE_TRUSTEES | {sid_text(user)}
    return any(ace[0] in _ALLOW_ACES and sid_text(ace[5]) not in allowed for ace in aces)


def folder_shared(path: Path) -> bool | None:
    """Other accounts of this computer may open ``path`` (None: unknown)."""
    user, sddl = user_sid(), folder_security(path)
    if user is None or sddl is None:
        return None
    return sddl_others(sddl, user)


def make_private(path: Path, *, created: bool = False) -> bool:
    """Only this account, SYSTEM and Administrators may open ``path``: the inherited permissions
    (``Authenticated Users: modify`` of a drive root) are replaced, the change goes on to what
    the folder holds. Only a folder this account owns is changed, or one this process has just
    ``created`` (an elevated process creates folders owned by Administrators): a folder that
    Administrators own may be another account's, which would lose its access. True when read
    back private."""
    user, sddl = user_sid(), folder_security(path)
    owner = sddl_owner(sddl) if sddl is not None else None
    if user is None or owner is None:
        return False
    if owner != sid_text(user) and not (created and owner == _ADMINISTRATORS_SID):
        return False
    _close(path, user)
    return folder_shared(path) is False


def _icacls() -> str:
    """icacls.exe of Windows itself, never one of the current folder."""
    return windows_program("icacls.exe")


def _close(path: Path, account: str) -> None:
    """``account``, SYSTEM and Administrators get full control of ``path``, nothing inherited;
    what the folder holds inherits that. /C: a file below that refuses the change does not stop
    it half way (that left the folder protected with a copy of what it had inherited). Rules
    given to another account on the folder itself - such a copy, or one added by hand - outlive
    /inheritance:r and are removed by name."""
    grants = [f"*{sid}:(OI)(CI)F" for sid in (account, _SYSTEM_SID, _ADMINISTRATORS_SID)]
    _run([_icacls(), str(path), "/inheritance:r", "/grant:r", *grants, "/C", "/Q"], timeout=120)
    sddl = folder_security(path)
    # One account at a time: icacls refuses a whole call naming an account Windows no longer
    # knows (a deleted one's SID), and that one stays (read back as open).
    for sid in _other_trustees(sddl, account) if sddl is not None else []:
        _run([_icacls(), str(path), "/remove:g", f"*{sid}", "/C", "/Q"], timeout=120)


def _other_trustees(sddl: str, user: str) -> list[str]:
    """The accounts other than ``user``, SYSTEM and Administrators an allow rule lets in, as
    ``S-1-…`` text, in the order of the rules."""
    match = _SDDL.fullmatch(sddl)
    aces = _aces(match.group("aces") or "") if match else None
    allowed = _PRIVATE_TRUSTEES | {sid_text(user)}
    found = [sid_text(ace[5]) for ace in aces or [] if ace[0] in _ALLOW_ACES]
    return list(dict.fromkeys(sid for sid in found if sid not in allowed))


# --- handing a folder back to its owner (tow permissions fix) -----------------------------------


def elevated() -> bool:
    """This process runs with the Administrators group enabled (an administrator terminal)."""
    try:
        return bool(_dll("shell32").IsUserAnAdmin())
    except AttributeError, OSError:
        return False


_WTS_CURRENT_SESSION = 0xFFFFFFFF
_WTS_USER_NAME = 5
_WTS_DOMAIN_NAME = 7


def session_user_sid() -> str | None:
    """The account signed in to this process's Windows session - the person at the screen - as
    ``S-1-…`` text, or None. An administrator terminal opened with an administrator's password
    from a standard account runs as the administrator; the session's account is still the
    standard one."""
    try:
        from ctypes import wintypes

        wtsapi32 = _dll("wtsapi32")
        names = []
        for info in (_WTS_DOMAIN_NAME, _WTS_USER_NAME):
            buffer, size = wintypes.LPWSTR(), wintypes.DWORD()
            if not wtsapi32.WTSQuerySessionInformationW(
                None, wintypes.DWORD(_WTS_CURRENT_SESSION), info, ctypes.byref(buffer), ctypes.byref(size)
            ):
                return None
            try:
                names.append(str(buffer.value or ""))
            finally:
                wtsapi32.WTSFreeMemory(buffer)
    except AttributeError, OSError, ValueError, ImportError:
        return None
    domain, user = names
    if not user:
        return None
    return account_of(f"{domain}\\{user}" if domain else user)


def personal_account(account: str) -> bool:
    """``account`` is a person's account (local, domain or Microsoft), not a group, SYSTEM or a
    service: only such an account is ever made the owner of an install."""
    return sid_text(account).startswith(("S-1-5-21-", "S-1-12-1-"))


def folder_owner(path: Path) -> str | None:
    """The owner of ``path`` as ``S-1-…`` text, or None."""
    sddl = folder_security(path)
    return sddl_owner(sddl) if sddl is not None else None


def account_name(account: str) -> str:
    """``DOMAIN\\name`` of a SID for a person to read; the SID itself when Windows cannot say."""
    try:
        from ctypes import wintypes

        advapi32 = _dll("advapi32")
        sid = ctypes.c_void_p()
        if not advapi32.ConvertStringSidToSidW(account, ctypes.byref(sid)):
            return account
        try:
            name, domain = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(256)
            size, domain_size, use = wintypes.DWORD(256), wintypes.DWORD(256), wintypes.DWORD()
            if not advapi32.LookupAccountSidW(
                None, sid, name, ctypes.byref(size), domain, ctypes.byref(domain_size), ctypes.byref(use)
            ):
                return account
            return f"{domain.value}\\{name.value}" if domain.value else name.value
        finally:
            _dll("kernel32").LocalFree(sid)
    except AttributeError, OSError, ValueError, ImportError:
        return account


def account_of(name: str) -> str | None:
    """The SID of an account written as ``DOMAIN\\name``, ``name`` or ``S-1-…``, or None."""
    if name[:2].upper() == "S-":
        return sid_text(name)
    try:
        from ctypes import wintypes

        advapi32 = _dll("advapi32")
        sid = ctypes.create_string_buffer(68)  # SECURITY_MAX_SID_SIZE
        domain = ctypes.create_unicode_buffer(256)
        size, domain_size, use = wintypes.DWORD(68), wintypes.DWORD(256), wintypes.DWORD()
        if not advapi32.LookupAccountNameW(
            None, name, sid, ctypes.byref(size), domain, ctypes.byref(domain_size), ctypes.byref(use)
        ):
            return None
        text = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            return None
        return _local_string(text)
    except AttributeError, OSError, ValueError, ImportError:
        return None


def hand_over(path: Path, account: str) -> bool:
    """``account`` becomes the owner of ``path`` (the folder itself, not what it holds) and the
    folder is closed for it as at every start: ``account``, SYSTEM and Administrators, nothing
    inherited. Needs an administrator terminal. True when read back so."""
    account = sid_text(account)
    if not personal_account(account):
        return False
    _run([_icacls(), str(path), "/setowner", f"*{account}", "/Q"], timeout=120)
    _close(path, account)
    sddl = folder_security(path)
    return sddl is not None and sddl_owner(sddl) == account and sddl_others(sddl, account) is False


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

    def root_shared(self, path: Path) -> bool | None:
        """The install root is closed as keys/ and data/ are: anyone else who may open it."""
        return folder_shared(path)

    def make_private(self, path: Path, *, created: bool = False) -> bool:
        return make_private(path, created=created)

    def allocated_bytes(self, path: Path) -> int | None:
        return compressed_file_size(path)

    def volume(self, path: Path) -> Volume | None:
        return volume(path)

    def elevated(self) -> bool:
        return elevated()

    def current_account(self) -> str | None:
        return user_sid()

    def invoking_account(self) -> str | None:
        """An administrator terminal of Windows runs as the account that opened it - unless a
        standard account typed an administrator's password: see ``session_account``."""
        return user_sid()

    def session_account(self) -> str | None:
        return session_user_sid()

    def personal_account(self, account: str) -> bool:
        return personal_account(account)

    def folder_owner(self, path: Path) -> str | None:
        return folder_owner(path)

    def shared_for(self, path: Path, account: str, *, root: bool = False) -> bool | None:
        """Accounts other than `account`, SYSTEM and Administrators may open `path`."""
        del root  # the root is closed as keys/ and data/ are
        sddl = folder_security(path)
        return None if sddl is None else sddl_others(sddl, account)

    def account_name(self, account: str) -> str:
        return account_name(account)

    def account_of(self, name: str) -> str | None:
        return account_of(name)

    def hand_over(self, path: Path, account: str) -> bool:
        return hand_over(path, account)

    def open_url(self, url: str) -> bool:
        return _common.open_url(url)

    def ui_language(self) -> str | None:
        return ui_language()
