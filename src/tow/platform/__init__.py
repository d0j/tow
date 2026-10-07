"""Everything that differs between Windows, Linux and macOS, behind one interface.

The rest of TOW is OS-neutral: it asks ``current()`` - the backend of this machine - for boot
and logon times, shutdown reasons, starting and stopping processes, browsers and the folders
a download must never go to. Tests inject another backend (``use``) to run the logic of the
other systems on any machine.

Every probe fails soft: an unknown fact is ``None`` / ``[]`` / ``False``, never an exception.
"""

from __future__ import annotations

import os
import stat
import sys
import threading
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol

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
    "backend_for",
    "current",
    "is_link_like",
    "is_plain_dir",
    "is_plain_file",
    "is_windows",
    "private_folders",
    "private_root",
    "set_backend",
    "this_os",
    "use",
    "use_private_files",
    "user_id",
    "user_name",
]
