"""``tow run``, ``tow stop`` and ``tow restart``: one supervised TOW service (since 1.18).

One supervisor owns the web server and scheduled child jobs, and runs watchdog duties.
On every OS: see ``core`` for the loop, ``schedule`` for the timing and ``layout`` for its
files and the control requests other processes send it.
"""

from __future__ import annotations

import contextlib
import logging
import logging.handlers
import os
import signal
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tow.supervisor import layout
from tow.supervisor.core import LOG, Deps, Supervisor

RUN_LOG_BYTES = 5 * 1024 * 1024
RUN_LOG_BACKUPS = 3
# A stop by the OS (systemd, launchd, Ctrl+C) gives a running job this long, not ten minutes.
SIGNAL_STOP_WAIT_SEC = 20.0
# After that signal: the job, then the web server, are each stopped (10 s) and reaped (5 s).
# systemd's TimeoutStopSec and launchd's ExitTimeOut must leave at least this much.
SIGNAL_STOP_BUDGET_SEC = SIGNAL_STOP_WAIT_SEC + 2 * (10.0 + 5.0)

__all__ = ["layout", "request_restart", "request_stop", "run_supervisor"]


def _logging() -> list[logging.Handler]:
    """``data/logs/run.log``, rotated (5 MiB x 3); also the terminal when there is one.

    Only a real terminal: under systemd or launchd stderr is the journal or launchd.log, which
    would get every line twice (run.log has them all).
    """
    path = layout.logs_dir() / "run.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=RUN_LOG_BYTES, backupCount=RUN_LOG_BACKUPS, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    handlers: list[logging.Handler] = [handler]
    if _interactive(sys.stderr):  # pythonw (Task Scheduler) has no stderr at all
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
        handlers.append(console)
    for item in handlers:
        LOG.addHandler(item)
    LOG.setLevel(logging.INFO)
    return handlers


def _interactive(stream: Any) -> bool:
    try:
        return stream is not None and bool(stream.isatty())
    except AttributeError, OSError, ValueError:
        return False


def _spawn(argv: list[str], output: Path, append: bool) -> Any:
    from tow.supervisor import _os

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("ab" if append else "wb") as handle:
        return _os.spawn(argv, cwd=layout.install_root(), env=layout.child_env(), stdout=handle, stderr=handle)


def _stop_pid(pid: int, timeout: float = 10.0) -> bool:
    """Stop a process and everything it started; True when it is gone (nothing to stop counts)."""
    from tow import platform

    backend = platform.current()
    return pid <= 0 or not backend.process_alive(pid) or backend.terminate(pid, timeout)


def _stop(child: Any) -> bool:
    if child.poll() is not None:
        return True
    gone = _stop_pid(int(child.pid), 10.0)
    with contextlib.suppress(Exception):
        child.wait(timeout=5)  # reaped: no zombie on POSIX
    return gone


def _facts() -> dict[str, float]:
    from tow.diagnostic_json import epoch
    from tow.snapshots import list_snapshots
    from tow.snapshots import status as backup_status
    from tow.store import load_state

    health = load_state().get("health") or {}
    # The attempt time on purpose: failed checks are not retried every second.
    last_check = (epoch(health.get("auto_at_ts")) or 0.0) if isinstance(health, dict) else 0.0
    last_ok = epoch(backup_status().get("last_ok_at")) or 0.0
    if not last_ok:
        newest = list_snapshots(limit=1)
        last_ok = (epoch(newest[0]["created_ts"]) or 0.0) if newest else 0.0
    return {"last_scheduled_check": last_check, "last_backup_ok": last_ok}


def _expire_undo() -> None:
    """An expired "Undo" takes its saved secrets with it even when no page is opened (the web
    server did this only before a request); a postponed removal is retried here too."""
    from tow import undo

    try:
        if undo.cleanup_needed():
            undo.cleanup()
    except Exception as exc:  # noqa: BLE001 - the watchdog duty goes on; the next pass retries (logged)
        LOG.warning("undo secrets not cleaned up: %s", type(exc).__name__)


def _watchdog_pass(wake_ts: float | None) -> Any:
    from tow import pulse
    from tow.watchdog import run_watchdog

    _expire_undo()

    probes = pulse.Probes(
        machine=pulse.machine,
        shutdown_events=pulse.shutdown_events,
        port_listening=lambda _port: False,
        crash_line=lambda since: pulse.last_crash_line(layout.logs_dir(), since),
    )
    return run_watchdog(wake_ts=wake_ts, probes=probes)


def _send(text: str) -> bool:
    from tow.watchdog import send_to_messengers

    return send_to_messengers(text)


def default_deps() -> Deps:
    from tow import lifecycle, platform, pulse
    from tow.config import load_config
    from tow.supervisor import _os
    from tow.watchdog import healthy

    return Deps(
        spawn=_spawn,
        stop=_stop,
        healthy=healthy,
        port_open=_os.port_open,
        watchdog_pass=_watchdog_pass,
        send=_send,
        load_config=load_config,
        facts=_facts,
        crash_line=lambda since: pulse.last_crash_line(layout.logs_dir(), since),
        restart_marker=lifecycle.update_restart_marker,
        port_owner=lambda port: platform.current().port_owner(port),
        stop_pid=_stop_pid,
    )


def _clear_old_requests() -> None:
    for action in layout.CONTROL_ACTIONS:
        with contextlib.suppress(OSError):
            (layout.control_dir() / action).unlink(missing_ok=True)


def _acquire(lock: layout.InstanceLock, tries: int = 3) -> bool:
    for attempt in range(tries):
        if lock.acquire():
            return True
        if attempt + 1 < tries:
            time.sleep(0.3)
    return False


def _cleanup_children(supervisor: Supervisor) -> None:
    """Attempt every tracked child before releasing the instance lock on an abnormal exit."""
    children = []
    if supervisor.job is not None:
        children.append((supervisor.job.name, supervisor.job.child))
    if supervisor.server.child is not None:
        children.append(("web server", supervisor.server.child))
    for name, child in children:
        LOG.error("stopping %s (pid %s) on the way out", name, child.pid)
        supervisor.stop_child(child, name)
    supervisor.job = None
    supervisor.server.child = None


def run_supervisor(deps: Deps | None = None) -> int:
    """``tow run``: returns the exit code (0 after a requested stop)."""
    from tow import __version__, platform
    from tow.i18n import t
    from tow.supervisor import _os

    # Started by the OS (Task Scheduler, systemd, launchd) there are no launcher variables:
    # the runtime layout is resolved before anything reads data/ or config.yaml.
    os.environ.update(layout.child_env())
    lock = layout.InstanceLock()
    # A status probe (Settings, the updater) holds the lock for an instant: try a few times.
    if not _acquire(lock):
        other = layout.read_json(layout.pid_path())
        print(t("supervisor.already_running", pid=other.get("pid") or "?"))
        # Not a failure: an autostart that finds TOW running (started by hand or by an update)
        # must not be retried in a loop by systemd / launchd.
        return 0
    handlers: list[logging.Handler] = []
    supervisor: Supervisor | None = None
    try:
        handlers = _logging()
        supervisor = Supervisor(deps or default_deps(), python=_os.child_python())
        if deps is None:
            # Windows: a job object ends the web server with this process, however it ends.
            platform.current().bind_children()
        refusal = supervisor.preflight()
        if refusal:
            print(refusal)
            LOG.error(refusal)
            if os.environ.get("TOW_AUTOSTART") == "launchd":
                # launchd retries a failed agent without a limit (KeepAlive): another program on
                # the port is said once in run.log and launchd.log, not every minute for ever.
                return 0
            return 3
        _clear_old_requests()
        layout.write_json(
            layout.pid_path(),
            {"pid": os.getpid(), "version": __version__, "port": supervisor.port, "started_at": time.time()},
        )
        LOG.info("TOW %s started (pid %s)", __version__, os.getpid())

        def by_signal(_signum: int, _frame: Any) -> None:
            supervisor.request_stop({"by": "signal"})
            supervisor.stop_deadline = min(supervisor.stop_deadline, supervisor.deps.monotonic() + SIGNAL_STOP_WAIT_SEC)

        previous = signal.getsignal(signal.SIGTERM)
        with contextlib.suppress(ValueError, OSError):  # not the main thread
            signal.signal(signal.SIGTERM, by_signal)
        try:
            while not supervisor.finished:
                try:
                    supervisor.loop()
                except KeyboardInterrupt:
                    if supervisor.stopping is not None:
                        raise
                    by_signal(signal.SIGINT, None)
        finally:
            with contextlib.suppress(ValueError, OSError, TypeError):
                signal.signal(signal.SIGTERM, previous)
        return 0
    finally:
        if supervisor is not None:
            _cleanup_children(supervisor)
        with contextlib.suppress(OSError):
            layout.pid_path().unlink(missing_ok=True)
        lock.release()
        for item in handlers:
            LOG.removeHandler(item)
            item.close()


def request_stop(*, by: str = "cli") -> dict[str, Any] | None:
    """Ask the running supervisor to stop; None when none runs."""
    if layout.running() is None:
        return None
    return layout.request("stop", by=by)


def request_restart(*, by: str = "cli", operation_id: str | None = None) -> dict[str, Any] | None:
    """Ask the running supervisor to restart the web server; None when none runs."""
    if layout.running() is None:
        return None
    return layout.request("restart", by=by, operation_id=operation_id)


def wait_stopped(
    timeout: float,
    *,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> bool:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if layout.running() is None:
            return True
        sleep(0.5)
    return layout.running() is None
