"""Linux and macOS: the same facts as on Windows, from what these systems keep.

- boot time: /proc/uptime (Linux), ``sysctl kern.boottime`` (macOS);
- time asleep: the clock that counts sleep minus the one that does not
  (CLOCK_BOOTTIME - CLOCK_MONOTONIC on Linux, CLOCK_MONOTONIC - CLOCK_UPTIME_RAW on macOS);
- logon time: not known (None); shutdown reasons: when the previous boots ended, from
  ``journalctl --list-boots`` on Linux (best effort), none on macOS;
- processes: a detached start gets its own session; stopping signals the whole group;
- Chrome, Chromium or Edge for the browser sign-in; system folders and the folders programs
  start from (~/.config, ~/Library) for the save-path rule.

The functions take the system through small module-level seams (``_read``, ``_run``,
``_clock``, ``_signal``...), so tests run this logic on any machine.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import posixpath
import re
import shutil
import signal
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from tow.platform import _common
from tow.platform._common import Volume

# Folders a download or backup never goes to, as typed or as resolved. /var as a whole is not
# among them: download folders live there (/var/lib/transmission-daemon/downloads, macOS's
# temp folders); only the parts programs run from are.
SYSTEM_FOLDERS: tuple[str, ...] = ("/bin", "/boot", "/dev", "/etc", "/lib", "/lib64", "/proc", "/sbin", "/sys", "/usr")
SYSTEM_FOLDERS += ("/var/spool", "/var/db", "/var/root", "/System", "/Library", "/Applications")
SYSTEM_FOLDERS += ("/private/etc", "/private/var/root", "/private/var/db")
# Under the home folder: keys, and where programs start from (autostart, LaunchAgents).
HOME_FOLDERS = (".ssh", ".config", "Library")

_LINUX_BROWSERS: tuple[str, ...] = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")
_LINUX_BROWSERS += ("microsoft-edge", "microsoft-edge-stable")
_MAC_APPS = (
    "Google Chrome.app/Contents/MacOS/Google Chrome",
    "Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "Chromium.app/Contents/MacOS/Chromium",
)
_KILL = getattr(signal, "SIGKILL", 9)
# The sign-in browser keeps nothing outside its session folder: no keyring or Keychain entry
# (a basic password store, a mock Keychain), and HOME/XDG folders inside the session, so
# ~/.pki/nssdb, the fontconfig cache and the like are not written into the owner's home.
BROWSER_FLAGS = ("--password-store=basic", "--use-mock-keychain")
# What a graphical program needs to show a window, when TOW was started without it (a systemd
# user service started before the desktop): taken from the user manager, never overwritten.
_DISPLAY_ENV = ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR")


# --- seams (tests replace these) ----------------------------------------------------------------


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


def _run(args: list[str], timeout: float = 20) -> str:
    return _common.run_quiet(args, timeout=timeout)


def _clock(name: str) -> float | None:
    clock_id = getattr(time, name, None)
    read = getattr(time, "clock_gettime", None)  # POSIX only
    if clock_id is None or read is None:
        return None
    try:
        return float(read(clock_id))
    except OSError, AttributeError:
        return None


def _signal(pid: int, sig: int) -> None:
    os.kill(pid, sig)  # pragma: no cover - replaced in tests on Windows


def _signal_group(pgid: int, sig: int) -> None:
    os.killpg(pgid, sig)  # type: ignore[attr-defined, unused-ignore]  # pragma: no cover


def _group_of(pid: int) -> int | None:
    try:
        return int(os.getpgid(pid))  # type: ignore[attr-defined, unused-ignore]
    except OSError, AttributeError:
        return None


def _reap(pid: int) -> bool:
    """True when ``pid`` was this process's child and has exited (collected now)."""
    try:
        done, _status = os.waitpid(pid, getattr(os, "WNOHANG", 1))
    except ChildProcessError, OSError, AttributeError:
        return False
    return done == pid


def _set_parent_death_signal(sig: int) -> bool:
    """Linux prctl(PR_SET_PDEATHSIG): the kernel sends ``sig`` when the parent ends."""
    import ctypes

    try:
        # The process's own symbols include libc: no ldconfig run (find_library starts one).
        libc = ctypes.CDLL(None, use_errno=True)
        return int(libc.prctl(1, int(sig), 0, 0, 0)) == 0  # PR_SET_PDEATHSIG = 1
    except AttributeError, OSError, TypeError, ValueError:
        return False


def _hard_link(source: Path, destination: Path) -> None:
    os.link(source, destination)


# What os.link answers where a file system keeps no hard links; what a rename that never replaces
# answers where the file system (EINVAL, ENOTSUP) or the C library (ENOSYS: none here) cannot.
_NO_HARD_LINKS = frozenset({errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS})
_NO_NATIVE_RENAME = frozenset({errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS})
_AT_FDCWD = -100  # linux/fcntl.h
_RENAME_NOREPLACE = 1  # linux/fs.h
_RENAME_EXCL = 0x00000004  # macOS sys/stdio.h


def _rename_no_replace(source: str, destination: str, system: str) -> int:
    """Rename ``source`` to ``destination`` unless that name exists, by the C library's own call
    (never a guessed syscall number): 0 when renamed, else the errno (ENOSYS: no such call)."""
    import ctypes

    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if system == "linux":
            call = libc.renameat2
            call.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
            arguments: tuple[Any, ...] = (
                _AT_FDCWD,
                os.fsencode(source),
                _AT_FDCWD,
                os.fsencode(destination),
                _RENAME_NOREPLACE,
            )
        else:
            call = libc.renamex_np
            call.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
            arguments = (os.fsencode(source), os.fsencode(destination), _RENAME_EXCL)
        call.restype = ctypes.c_int
    except AttributeError, OSError, TypeError:
        return errno.ENOSYS
    ctypes.set_errno(0)
    if call(*arguments) == 0:
        return 0
    return ctypes.get_errno() or errno.EIO


def _resolve(path: Path) -> str:
    return posixpath.realpath(os.fspath(path))


# File system types as /proc/self/mountinfo (Linux) or statfs (macOS) name them. FUSE as such
# is neither: it carries local and network file systems alike, so only a named one counts.
_NETWORK_FILESYSTEMS = frozenset(
    {"nfs", "nfs4", "cifs", "smb3", "smbfs", "9p", "ncpfs", "afs", "afpfs", "webdav", "davfs", "ceph"}
    | {"glusterfs", "lustre", "gpfs", "beegfs", "orangefs", "sshfs", "fuse.sshfs", "fuse.glusterfs"}
    | {"fuse.cephfs", "fuse.rclone", "fuse.s3fs", "fuse.gcsfuse", "fuse.davfs2"}
)
_LOCAL_FILESYSTEMS = frozenset(
    {"ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "f2fs", "jfs", "reiserfs", "bcachefs", "nilfs2"}
    | {"vfat", "msdos", "exfat", "ntfs", "ntfs3", "fuseblk", "hfs", "hfsplus", "apfs", "ufs", "tmpfs"}
)


def _kind(fstype: str) -> str:
    name = fstype.casefold()
    return "network" if name in _NETWORK_FILESYSTEMS else "local" if name in _LOCAL_FILESYSTEMS else "unknown"


def _network_identity(source: str, root: str = "/") -> str | None:
    """A network mount by the share it shows: ``server:/export``, ``//server/share`` (an SMB
    URL's account name left out: the same share), and the folder of it mounted when not its top."""
    text = source.strip()
    if not text or text.casefold() in {"none", "-"}:
        return None
    text = re.sub(r"^//[^/@]*@", "//", text)
    return "net:" + text + ("" if root in ("", "/") else root)


def _unescape_mount(text: str) -> str:
    """mountinfo writes a space, a tab, a newline and a backslash as octal escapes."""
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), text)


# udev's names for block devices by what their file system says (first) and by their partition:
# links to the device, whose "major:minor" /sys/class/block/<device>/dev gives.
_DISK_LINKS = (("/dev/disk/by-uuid", "uuid:"), ("/dev/disk/by-partuuid", "partuuid:"))
_DEVICE_NUMBER = re.compile(r"\d+:\d+")


def _device_number(device: str) -> str | None:
    """``major:minor`` of the block device ``device`` (a /dev path or a link to one), read-only."""
    try:
        name = os.path.basename(os.path.realpath(device))
    except OSError, ValueError:
        return None
    if not name or name in {".", ".."}:
        return None
    number = _read(f"/sys/class/block/{name}/dev").strip()
    return number if _DEVICE_NUMBER.fullmatch(number) else None


def _block_identities() -> dict[str, str]:
    """{"major:minor": identity} of every block device udev names by a file system UUID or, for
    one without, a partition UUID. Read-only: links listed and resolved, nothing opened."""
    found: dict[str, str] = {}
    for folder, prefix in _DISK_LINKS:
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            continue
        for name in names:
            number = _device_number(os.path.join(folder, name))
            if number is not None and number not in found:
                found[number] = prefix + name
    return found


def linux_volume(path: str, mountinfo: str) -> Volume | None:
    """The mount holding the resolved ``path``, from /proc/self/mountinfo: the longest mount
    point above it, the last listed of equal ones (mounts stack). A volume that is not mounted is
    not listed, so its empty mount point folder is on the mount above it.

    Its identity is the file system's own: the UUID of its block device (by the mount's device
    number, else its source device: btrfs shows an anonymous number), or the share of a network
    one; a mount of a folder inside it (a bind mount, a btrfs subvolume) adds that folder. A
    tmpfs, an overlay or a device without a UUID has none (None), whatever its type and place."""
    best: tuple[str, str, str, str, str] | None = None
    for line in mountinfo.splitlines():
        before, separator, after = line.partition(" - ")
        fields, tail = before.split(), after.split()
        if not separator or len(fields) < 5 or len(tail) < 2:
            continue
        point = _unescape_mount(fields[4])
        if path != point and not path.startswith(point.rstrip("/") + "/"):
            continue
        if best is None or len(point) >= len(best[1]):
            best = (tail[0], point, _unescape_mount(tail[1]), fields[2], _unescape_mount(fields[3]))
    if best is None:
        return None
    fstype, _point, source, number, root = best
    kind = _kind(fstype)
    if kind == "network":
        return Volume(_network_identity(source, root), kind)
    devices = _block_identities()
    identity = devices.get(number)
    if identity is None and source.startswith("/dev/"):
        source_number = _device_number(source)
        identity = devices.get(source_number) if source_number is not None else None
    if identity is not None and root not in ("", "/"):
        identity += "|" + root
    return Volume(identity, kind)


def _statfs(path: str) -> tuple[str, str, str] | None:
    """(type, mount point, source) of the file system holding ``path``: macOS statfs, through
    the C library with the 64-bit-inode struct statfs (sys/mount.h); None when it fails."""
    import ctypes

    class StatFs(ctypes.Structure):
        _fields_ = (
            ("f_bsize", ctypes.c_uint32),
            ("f_iosize", ctypes.c_int32),
            ("f_blocks", ctypes.c_uint64),
            ("f_bfree", ctypes.c_uint64),
            ("f_bavail", ctypes.c_uint64),
            ("f_files", ctypes.c_uint64),
            ("f_ffree", ctypes.c_uint64),
            ("f_fsid", ctypes.c_int32 * 2),
            ("f_owner", ctypes.c_uint32),
            ("f_type", ctypes.c_uint32),
            ("f_flags", ctypes.c_uint32),
            ("f_fssubtype", ctypes.c_uint32),
            ("f_fstypename", ctypes.c_char * 16),
            ("f_mntonname", ctypes.c_char * 1024),
            ("f_mntfromname", ctypes.c_char * 1024),
            ("f_flags_ext", ctypes.c_uint32),
            ("f_reserved", ctypes.c_uint32 * 7),
        )

    try:
        libc = ctypes.CDLL(None, use_errno=True)
        # Intel Macs name the 64-bit-inode call statfs$INODE64; Apple silicon has only that one.
        call = getattr(libc, "statfs$INODE64", None) or libc.statfs
        call.argtypes = (ctypes.c_char_p, ctypes.POINTER(StatFs))
        call.restype = ctypes.c_int
        found = StatFs()
        if call(os.fsencode(path), ctypes.byref(found)) != 0:
            return None
        return (
            os.fsdecode(found.f_fstypename),
            os.fsdecode(found.f_mntonname),
            os.fsdecode(found.f_mntfromname),
        )
    except AttributeError, OSError, TypeError, ValueError:
        return None


_ATTR_BIT_MAP_COUNT = 5  # sys/attr.h
_ATTR_VOL_INFO = 0x80000000
_ATTR_VOL_UUID = 0x00040000


def _volume_uuid(mount_point: str) -> str | None:
    """The UUID of the macOS volume mounted at ``mount_point``: getattrlist ATTR_VOL_UUID, else
    what ``diskutil info`` says (read-only); None when neither tells."""
    import ctypes
    import uuid

    class AttrList(ctypes.Structure):
        _fields_ = (
            ("bitmapcount", ctypes.c_ushort),
            ("reserved", ctypes.c_uint16),
            ("commonattr", ctypes.c_uint32),
            ("volattr", ctypes.c_uint32),
            ("dirattr", ctypes.c_uint32),
            ("fileattr", ctypes.c_uint32),
            ("forkattr", ctypes.c_uint32),
        )

    try:
        libc = ctypes.CDLL(None, use_errno=True)
        call = libc.getattrlist
        call.argtypes = (ctypes.c_char_p, ctypes.POINTER(AttrList), ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint)
        call.restype = ctypes.c_int
        wanted = AttrList(_ATTR_BIT_MAP_COUNT, 0, 0, _ATTR_VOL_INFO | _ATTR_VOL_UUID, 0, 0, 0)
        buffer = ctypes.create_string_buffer(64)
        if call(os.fsencode(mount_point), ctypes.byref(wanted), buffer, len(buffer), 0) == 0:
            raw = buffer.raw[4:20]  # u_int32_t length, then the uuid_t
            if any(raw):
                return str(uuid.UUID(bytes=raw)).upper()
    except AttributeError, OSError, TypeError, ValueError:
        pass
    return _diskutil_uuid(mount_point)


def _diskutil_uuid(mount_point: str) -> str | None:
    """``diskutil info -plist``'s VolumeUUID of the volume at ``mount_point`` (it only reads)."""
    import plistlib

    output = _run(["/usr/sbin/diskutil", "info", "-plist", mount_point], timeout=10)
    try:
        found = plistlib.loads(output.encode("utf-8")).get("VolumeUUID") if output else None
    except AttributeError, ValueError, plistlib.InvalidFileException:
        return None
    return found.upper() if isinstance(found, str) and re.fullmatch(r"[0-9A-Fa-f-]{36}", found) else None


def _parent_pid() -> int:
    return os.getppid()


def _is_file(path: str) -> bool:
    return os.path.isfile(path)


def _which(name: str) -> str | None:
    return shutil.which(name)


def _listdir(path: str) -> list[str]:
    try:
        return os.listdir(path)
    except OSError:
        return []


def _readlink(path: str) -> str:
    try:
        return os.readlink(path)
    except OSError, ValueError:
        return ""


def _realpath(path: str) -> str:
    try:
        return os.path.realpath(path)
    except OSError, ValueError:
        return path


def _head(path: str, size: int = 4096) -> bytes:
    """The first bytes of a file (b"" when it cannot be read)."""
    try:
        with open(path, "rb") as handle:
            return handle.read(size)
    except OSError:
        return b""


def is_snap_browser(path: str) -> bool:
    """A browser from a snap package (Ubuntu's chromium): it runs confined and cannot use a
    profile outside the home folder, nor a HOME of its own. Ubuntu's ``chromium-browser`` is a
    small script that starts the snap."""
    if path.startswith("/snap/") or _realpath(path).startswith("/snap/"):
        return True
    head = _head(path)
    return head.startswith(b"#!") and b"/snap/" in head


def session_environment(raw: str) -> dict[str, str]:
    """The display variables of ``systemctl --user show-environment`` output."""
    found: dict[str, str] = {}
    for line in raw.splitlines():
        name, sep, value = line.partition("=")
        if sep and name in _DISPLAY_ENV and value:
            found[name] = value
    return found


# --- the machine --------------------------------------------------------------------------------


def linux_boot_time(now: float) -> float | None:
    fields = _read("/proc/uptime").split()
    try:
        return now - float(fields[0])
    except IndexError, ValueError:
        return None


_BOOTTIME = re.compile(r"\bsec\s*=\s*(\d+)")


def macos_boot_time() -> float | None:
    match = _BOOTTIME.search(_run(["sysctl", "-n", "kern.boottime"], timeout=5))
    return float(match.group(1)) if match else None


def linux_boot_ends(raw: str, since: float, until: float) -> list[dict[str, Any]]:
    """The ends of earlier boots between two moments, from ``journalctl --list-boots -o json``."""
    try:
        boots = json.loads(raw) if raw.strip() else []
    except ValueError:
        return []
    events = []
    for boot in boots if isinstance(boots, list) else []:
        if not isinstance(boot, dict) or boot.get("index") == 0:
            continue  # the running boot has not ended
        try:
            ended = int(boot["last_entry"]) / 1e6
        except KeyError, TypeError, ValueError:
            continue
        if since <= ended <= until:
            events.append({"id": "stopped", "ts": ended, "process": "", "reason": "", "action": ""})
    return sorted(events, key=lambda item: item["ts"])


def parse_lsof_pid(raw: str) -> int | None:
    """The first ``p<pid>`` line of ``lsof -F p``."""
    for line in raw.splitlines():
        if line.startswith("p") and line[1:].strip().isdigit():
            return int(line[1:].strip())
    return None


_SS_PID = re.compile(r"\bpid=(\d+)")


def parse_ss_pid(raw: str, port: int) -> int | None:
    """The listener's pid from ``ss -Hltnp`` (``users:(("python",pid=1234,fd=6))``); None when
    no line listens on ``port`` or ss may not say whose it is (another user's process)."""
    for line in raw.splitlines():
        fields = line.split()
        if len(fields) >= 4 and fields[3].endswith(f":{int(port)}") and (match := _SS_PID.search(line)):
            return int(match.group(1))
    return None


def proc_listening_inodes(raw: str, port: int) -> set[str]:
    """Socket inodes listening on ``port`` in a /proc/net/tcp(6) table (state 0A = LISTEN)."""
    inodes: set[str] = set()
    for line in raw.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 10 or fields[3] != "0A":
            continue
        try:
            listening = int(fields[1].rsplit(":", 1)[1], 16)
        except IndexError, ValueError:
            continue
        if listening == int(port) and fields[9] != "0":
            inodes.add(fields[9])
    return inodes


def proc_listeners(port: int) -> set[str] | None:
    """The socket inodes listening on ``port`` by /proc/net/tcp(6): an empty set when nothing
    listens there, None when neither table can be read (no /proc)."""
    tables = [_read("/proc/net/tcp"), _read("/proc/net/tcp6")]
    if not any(tables):
        return None
    return set().union(*(proc_listening_inodes(table, port) for table in tables))


def proc_listener_pid(port: int, inodes: set[str] | None = None) -> int | None:
    """The process holding a listening socket of ``port`` (``inodes``: its sockets, when already
    read), from /proc: only this user's processes can be read, which is what TOW needs."""
    inodes = proc_listeners(port) if inodes is None else inodes
    if not inodes:
        return None
    wanted = {f"socket:[{inode}]" for inode in inodes}
    for entry in _listdir("/proc"):
        if not entry.isdigit():
            continue
        if any(_readlink(f"/proc/{entry}/fd/{fd}") in wanted for fd in _listdir(f"/proc/{entry}/fd")):
            return int(entry)
    return None


def proc_parent_and_command(pid: int) -> tuple[int, str] | None:
    """(parent pid, command line) from /proc when ps is not installed."""
    stat = _read(f"/proc/{int(pid)}/stat")
    fields = stat.rpartition(")")[2].split()  # the name in (...) may contain spaces
    if len(fields) < 2:
        return None
    try:
        parent = int(fields[1])
    except ValueError:
        return None
    return parent, " ".join(_read(f"/proc/{int(pid)}/cmdline").split("\0")).strip()


def parse_ps(raw: str) -> tuple[int, str] | None:
    """``ps -o ppid=,command=``: (parent pid, command line)."""
    text = raw.strip()
    if not text:
        return None
    head, _, command = text.partition(" ")
    try:
        return int(head), command.strip()
    except ValueError:
        return None


def _pwd() -> Any:
    """The password database module (Linux and macOS only; ImportError elsewhere)."""
    import importlib

    return importlib.import_module("pwd")


class PosixBackend:
    """Linux (``name="linux"``) or macOS (``name="macos"``)."""

    def __init__(self, name: str = "linux", *, home: str | None = None) -> None:
        if name not in ("linux", "macos"):
            raise ValueError(f"not a POSIX platform: {name!r}")
        self.name = name
        self._home = home

    def publish_exclusive(self, source: Path, destination: Path) -> None:
        """Publish the complete, flushed ``source`` as ``destination`` without ever replacing it:
        a hard link, or where the file system has none (exFAT, FAT, SMB without unix extensions,
        some FUSE) the system's own rename that refuses an existing name - renameat2
        RENAME_NOREPLACE on Linux, renamex_np RENAME_EXCL on macOS - which moves ``source``.
        An existing destination (a link too) raises FileExistsError; a file system or C library
        that offers neither fails closed with the link's error; any other error is raised."""
        try:
            _hard_link(source, destination)
            return
        except OSError as error:
            if error.errno not in _NO_HARD_LINKS:
                raise
            refused = error
        code = _rename_no_replace(os.fspath(source), os.fspath(destination), self.name)
        if code == 0:
            return
        if code == errno.EEXIST:
            raise FileExistsError(code, os.strerror(code), os.fspath(destination))
        if code in _NO_NATIVE_RENAME:
            raise refused
        raise OSError(code, os.strerror(code), os.fspath(destination))

    @property
    def home(self) -> str:
        value = self._home if self._home is not None else os.environ.get("HOME", "")
        return value if value.startswith("/") else ""

    # --- the machine ----------------------------------------------------------------------------

    def boot_time(self, now: float | None = None) -> float | None:
        current = time.time() if now is None else now
        return linux_boot_time(current) if self.name == "linux" else macos_boot_time()

    def asleep_seconds(self) -> float | None:
        if self.name == "linux":
            with_sleep, without = _clock("CLOCK_BOOTTIME"), _clock("CLOCK_MONOTONIC")
        else:
            with_sleep, without = _clock("CLOCK_MONOTONIC"), _clock("CLOCK_UPTIME_RAW")
        if with_sleep is None or without is None:
            return None
        return max(0.0, with_sleep - without)

    def logon_time(self) -> float | None:
        return None  # not kept in a form worth parsing; the messages do without it

    def shutdown_reasons(self, since: float, until: float) -> list[dict[str, Any]]:
        if self.name != "linux":
            return []
        return linux_boot_ends(_run(["journalctl", "--list-boots", "-o", "json", "--no-pager"]), since, until)

    # --- processes ------------------------------------------------------------------------------

    def popen_options(self, *, new_group: bool = False, hidden: bool = True) -> dict[str, Any]:
        del hidden  # there is no console window to hide
        return {"start_new_session": new_group, "close_fds": True}

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
        """Start ``argv`` in its own session, so it outlives this process and its terminal."""
        if require_breakaway:
            from tow.autostart import service_manager

            if service_manager() is not None:
                raise OSError("the updater needs an independent service-manager job")
        with _common.output_to(log_path) as output:
            process = subprocess.Popen(
                list(argv),
                cwd=str(cwd) if cwd else None,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=output,
                start_new_session=True,
                close_fds=True,
            )
        return int(process.pid)

    def process_alive(self, pid: int) -> bool:
        try:
            pid = int(pid)
        except TypeError, ValueError:
            return False
        if pid <= 0 or _reap(pid):
            return False
        try:
            _signal(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # another user's process
        except OSError:
            return False
        return True

    def bind_children(self) -> bool:
        return False  # each child watches its parent itself (die_with_parent)

    def die_with_parent(self, parent_pid: int) -> bool:
        """This process gets SIGTERM when ``parent_pid`` ends (Linux; best effort).

        False on macOS, or when the parent is already gone - the caller's own watch on the
        parent covers both.
        """
        if self.name != "linux" or not _set_parent_death_signal(signal.SIGTERM):
            return False
        return _parent_pid() == int(parent_pid)  # it may have ended before the call

    def terminate(self, pid: int, timeout: float = 10.0) -> bool:
        """SIGTERM to the process (its whole group when it leads one), SIGKILL after ``timeout``."""
        try:
            pid = int(pid)
        except TypeError, ValueError:
            return False
        if pid <= 0:
            return False
        group = _group_of(pid) == pid
        for sig, wait in ((signal.SIGTERM, timeout), (_KILL, 2.0)):
            try:
                (_signal_group if group else _signal)(pid, sig)
            except ProcessLookupError:
                return True
            except OSError:
                return False
            if _common.wait_gone(self.process_alive, pid, wait):
                return True
        return False

    def _listener(self, port: int) -> int | None:
        """The process listening on ``port``. macOS: lsof. Linux: ``ss`` (iproute2) first - lsof
        can hang on a stale NFS mount -, then /proc, then lsof, and only while the answer is
        not known: a tool that is missing, fails or names no PID (another account's process)
        passes the question on, and lsof is not asked when /proc shows nothing listens there.
        Each tool is bounded by its timeout."""
        port = int(port)

        def lsof() -> int | None:
            if not _which("lsof"):
                return None
            return parse_lsof_pid(_run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fp"], timeout=10))

        if self.name != "linux":
            return lsof()
        if _which("ss") and (pid := parse_ss_pid(_run(["ss", "-Hltnp", "sport", "=", f":{port}"], timeout=10), port)):
            return pid
        inodes = proc_listeners(port)
        if inodes == set():
            return None  # nothing listens there
        if inodes and (pid := proc_listener_pid(port, inodes)) is not None:
            return pid
        return lsof()

    def _process(self, pid: int) -> tuple[int, str] | None:
        found = parse_ps(_run(["ps", "-ww", "-o", "ppid=,command=", "-p", str(pid)], timeout=5))
        if found is None and self.name == "linux":
            found = proc_parent_and_command(pid)  # ps is not installed everywhere either
        return found

    def process_command(self, pid: int) -> str | None:
        try:
            pid = int(pid)
        except TypeError, ValueError:
            return None
        if pid <= 0:
            return None
        found = self._process(pid)
        return found[1] if found and found[1] else None

    def port_owner(self, port: int) -> dict[str, Any] | None:
        pid = self._listener(port)
        if pid is None:
            return None
        own = self._process(pid)
        if own is None:
            return {"pid": pid, "cmd": "", "parent": None, "parent_cmd": ""}
        parent, command = own
        parent_info = self._process(parent)
        return {"pid": pid, "cmd": command, "parent": parent, "parent_cmd": parent_info[1] if parent_info else ""}

    def bring_to_front(self, pid: int) -> None:
        del pid  # the window manager shows a new browser window itself

    def open_url(self, url: str) -> bool:
        # Linux without a desktop (SSH, a server): webbrowser would run a text browser in the
        # terminal and hold it. The caller shows the address instead.
        if self.name == "linux" and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            return False
        return _common.open_url(url)

    def ui_language(self) -> str | None:
        """The terminal's language from the locale variables ("ru_RU.UTF-8" -> "ru-RU"), or None
        ("C" and "POSIX" name no language)."""
        for name in ("LC_ALL", "LC_MESSAGES", "LANG"):
            value = os.environ.get(name, "").split(".", 1)[0].split("@", 1)[0].strip()
            if value and value not in {"C", "POSIX"}:
                return value.replace("_", "-")
        return None

    # --- browsers and folders -------------------------------------------------------------------

    def browser_executables(self) -> list[str]:
        found: list[str] = []
        if self.name == "macos":
            bases = ["/Applications", *([posixpath.join(self.home, "Applications")] if self.home else [])]
            found += [posixpath.join(base, app) for app in _MAC_APPS for base in bases]
            found = [path for path in found if _is_file(path)]
        for name in _LINUX_BROWSERS:
            with contextlib.suppress(OSError):
                if path := _which(name):
                    found.append(path)
        found = list(dict.fromkeys(found))
        if self.name == "linux":  # a snap cannot use TOW's profile: any other browser first
            found.sort(key=is_snap_browser)
        return found

    def browser_launch(self, executable: str, home: Path) -> dict[str, Any]:
        """How to start the sign-in browser: extra ``args``, the ``env`` (HOME and the XDG
        folders inside ``home``, the session folder; the display variables a service started
        before the desktop lacks) and whether it is ``confined`` (a snap: refused, it cannot
        use a profile outside the home folder)."""
        env = dict(os.environ)
        if self.name == "linux":
            if not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")):
                found = session_environment(_run(["systemctl", "--user", "show-environment"], timeout=5))
                env.update({name: value for name, value in found.items() if not env.get(name)})
            # X11 finds its cookie in ~/.Xauthority by default: the real home, not the session's.
            xauthority = posixpath.join(self.home, ".Xauthority") if self.home else ""
            if not env.get("XAUTHORITY") and xauthority and _is_file(xauthority):
                env["XAUTHORITY"] = xauthority
        env["HOME"] = str(home)
        env["XDG_CONFIG_HOME"] = str(home / ".config")
        env["XDG_CACHE_HOME"] = str(home / ".cache")
        env["XDG_DATA_HOME"] = str(home / ".local" / "share")
        return {
            "args": list(BROWSER_FLAGS),
            "env": env,
            "confined": self.name == "linux" and is_snap_browser(executable),
        }

    def folder_shared(self, path: Path) -> bool | None:
        """Other accounts may open ``path``: any group or other permission bit."""
        try:
            return bool(path.stat().st_mode & 0o077)
        except OSError:
            return None

    def root_shared(self, path: Path) -> bool | None:
        """Every account may write in the install root (a group permission may be deliberate)."""
        try:
            return bool(path.stat().st_mode & 0o002)
        except OSError:
            return None

    def make_private(self, path: Path, *, created: bool = False) -> bool:
        """``path`` becomes 0700 when this account owns it (what this process ``created`` it
        does); True when read back private."""
        del created
        own_uid = getattr(os, "geteuid", None)
        try:
            if own_uid is None or path.stat().st_uid != own_uid():
                return False
            path.chmod(0o700)
        except OSError:
            return False
        return self.folder_shared(path) is False

    def allocated_bytes(self, path: Path) -> int | None:
        """What ``path`` (not followed) takes on disk: its allocated 512-byte blocks."""
        try:
            blocks = getattr(path.lstat(), "st_blocks", None)
        except OSError, ValueError:
            return None
        return blocks * 512 if type(blocks) is int and blocks >= 0 else None

    def volume(self, path: Path) -> Volume | None:
        """The mount holding the existing ``path``: /proc/self/mountinfo on Linux; statfs and the
        volume's UUID on macOS (the URL it is mounted from for a network one)."""
        real = _resolve(path)
        if self.name == "linux":
            return linux_volume(real, _read("/proc/self/mountinfo"))
        found = _statfs(real)
        if found is None:
            return None
        fstype, mount_point, source = found
        kind = _kind(fstype)
        if kind == "network":
            return Volume(_network_identity(source), kind)
        uuid = _volume_uuid(mount_point)
        return Volume("uuid:" + uuid if uuid else None, kind)

    # --- handing a folder back to its owner (tow permissions fix) ---------------------------------

    def elevated(self) -> bool:
        geteuid = getattr(os, "geteuid", None)
        return geteuid is not None and geteuid() == 0

    def current_account(self) -> str | None:
        geteuid = getattr(os, "geteuid", None)
        return str(geteuid()) if geteuid is not None else None

    def invoking_account(self) -> str | None:
        """Under sudo, the account that ran it (SUDO_UID); else this one."""
        sudo_uid = os.environ.get("SUDO_UID", "").strip()
        if self.elevated() and sudo_uid.isdigit():
            return sudo_uid
        return self.current_account()

    def session_account(self) -> str | None:
        """sudo says who ran it: the same account as ``invoking_account``."""
        return self.invoking_account()

    def personal_account(self, account: str) -> bool:
        return account.isdigit() and account != "0"

    def folder_owner(self, path: Path) -> str | None:
        try:
            return str(path.stat().st_uid)
        except OSError:
            return None

    def shared_for(self, path: Path, account: str, *, root: bool = False) -> bool | None:
        """The mode bits say it, whoever asks: the root when every account may write in it."""
        del account
        return self.root_shared(path) if root else self.folder_shared(path)

    def account_name(self, account: str) -> str:
        try:
            return str(_pwd().getpwuid(int(account)).pw_name)
        except ImportError, KeyError, ValueError:
            return account

    def account_of(self, name: str) -> str | None:
        if name.isdigit():
            return name
        try:
            return str(_pwd().getpwnam(name).pw_uid)
        except ImportError, KeyError:
            return None

    def hand_over(self, path: Path, account: str) -> bool:
        """``account`` becomes the owner of ``path`` (root only may give a folder away) and the
        folder becomes 0700; True when read back so."""
        chown = getattr(os, "chown", None)
        if not self.personal_account(account) or chown is None:
            return False
        try:
            if path.is_symlink():
                return False
            if self.folder_owner(path) != account:
                chown(path, int(account), -1, follow_symlinks=False)
            path.chmod(0o700)
        except OSError:
            return False
        return self.folder_owner(path) == account and self.folder_shared(path) is False

    def protected_folders(self) -> list[str]:
        roots = list(SYSTEM_FOLDERS)
        if self.home:
            roots += [posixpath.join(self.home, name) for name in HOME_FOLDERS]
        return roots
