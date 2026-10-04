"""Where the supervisor keeps its files, and how other processes talk to it.

Everything lives in ``tow.paths.run_dir()`` (``data/run/``):

- ``run.lock`` - held by the one running ``tow run`` (single instance, released by the OS
  when the process dies, so a crash never leaves a stale lock);
- ``run.pid``  - who holds it: pid, version, port, start time (information only);
- ``status.json`` - what the supervisor is doing (web server, jobs, restarts), for Settings;
- ``schedule.json`` - its own last scheduled check and night copy attempt (they survive a
  restart: the cadence never depends on manual checks or progress passes);
- ``control/restart`` and ``control/stop`` - requests from other processes (Settings, the
  ``tow stop|restart`` CLI, the updater). No signals: they do not exist the same way on every OS.
"""

from __future__ import annotations

import os
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tow import paths
from tow.diagnostic_json import encode_object, epoch, read_object
from tow.paths import repo_root
from tow.platform import locks
from tow.store import atomic_write_text, init_lock_file

CONTROL_ACTIONS = ("restart", "stop")


def install_root() -> Path:
    """The install folder: ``<root>/app`` (this code), ``<root>/data``, ``<root>/config.yaml``."""
    return paths.root()


def run_dir() -> Path:
    return paths.run_dir()


def logs_dir() -> Path:
    return paths.logs_dir()


def control_dir() -> Path:
    return run_dir() / "control"


def lock_path() -> Path:
    return run_dir() / "run.lock"


def pid_path() -> Path:
    return run_dir() / "run.pid"


def status_path() -> Path:
    return run_dir() / "status.json"


def schedule_path() -> Path:
    return run_dir() / "schedule.json"


def child_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """The environment for ``tow`` children and for a supervisor started by the OS.

    A Task Scheduler task, a systemd unit or a LaunchAgent starts ``python -m tow run``
    without the launcher's variables: the runtime layout is resolved here once (config and
    data next to the code's folder) and handed to every child.
    """
    env = dict(os.environ if base is None else base)
    root = install_root()
    if root != repo_root():
        env.setdefault("TOW_HOME", str(root / "data"))
        if (root / "config.yaml").is_file():
            env.setdefault("TOW_CONFIG", str(root / "config.yaml"))
    env["TOW_ROOT"] = str(root)
    env["PYTHONIOENCODING"] = "utf-8"  # output files keep Cyrillic readable
    return env


def read_json(path: Path) -> dict[str, Any]:
    try:
        return read_object(path)
    except OSError, UnicodeError, ValueError, TypeError, RecursionError:
        return {}


def write_json(path: Path, value: dict[str, Any], *, durable: bool = True) -> None:
    """Replace the file whole; ``durable=False`` skips the flush to disk (status, rewritten often)."""
    text = encode_object(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    if durable:
        atomic_write_text(path, text)
        return
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def request(action: str, *, by: str = "cli", operation_id: str | None = None) -> dict[str, Any]:
    """Ask the running supervisor to restart the web server or to stop; it polls every second."""
    if action not in CONTROL_ACTIONS:
        raise ValueError(f"unknown control action: {action}")
    payload = {
        "action": action,
        "by": by,
        "operation_id": operation_id or f"{action}-{uuid.uuid4().hex[:12]}",
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    write_json(control_dir() / action, payload)
    return payload


def take_request(action: str, directory: Path | None = None) -> dict[str, Any] | None:
    """The pending request for ``action`` (removed), or None."""
    path = (directory or control_dir()) / action
    if not path.exists():
        return None
    payload = read_json(path) or {"action": action}
    with suppress(OSError):
        path.unlink()
    return payload


class InstanceLock:
    """The single-instance lock: one ``tow run`` per install (per data folder)."""

    def __init__(self, path: Path | None = None):
        self.path = path or lock_path()
        self._handle: Any = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            init_lock_file(handle)
            if not locks.lock(handle, wait=False):
                handle.close()
                return False
        except OSError:
            handle.close()
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        with suppress(OSError):
            locks.unlock(handle)
        handle.close()


def busy() -> bool:
    """TOW of this install is running - the supervisor, or a web server left on its port.

    ``tow setup`` asks this before it replaces ``app/.venv``: Windows lets a folder whose programs
    are running be renamed, so the running TOW would lose its libraries mid-way.
    """
    if running() is not None:
        return True
    from tow.config import DEFAULTS, load_config, port_of
    from tow.supervisor._os import port_open

    try:
        port = port_of(load_config())
    except Exception:  # noqa: BLE001 - a broken config: the default port tells as well
        port = int(DEFAULTS["port"])
    return port_open(port)


def running() -> dict[str, Any] | None:
    """The running supervisor of this install (its pid file), or None when none runs.

    Probed through the lock itself, not the pid: a crashed supervisor leaves its pid file but
    never the lock.
    """
    if not lock_path().exists():
        return None
    probe = InstanceLock()
    if probe.acquire():
        probe.release()
        return None
    return read_json(pid_path()) or {"pid": None}


def status() -> dict[str, Any]:
    """What the running supervisor last reported (empty when none runs)."""
    return read_json(status_path()) if running() else {}


def _epoch(value: Any) -> float:
    return epoch(value) or 0.0


def next_check_at(interval_sec: int, health: dict[str, Any] | None = None) -> float | None:
    """When the next scheduled check starts (epoch seconds), or None when nothing says so.

    The running supervisor's own plan (``status.json``) first; else its last scheduled start
    (``schedule.json``) or the last scheduled check in the state (``health.auto_at_ts``) plus
    the interval. Never ``at_ts``: manual checks and progress passes move it too.
    """
    next_jobs = status().get("next")
    planned = next_jobs.get("check") if isinstance(next_jobs, dict) else None
    if isinstance(planned, str):
        with suppress(ValueError, OverflowError, OSError):
            when = epoch(datetime.fromisoformat(planned).timestamp())
            if when is not None:
                return when
    last = _epoch(read_json(schedule_path()).get("check_started_at")) or _epoch(
        health.get("auto_at_ts") if isinstance(health, dict) else None
    )
    return epoch(last + int(interval_sec)) if last > 0 else None
