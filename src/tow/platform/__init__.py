"""Everything that differs between Windows, Linux and macOS, behind one interface.

The rest of TOW is OS-neutral: it asks ``current()`` - the backend of this machine - for boot
and logon times, shutdown reasons, starting and stopping processes, browsers and the folders
a download must never go to. Tests inject another backend (``use``) to run the logic of the
other systems on any machine.

Every probe fails soft: an unknown fact is ``None`` / ``[]`` / ``False``, never an exception.
"""

from __future__ import annotations

import os
import sys
import threading
from collections.abc import Iterator, Sequence
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
    ) -> int: ...

    def process_alive(self, pid: int) -> bool: ...

    def bind_children(self) -> bool: ...

    def die_with_parent(self, parent_pid: int) -> bool: ...

    def terminate(self, pid: int, timeout: float = 10.0) -> bool: ...

    def port_owner(self, port: int) -> dict[str, Any] | None: ...

    def browser_executables(self) -> list[str]: ...

    def browser_launch(self, executable: str, home: Path) -> dict[str, Any]: ...

    def bring_to_front(self, pid: int) -> None: ...

    def protected_folders(self) -> list[str]: ...

    def open_url(self, url: str) -> bool: ...


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


PRIVATE_UMASK = 0o077


def use_private_files() -> bool:
    """Files and folders this process creates from now on are for this user only.

    Called once at process start (the CLI and the web app), so data/, logs and temporary files
    are never readable by other accounts on Linux and macOS (umask 077). On Windows the
    install folder's permissions apply and nothing changes. True when the umask was set.
    """
    if this_os() == "windows":
        return False
    os.umask(PRIVATE_UMASK)
    return True


__all__ = [
    "NAMES",
    "PRIVATE_UMASK",
    "Backend",
    "backend_for",
    "current",
    "is_windows",
    "set_backend",
    "this_os",
    "use",
    "use_private_files",
]
