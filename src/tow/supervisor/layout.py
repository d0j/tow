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
from tow.store import atomic_write_text, init_lock_file, replace_with_retry

CONTROL_ACTIONS = ("restart", "stop")
# A non-durable write (status.json, every change) waits at most about 0.3 s for a reader: the
# supervisor's loop ticks every second, and the next tick tries again.
STATUS_REPLACE_ATTEMPTS = 5


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
    # A TOW_ROOT this install does not follow (left from a move) stays as it was: the children
    # ignore it the same way, and run.log names it at every start (outside_overrides).
    if not paths.root_env_ignored(env.get("TOW_ROOT", "")):
        env["TOW_ROOT"] = str(root)
    env["PYTHONIOENCODING"] = "utf-8"  # output files keep Cyrillic readable
    return env


OVERRIDES = ("TOW_ROOT", "TOW_HOME", "TOW_CONFIG", "TOW_MASTER_KEY_FILE")


def outside_overrides(env: dict[str, str] | None = None) -> list[str]:
    """The variables among ``OVERRIDES`` that lead this runtime install out of its folder.

    They are honoured everywhere alike (the launchers, Task Scheduler, systemd, launchd all
    start TOW in the owner's environment), so a variable set for the whole account - left from
    another install, say - makes this one use another data folder, config or key. Said in
    run.log at every start; a development checkout is not a portable install and is skipped.
    A ``TOW_ROOT`` of another folder is not followed (``paths.root_env_ignored``) but named too.
    """
    values = os.environ if env is None else env
    root = install_root()
    if root == repo_root():
        return []
    found = []
    for name in OVERRIDES:
        value = values.get(name, "").strip()
        if not value:
            continue
        path = Path(os.path.abspath(value)) if os.path.isabs(value) else None
        if path is not None and not path.is_relative_to(Path(os.path.abspath(root))):
            found.append(name)
    return found


def read_json(path: Path) -> dict[str, Any]:
    try:
        return read_object(path)
    except OSError, UnicodeError, ValueError, TypeError, RecursionError:
        return {}


def write_json(path: Path, value: dict[str, Any], *, durable: bool = True) -> None:
    """Replace the file whole; ``durable=False`` skips the flush to disk (status, rewritten often).

    Either way a reader holding the file (Settings, ``tow status``; on Windows that refuses the
    replace) is waited for briefly, and a failed write leaves no temporary file behind.
    """
    text = encode_object(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    if durable:
        atomic_write_text(path, text)
        return
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        replace_with_retry(temporary, path, attempts=STATUS_REPLACE_ATTEMPTS)
    except BaseException:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise


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


def install_id() -> str:
    """This install as its web server names itself on ``/healthz`` (asked from this computer).

    A hash of the code folder and the install root: the same for every process of this install,
    another one for a copy of the folder - so ``tow start``, ``tow setup`` and the supervisor
    never take another TOW answering on the same port for their own. The root counts too: a
    copy whose environment still runs the original's code (its ``.venv`` holds absolute paths)
    has the original's code folder, and its copied status.json names the original's web
    server - which it would otherwise stop as a server its own supervisor left behind.
    """
    import hashlib

    folder = os.path.normcase(str(repo_root()))
    try:
        install = os.path.normcase(os.path.realpath(install_root()))
    except RuntimeError:  # a wheel without a layout: the code folder alone
        install = ""
    named = f"{folder}\0{install}" if install else folder
    return hashlib.sha256(named.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def own_server_command(command: str, logs: Path | None = None) -> bool:
    """A command line of this install's web server: ``-m tow serve`` with its own log file."""

    def norm(text: str) -> str:
        return os.path.normcase(text.replace('"', ""))

    log = (logs or logs_dir()) / "serve.log"
    return "-m tow serve" in norm(command) and norm(str(log)) in norm(command)


def configured_port() -> int:
    from tow.config import DEFAULTS, load_config, port_of

    try:
        return port_of(load_config())
    except Exception:  # noqa: BLE001 - a broken config: the default port tells as well
        return int(DEFAULTS["port"])


def port_holder(port: int) -> str | None:
    """Who holds ``port``: None (nobody), ``ours`` (a web server of this install, e.g. one a
    killed supervisor left behind) or ``other`` (another program, or another TOW folder)."""
    from tow.supervisor._os import port_open
    from tow.watchdog import healthy

    if not port_open(port):
        return None
    if healthy(port, install=install_id()):
        return "ours"
    from tow import platform

    # Not answering as this install: a hung server of it is still known by its command line.
    owner = platform.current().port_owner(port) or {}
    return "ours" if own_server_command(str(owner.get("cmd") or "")) else "other"


def busy() -> bool:
    """TOW of this install is running - the supervisor, or a web server of it left on its port.

    ``tow setup`` asks this before it replaces ``app/.venv``: Windows lets a folder whose programs
    are running be renamed, so the running TOW would lose its libraries mid-way. Another
    program or another TOW folder on the port does not make this install busy
    (``port_holder``).
    """
    if running() is not None:
        return True
    return port_holder(configured_port()) == "ours"


def setup_check() -> int:
    """What ``tow setup`` (``scripts/tow-setup.cmd``, ``scripts/tow``) asks before it replaces
    ``app/.venv``: 4 when TOW of this install runs (the launcher says to stop it first), else 0.

    Another program or another TOW folder on the port does not stop the setup - it was said to
    be "TOW is running" before - but is named, with the port: TOW starts once it is free.
    """
    if running() is not None:
        return 4
    port = configured_port()
    holder = port_holder(port)
    if holder == "ours":
        return 4
    if holder == "other":
        from tow import i18n

        with suppress(Exception):  # a broken config: the default language
            i18n.use(i18n.terminal_language())
        print(i18n.t("cli.setup.port_other", port=port), flush=True)
    return 0


def interrupted_update() -> bool:
    """An update was cut off while it switched the code: ``app/`` may hold half of the new one.

    scripts/update.py keeps ``.update-switch.json`` in the install while it switches an archive
    install and holds ``.update.lock`` while it runs; it starts TOW itself then. A record without
    that lock was left by an update that did not finish (an accepted record is finished code, a
    restored one the previous code with its data back).
    """
    root = install_root()
    record = root / ".update-switch.json"
    if not record.exists():
        return False
    found = read_json(record)
    if found.get("phase") == "accepted" or found.get("restored") is True:
        return False
    try:
        with (root / ".update.lock").open("r+b") as handle:
            if not locks.lock(handle, wait=False):
                return False  # the update runs: it starts TOW and checks it
            locks.unlock(handle)
    except OSError:
        pass  # no lock file (or none to open): no update holds it
    return True


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


def topic_timer_status(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Read the supervisor's actual plan; never invent a deadline when it is stopped."""
    import time

    from tow.topic_timers import policy

    now = time.time()
    report = status()
    planned = report.get("topic_timers")
    planned = planned if isinstance(planned, dict) else {}
    batch = read_json(schedule_path()).get("timer_batch")
    batch = batch if isinstance(batch, dict) else {}
    job = report.get("job")
    timer_running = isinstance(job, dict) and job.get("name") == "timer"
    result = {}
    for topic in state.get("topics") or []:
        item = policy(topic)
        if item is None:
            continue
        tid = str(topic.get("id") or "")
        entry = planned.get(tid)
        next_at = 0.0
        if isinstance(entry, dict) and entry.get("revision") == item["revision"]:
            with suppress(ValueError, OverflowError, OSError, TypeError):
                next_at = _epoch(datetime.fromisoformat(str(entry.get("at"))).timestamp())
        if topic.get("paused"):
            phase = "paused"
        elif topic.get("once_done"):
            phase = "done"
        elif not report:
            phase = "stopped"
        elif timer_running and isinstance(batch.get(tid), dict) and batch[tid].get("revision") == item["revision"]:
            phase = "running"
        else:
            phase = "scheduled" if next_at > now else "queued" if next_at else "waiting"
        result[tid] = {"minutes": item["minutes"], "state": phase, "next_at": next_at if report else 0, "now": now}
    return result


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
