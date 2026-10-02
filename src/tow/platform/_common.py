"""Helpers both backends share: quiet commands, process output to a log, waiting for an exit."""

from __future__ import annotations

import contextlib
import subprocess
import time
import webbrowser
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import IO


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
