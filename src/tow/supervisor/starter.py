"""``tow start``: TOW in the background and its page in the browser.

What the start files run after the first-start setup: ``Start TOW.cmd`` of the Windows bundle,
``Start TOW.command`` on macOS and ``start-tow`` on Linux (``scripts/tow-start.cmd``,
``scripts/tow-start``). When this install's ``tow run`` runs already, only the page opens.
Otherwise ``tow run`` starts detached and without a window - it outlives the terminal that
started it - and the page opens once ``/healthz`` answers. A ``tow run`` that ends before it
answers (another program on the port, a broken config) is reported at once, with its log.
Only this install's page counts (``layout.install_id``): another TOW folder answering on the
same port is not "started".
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tow.supervisor import layout

# ``tow run`` gives a fresh web server 90 s to answer; a first start needs a little more.
DEFAULT_WAIT_SEC = 120.0
POLL_SEC = 0.5


@dataclass
class Deps:
    running: Callable[[], dict[str, Any] | None]
    spawn: Callable[[], int]
    alive: Callable[[int], bool]
    healthy: Callable[[int], bool]
    open_url: Callable[[str], bool]
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic


def stderr_log() -> Path:
    """Where the started ``tow run`` writes what it prints before its own log is open."""
    return layout.logs_dir() / "run-stderr.log"


def output_size() -> int:
    """How much the started ``tow run`` logs have already written (before a new start).

    Every start appends to the file: one that reached the cap of the web server's appended log
    becomes ``run-stderr.log.1`` first (the older one goes), so it never grows for ever.
    """
    from tow.supervisor import APPENDED_LOG_BYTES, _cap_log

    _cap_log(stderr_log(), APPENDED_LOG_BYTES)
    try:
        return stderr_log().stat().st_size
    except OSError:
        return 0


def output_since(offset: int, *, lines: int = 3) -> list[str]:
    """The last lines a ``tow run`` wrote after ``offset``: why it ended (``port N is already in
    use``, a broken config.yaml), for the window that started it."""
    try:
        with stderr_log().open("rb") as handle:
            end = handle.seek(0, 2)
            handle.seek(max(0, offset, end - 64 * 1024))
            text = handle.read().decode("utf-8", "replace")
    except OSError:
        return []
    found = [line.strip() for line in text.splitlines() if line.strip()]
    return found[-lines:]


def run_argv() -> list[str]:
    """``python -m tow run`` of this environment; ``pythonw`` on Windows (no console window)."""
    executable = sys.executable
    windowless = Path(executable).with_name("pythonw.exe")
    if Path(executable).name.lower() == "python.exe" and windowless.is_file():
        executable = str(windowless)
    return [executable, "-m", "tow", "run"]


def _spawn() -> int:
    from tow import platform

    return platform.current().spawn_detached(
        run_argv(), hidden=True, log_path=stderr_log(), cwd=layout.install_root(), env=layout.child_env()
    )


def default_deps() -> Deps:
    from tow import platform
    from tow.watchdog import healthy

    install = layout.install_id()
    return Deps(
        running=layout.running,
        spawn=_spawn,
        alive=lambda pid: platform.current().process_alive(pid),
        # This install's page only: a copy of the folder started on the same port answers too.
        healthy=lambda port: healthy(port, install=install),
        open_url=lambda url: platform.current().open_url(url),
    )


def start(
    port: int, *, wait: float = DEFAULT_WAIT_SEC, browser: bool = True, deps: Deps | None = None
) -> dict[str, Any]:
    """Start ``tow run`` unless it runs, wait for its page, open it.

    ``state``: ``running`` (it ran already), ``started``, ``exited`` (the new ``tow run`` ended
    before it answered) or ``timeout``; ``browser`` says whether a browser was opened.
    """
    deps = deps or default_deps()
    url = f"http://127.0.0.1:{port}/"
    pid = None if deps.running() is not None else deps.spawn()
    deadline = deps.monotonic() + max(0.0, wait)
    while True:
        if deps.healthy(port):
            opened = bool(browser and deps.open_url(url))
            state = "running" if pid is None else "started"
            return {"ok": True, "state": state, "url": url, "pid": pid, "browser": opened}
        # Ended: unless another `tow run` took over in the meantime (then that one is waited for).
        if pid is not None and not deps.alive(pid) and deps.running() is None:
            return {"ok": False, "state": "exited", "url": url, "pid": pid, "browser": False}
        if deps.monotonic() >= deadline:
            return {"ok": False, "state": "timeout", "url": url, "pid": pid, "browser": False}
        deps.sleep(POLL_SEC)
