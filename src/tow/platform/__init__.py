"""Everything that differs between Windows, Linux and macOS, behind one interface.

The rest of TOW is OS-neutral: it asks ``current()`` - the backend of this machine - for boot
and logon times, shutdown reasons, starting and stopping processes, browsers and the folders
a download must never go to. Tests inject another backend (``use``) to run the logic of the
other systems on any machine.

Every probe fails soft: an unknown fact is ``None`` / ``[]`` / ``False``, never an exception.
"""

from __future__ import annotations

import codecs
import ntpath
import os
import stat
import sys
import threading
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal, Protocol

from tow.platform._common import Volume

NAMES = ("windows", "linux", "macos")


class Backend(Protocol):
    """What TOW asks the operating system (docs/PORTABLE.md §9)."""

    name: str

    def boot_time(self, now: float | None = None) -> float | None: ...

    def asleep_seconds(self) -> float | None: ...

    def logon_time(self) -> float | None: ...

    def shutdown_reasons(self, since: float, until: float) -> list[dict[str, Any]]: ...

    def popen_options(self, *, new_group: bool = False, hidden: bool = True) -> dict[str, Any]: ...

    def spawn_detached(
        self,
        argv: Sequence[str],
        *,
        hidden: bool = True,
        log_path: Path | None = None,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        require_breakaway: bool = False,
    ) -> int: ...

    def process_alive(self, pid: int) -> bool: ...

    def process_command(self, pid: int) -> str | None: ...

    def publish_exclusive(self, source: Path, destination: Path) -> None: ...

    def bind_children(self) -> bool: ...

    def die_with_parent(self, parent_pid: int) -> bool: ...

    def terminate(self, pid: int, timeout: float = 10.0) -> bool: ...

    def port_owner(self, port: int) -> dict[str, Any] | None: ...

    def browser_executables(self) -> list[str]: ...

    def browser_launch(self, executable: str, home: Path) -> dict[str, Any]: ...

    def bring_to_front(self, pid: int) -> None: ...

    def protected_folders(self) -> list[str]: ...

    def folder_shared(self, path: Path) -> bool | None: ...

    def root_shared(self, path: Path) -> bool | None: ...

    def make_private(self, path: Path, *, created: bool = False) -> bool: ...

    # What a file takes on disk, as the file system says it (``allocated_file_bytes``).
    def allocated_bytes(self, path: Path) -> int | None: ...

    # The file system an existing path is on (``volume_of``).
    def volume(self, path: Path) -> Volume | None: ...

    # `tow permissions`: who owns a folder, and giving an install back to its owner.
    def elevated(self) -> bool: ...

    def current_account(self) -> str | None: ...

    def invoking_account(self) -> str | None: ...

    def session_account(self) -> str | None: ...

    def personal_account(self, account: str) -> bool: ...

    def folder_owner(self, path: Path) -> str | None: ...

    def shared_for(self, path: Path, account: str, *, root: bool = False) -> bool | None: ...

    def account_name(self, account: str) -> str: ...

    def account_of(self, name: str) -> str | None: ...

    def hand_over(self, path: Path, account: str) -> bool: ...

    def open_url(self, url: str) -> bool: ...

    def ui_language(self) -> str | None: ...


def this_os() -> str:
    """``windows``, ``macos`` or ``linux`` (any other POSIX system is treated as Linux)."""
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def backend_for(name: str) -> Backend:
    """A backend for the named system (on any machine: its pure logic is testable everywhere)."""
    if name == "windows":
        from tow.platform.windows import WindowsBackend

        return WindowsBackend()
    if name in ("linux", "macos"):
        from tow.platform.posix import PosixBackend

        return PosixBackend(name)
    raise ValueError(f"unknown platform: {name!r}")


_LOCK = threading.Lock()
_native: Backend | None = None
_injected: Backend | None = None


def current() -> Backend:
    """The backend in use: an injected one (tests), else this machine's."""
    global _native
    if _injected is not None:
        return _injected
    with _LOCK:
        if _native is None:
            _native = backend_for(this_os())
        return _native


def is_windows() -> bool:
    """The backend in use is Windows: what a page or a hint should say (commands, paths)."""
    return current().name == "windows"


def set_backend(backend: Backend | None) -> None:
    """Use ``backend`` from now on (``None``: this machine's again)."""
    global _injected
    _injected = backend


@contextmanager
def use(backend: Backend) -> Iterator[Backend]:
    """Use ``backend`` inside the block, then whatever was in use before."""
    global _injected
    previous = _injected
    _injected = backend
    try:
        yield backend
    finally:
        _injected = previous


def user_id() -> int:
    """This account's numeric user id on Linux and macOS (launchd's ``gui/<uid>``); 0 on Windows."""
    getuid = getattr(os, "getuid", None)
    return int(getuid()) if getuid is not None else 0


def user_name() -> str:
    """This account's login name as the session says it (``USER``, Windows ``USERNAME``); "" unknown."""
    return os.environ.get("USER") or os.environ.get("USERNAME") or ""


def windows_program(*parts: str) -> str:
    """A program of Windows itself, under ``%SystemRoot%\\System32`` (``windows_program("schtasks.exe")``),
    never one a PATH search finds: the venv's Scripts folder comes first there. Without a usable
    ``SystemRoot`` (missing, or not a full path) the default Windows folder."""
    roots = (os.environ.get(name, "").strip() for name in ("SystemRoot", "SYSTEMROOT", "windir"))
    root = next((value for value in roots if ntpath.isabs(value) and ntpath.splitdrive(value)[0]), r"C:\Windows")
    return ntpath.join(root, "System32", *parts)


def powershell_program() -> str:
    """Windows PowerShell 5.1, which every Windows has, by its full path."""
    return windows_program("WindowsPowerShell", "v1.0", "powershell.exe")


def console_codec() -> str:
    """The code page console programs write into a pipe: on Windows the OEM one (``schtasks`` and
    Windows PowerShell write it, not the ANSI code page Python reads by default), else UTF-8."""
    try:
        codecs.lookup("oem")  # the codec exists on Windows only
    except LookupError:
        return "utf-8"
    return "oem"


def decode_output(data: bytes) -> str:
    """A program's output as text: UTF-8 when it is (Linux, macOS, a PowerShell told so), else
    in the console's code page (``console_codec``); never raises."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode(console_codec(), errors="replace")


def is_link_like(info: os.stat_result) -> bool:
    """``info`` (from ``lstat``) is a symbolic link, or on Windows any reparse point (a junction,
    a mount point): an entry that may lead somewhere else, never followed or trusted as data."""
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def is_plain_file(info: os.stat_result) -> bool:
    """``info`` (from ``lstat``) is a regular file and not a link of any kind."""
    return stat.S_ISREG(info.st_mode) and not is_link_like(info)


def is_plain_dir(info: os.stat_result) -> bool:
    """``info`` (from ``lstat``) is a directory and not a link, junction or mount point."""
    return stat.S_ISDIR(info.st_mode) and not is_link_like(info)


# From this length on a full Windows path goes to the file API in its extended form: with long
# paths off (Windows' default) a folder's path ends at 248 characters, a file's at 260.
LONG_PATH_FROM = 248
_EXTENDED = "\\\\?\\"
_EXTENDED_UNC = "\\\\?\\UNC\\"


def extended_windows_path(text: str) -> str:
    """A full Windows path (``C:\\...``, ``\\\\host\\share\\...``) in the extended form that
    lifts the length limit (``\\\\?\\C:\\...``, ``\\\\?\\UNC\\host\\share\\...``). That form takes
    every character literally, so ``.``, ``..`` and slashes are resolved by text first. Any other
    text - already extended, a device, a relative or POSIX path - is returned as it is. The same
    on every system (``ntpath``)."""
    if text.replace("/", "\\").startswith((_EXTENDED, "\\\\.\\")):
        return text
    drive = ntpath.splitdrive(text)[0]
    if not drive or not ntpath.isabs(text):
        return text
    normal = ntpath.normpath(text)
    return _EXTENDED_UNC + normal[2:] if normal.startswith("\\\\") else _EXTENDED + normal


def ordinary_windows_path(text: str) -> str:
    """The extended form of a path back as it is ordinarily written (any other text unchanged)."""
    if text.startswith(_EXTENDED_UNC):
        return "\\\\" + text[len(_EXTENDED_UNC) :]
    if text.startswith(_EXTENDED) and ntpath.splitdrive(text[len(_EXTENDED) :])[0]:
        return text[len(_EXTENDED) :]
    return text


def long_path(path: Path | str) -> Path:
    """``path`` as this system's file API reaches it whatever its length: on Windows a full path
    of ``LONG_PATH_FROM`` characters or more in its extended form (a torrent client writes such
    files that way; without it they look missing), anything else as it is. Give it to ``stat``,
    ``resolve`` and the like before they run, never after: they fail on the long ordinary form.
    Compare such a path with an ordinary one through ``ordinary_path``."""
    text = os.fspath(path)
    if current().name == "windows" and len(text) >= LONG_PATH_FROM:
        return Path(extended_windows_path(text))
    return Path(path)


def ordinary_path(path: Path | str) -> Path:
    """``long_path`` undone: the path as it is ordinarily written, for comparing and showing."""
    return Path(ordinary_windows_path(os.fspath(path))) if current().name == "windows" else Path(path)


def volume_of(path: Path | str) -> Volume | None:
    """The file system the existing ``path`` is on (its identity, and whether it is a local or a
    network one), or None when it cannot be told. Read-only: nothing is written or mounted."""
    try:
        return current().volume(Path(path))
    except AttributeError, OSError, ValueError:  # a backend without the probe knows nothing
        return None


def allocated_file_bytes(path: Path | str, *, info: os.stat_result | None = None) -> int | None:
    """Bytes the regular file at ``path`` already reserves on disk, never its unallocated
    logical holes, and never more than its size; ``info`` is its ``lstat`` when the caller has
    it. None: not a plain file (a link, junction or mount point never counts), or unknown.

    POSIX reports allocated 512-byte blocks. On Windows an ordinary file reserves its size; for
    a sparse or compressed one the file system says what it takes (the backend's
    ``allocated_bytes``): a compressed file takes less than its size, a cautious credit. A file
    never receives a full-size credit just because its logical length is large.
    """
    path = long_path(path)  # a client's file may be past Windows' 260 characters
    if info is None:
        try:
            info = path.lstat()
        except OSError, ValueError:
            return None
    size = info.st_size
    if not is_plain_file(info) or type(size) is not int or size < 0:
        return None
    backend = current()
    if backend.name == "windows":
        attributes = getattr(info, "st_file_attributes", None)
        if type(attributes) is not int or attributes < 0:
            return None
        if not attributes & (stat.FILE_ATTRIBUTE_SPARSE_FILE | stat.FILE_ATTRIBUTE_COMPRESSED):
            return size
        try:
            allocated = backend.allocated_bytes(path)
        except AttributeError, OSError, ValueError:  # a backend without the question: unknown
            return None
        return min(size, allocated) if type(allocated) is int and allocated >= 0 else None
    blocks = getattr(info, "st_blocks", None)
    return min(size, blocks * 512) if type(blocks) is int and blocks >= 0 else None


def plain_kind(path: Path, *, missing_ok: bool = True) -> Literal["file", "dir", "other"] | None:
    """What ``path`` itself is, from ``lstat`` (a link is never followed): ``"file"`` for a plain
    file, ``"dir"`` for a plain directory, ``"other"`` for a link, junction, mount point or special
    file, and None when nothing is there. ``missing_ok=False`` lets that FileNotFoundError through
    instead; any other OSError is always the caller's."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        if missing_ok:
            return None
        raise
    if is_plain_file(info):
        return "file"
    if is_plain_dir(info):
        return "dir"
    return "other"


def child_path(root: Path | str, name: str) -> Path:
    """``root / name`` for one plain name directly inside ``root``: no separator, drive, ``.``
    or ``..`` (ValueError otherwise). Also for a network share (``\\\\nas\\share``).

    The containment is written as normpath + startswith, the form code scanning recognises.
    """
    base = os.path.normpath(root)
    if not base.endswith(os.sep):
        base += os.sep
    path = os.path.normpath(os.path.join(base, name))
    if (
        name in ("", ".", "..")
        or os.path.basename(name) != name
        or not path.startswith(base)
        or path[len(base) :] != name
    ):
        raise ValueError("not a plain name inside its folder")
    return Path(path)


PRIVATE_UMASK = 0o077


def use_private_files() -> bool:
    """Files and folders this process creates from now on are for this user only.

    Called once at process start (the CLI and the web app), so data/, logs and temporary files
    are never readable by other accounts on Linux and macOS (umask 077). On Windows nothing
    changes here: ``private_folders`` closes keys/ and data/ instead, and what TOW creates
    inside them inherits that. True when the umask was set.
    """
    if this_os() == "windows":
        return False
    os.umask(PRIVATE_UMASK)
    return True


def private_folders(folders: Iterable[Path], *, repair: bool = True, created: bool = False) -> list[Path]:
    """The folders among ``folders`` that other accounts of this computer can still open.

    With ``repair`` such a folder is first made private when this account owns it (or this
    process has just ``created`` it): on Windows only this account, SYSTEM and Administrators
    keep access (an install in a drive root inherits "Authenticated Users: modify"), on Linux
    and macOS it becomes 0700. Missing folders, links and unknown answers are skipped; never
    raises.
    """
    backend = current()
    still_open: list[Path] = []
    for folder in folders:
        try:
            if folder.is_symlink() or folder.is_junction() or not folder.is_dir():
                continue
            if backend.folder_shared(folder) is not True:
                continue
            if repair and backend.make_private(folder, created=created):
                continue
        except AttributeError, OSError, ValueError:  # a backend without the question: unknown
            continue
        still_open.append(folder)
    return still_open


def private_root(folder: Path, *, repair: bool = True) -> bool:
    """Whether other accounts of this computer can still change the install root ``folder``.

    Through the root they could change the code TOW runs (app/, runtime/, the start files)
    and so read the master key with the owner's rights. With ``repair`` the root is first
    closed when this account owns it: on Windows only this account, SYSTEM and Administrators
    keep access and what it holds inherits that (a folder in a drive root inherits
    "Authenticated Users: modify"); on Linux and macOS it becomes 0700 when every account may
    write in it (a folder shared with a group on purpose stays as it is). A drive or
    filesystem root, a link and unknown answers are left alone; never raises.
    """
    backend = current()
    try:
        if folder.parent == folder or folder.is_symlink() or folder.is_junction() or not folder.is_dir():
            return False
        if backend.root_shared(folder) is not True:
            return False
        if repair and backend.make_private(folder):
            return False
    except AttributeError, OSError, ValueError:  # a backend without the question: unknown
        return False
    return True


__all__ = [
    "NAMES",
    "PRIVATE_UMASK",
    "Backend",
    "Volume",
    "backend_for",
    "child_path",
    "console_codec",
    "current",
    "decode_output",
    "is_link_like",
    "is_plain_dir",
    "is_plain_file",
    "is_windows",
    "plain_kind",
    "private_folders",
    "private_root",
    "set_backend",
    "this_os",
    "use",
    "use_private_files",
    "user_id",
    "user_name",
    "volume_of",
]
