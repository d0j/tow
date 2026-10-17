"""Helpers both backends share: quiet commands, process output to a log, waiting for an exit."""

from __future__ import annotations

import contextlib
import subprocess
import time
import webbrowser
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import IO, NamedTuple


class Volume(NamedTuple):
    """The file system holding a path.

    ``identity`` names that very file system the same way after a reboot or a remount, and never
    another one put in its place (the same type at the same mount point or drive letter): one of
    ``VOLUME_IDENTITY_PREFIXES`` - Linux "uuid:"/"partuuid:" (the block device's file system or
    partition UUID), macOS "uuid:" (the volume UUID), Windows "win-volume:" (the volume GUID and
    serial number), and "net:"/"win-share:" for the share a network one shows. None when the
    system cannot name it so (a tmpfs, an overlay, a device without a UUID, a failed lookup):
    unknown, never a guess from a type, mount point or drive letter.

    ``kind`` is "local", "network" or "unknown" (a type not known to be either: FUSE in general,
    for one, also carries local file systems)."""

    identity: str | None
    kind: str


# What a positive identity starts with. A recorded identity of any other shape (1.33.1's
# development builds wrote a type and mount point, or a bare GUID) matches no volume: the owner
# accepts the folder's disk again.
VOLUME_IDENTITY_PREFIXES = ("uuid:", "partuuid:", "net:", "win-volume:", "win-share:")


def run_quiet(args: list[str], *, timeout: float = 20, creationflags: int = 0) -> str:
    """The command's output when it succeeds, "" when it fails, is missing or hangs."""
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            creationflags=creationflags,
        )
    except OSError, subprocess.SubprocessError, ValueError:
        return ""
    return result.stdout if result.returncode == 0 else ""


@contextlib.contextmanager
def output_to(log_path: Path | None) -> Iterator[IO[bytes] | int]:
    """Where a detached process writes: appended to ``log_path``, or nowhere."""
    if log_path is None:
        yield subprocess.DEVNULL
        return
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "ab") as handle:  # the child keeps its own handle
        yield handle


def wait_gone(alive: Callable[[int], bool], pid: int, timeout: float, *, step: float = 0.1) -> bool:
    """True when ``pid`` has exited within ``timeout`` seconds."""
    deadline = time.monotonic() + max(0.0, timeout)
    while alive(pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(step)
    return True


def open_url(url: str) -> bool:
    try:
        return bool(webbrowser.open(url))
    except webbrowser.Error, OSError:
        return False
