"""A child of ``tow run`` ends with it: ``tow serve --parent-pid N``.

The supervisor stops its web server itself; this covers a supervisor that cannot (killed,
crashed, its machine's session ending). Three layers, each best effort:

- Windows: the supervisor's job object ends its children with it (``platform.bind_children``);
- Linux: the kernel sends SIGTERM when the parent ends (``platform.die_with_parent``);
- everywhere: a thread asks every two seconds whether the parent still runs and, when it does
  not, lets the server finish its requests and stop (and ends the process if it hangs).

Without them the orphaned server kept the port: the next ``tow run`` refused to start ("port
busy") while ``tow stop`` said nothing was running.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from typing import Any

POLL_SEC = 2.0
# After asking the server to stop, this long before the process is ended anyway.
GRACE_SEC = 15.0

LOG = logging.getLogger("tow.serve")


def _parent_check(
    parent_pid: int, *, parent: Callable[[], int] = os.getppid, windows: bool | None = None
) -> Callable[[int], bool]:
    from tow import platform

    if windows if windows is not None else platform.is_windows():
        return platform.current().process_alive
    if parent() != int(parent_pid):
        # Started through something in between (not by that parent itself): its pid is all there is.
        return platform.current().process_alive
    return lambda pid: parent() == int(pid)


def watch_parent(
    parent_pid: int,
    on_gone: Callable[[], Any],
    *,
    alive: Callable[[int], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    interval: float = POLL_SEC,
) -> threading.Thread:
    """Call ``on_gone`` once ``parent_pid`` has ended (from a daemon thread).

    On Linux and macOS a process whose parent ended is handed to another one: ``getppid()``
    changing says so even when the old pid already belongs to a new process (asking whether
    that pid is alive would wait for ever then). Windows keeps the creator's pid instead, so it
    asks whether the parent still runs.
    """
    from tow import platform

    platform.current().die_with_parent(parent_pid)
    check = alive or _parent_check(parent_pid)

    def run() -> None:
        while check(parent_pid):
            sleep(interval)
        on_gone()

    thread = threading.Thread(target=run, name="tow-parent-watch", daemon=True)
    thread.start()
    return thread


def end_with_parent(
    parent_pid: int,
    server: Any,
    *,
    alive: Callable[[int], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    exit_process: Callable[[int], Any] = os._exit,
    grace: float = GRACE_SEC,
) -> threading.Thread:
    """Stop the uvicorn ``server`` when ``tow run`` (``parent_pid``) is gone."""

    def gone() -> None:
        LOG.warning("tow run (pid %s) has ended: the web server stops too", parent_pid)
        server.should_exit = True
        sleep(grace)
        exit_process(0)  # still here after the grace: a request hangs; the port must come free

    return watch_parent(parent_pid, gone, alive=alive, sleep=sleep)
