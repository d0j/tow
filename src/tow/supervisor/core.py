"""``tow run``: the supervisor that manages the web server, child jobs and watchdog.

The loop ticks every second and never blocks for long:

- the web server (``tow serve``) is a child process; ``/healthz`` is asked every few seconds
  (no proxies, short timeouts). A server that exits, or stops answering for a minute while its
  port stays held, is stopped and started again after a pause that doubles from 1 s up to
  5 minutes and drops back to 1 s after ten quiet minutes. Restarts are counted; the owner is
  told after the first one and once more when they keep coming (the watchdog's messages);
- scheduled checks, progress passes and night copies are child processes too, one at a time
  (a check additionally holds ``check_run_lock`` against checks started from the web page);
- the watchdog's duties (lateness, night copies, change alerts, heartbeat, queued messages)
  run in-process every 10 minutes, without its restart part;
- other processes talk to it through files in ``data/run/control`` (see ``layout``);
- a wall clock that jumped ahead of the monotonic one means the machine slept: the overdue
  jobs run, the server gets a fresh start-up grace, and lateness counts from the wake.
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from tow import __version__
from tow.config import as_bool, interval_sec_of, port_of
from tow.supervisor import layout
from tow.supervisor.schedule import Schedule, WakeDetector, parse_backup_time

LOG = logging.getLogger("tow.supervisor")

TICK_SEC = 1.0
HEALTH_EVERY_SEC = 10.0
# A fresh server gets this long to answer /healthz before it counts as hung.
START_GRACE_SEC = 90.0
# Held port, no answer for this many probes in a row (a minute): hung.
HANG_FAILS = 6
BACKOFF_FIRST_SEC = 1.0
BACKOFF_MAX_SEC = 300.0
# A server that stayed up this long resets the pause between restarts.
STABLE_SEC = 600.0
RESTART_WINDOW_SEC = 6 * 3600
RESTART_REPEAT_COUNT = 3
# A restart asked from Settings whose new server exits this many times in a row has failed.
RESTART_OPERATION_ATTEMPTS = 3
# On stop, a running check / night copy is given this long to finish, then it is stopped.
STOP_JOB_WAIT_SEC = 10 * 60
STOP_RETRY_SEC = 10.0
FACTS_EVERY_SEC = 60.0

JOB_TIMEOUT_SEC = {"check": 3600.0, "timer": 3600.0, "progress": 600.0, "backup": 1800.0}
JOB_ARGS = {
    "check": ["check", "--apply", "--notify", "--global-only", "--json"],
    "timer": ["check", "--apply", "--notify", "--timer-only", "--json"],
    "progress": ["check", "--apply", "--notify", "--progress-only", "--json"],
    "backup": ["backup", "--json"],
}


class Child(Protocol):
    pid: int

    def poll(self) -> int | None: ...


def _now_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds")


@dataclass
class Deps:
    """Everything the supervisor asks of the system; tests pass fakes."""

    spawn: Callable[[list[str], Path, bool], Child]  # argv, output file, append
    stop: Callable[[Child], bool]
    healthy: Callable[[int], bool]
    port_open: Callable[[int], bool]
    watchdog_pass: Callable[[float | None], Any]
    send: Callable[[str], bool]
    load_config: Callable[[], dict[str, Any]]
    facts: Callable[[], dict[str, Any]]  # last_scheduled_check, last_backup_ok, topic_timers
    crash_line: Callable[[float], str] = field(default=lambda _since: "")
    now: Callable[[], float] = time.time
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    in_thread: Callable[[Callable[[], Any], str], Any] | None = None  # None: a daemon thread
    restart_marker: Callable[[str, dict[str, Any]], Any] = field(default=lambda _op, _fields: None)
    # The process listening on a port ({pid, cmd, parent, ...} or None) and stopping a process
    # tree: only for a web server of this install left behind by a supervisor that died.
    port_owner: Callable[[int], dict[str, Any] | None] = field(default=lambda _port: None)
    stop_pid: Callable[[int], bool] = field(default=lambda _pid: False)
    # The web server on the port answers /healthz as this install (layout.install_id).
    own_server: Callable[[int], bool] = field(default=lambda _port: False)
    # A TOW answers /healthz on the port - this install's or another folder's.
    tow_server: Callable[[int], bool] = field(default=lambda _port: False)


@dataclass
class _Server:
    child: Child | None = None
    stop_retry_at: float = 0.0
    started_mono: float = 0.0
    started_wall: float = 0.0
    healthy_since: float | None = None  # monotonic, since the last failed probe
    up_since: float = 0.0  # the same moment on the wall clock (status.json)
    answered: bool = False  # answered at least once since it was started
    fails: int = 0
    next_probe: float = 0.0
    next_start: float = 0.0
    backoff: float = BACKOFF_FIRST_SEC
    last_exit: str = ""
    pending_cause: str = ""  # why it was (re)started, told once it answers
    pending_crash: str = ""
    operation: dict[str, Any] | None = None  # a restart asked from Settings, until it answers
    operation_exits: int = 0  # how often the server of that restart exited before answering


@dataclass
class _Job:
    name: str
    child: Child
    started_mono: float
    started_wall: float
    stop_retry_at: float = 0.0


class Supervisor:
    def __init__(self, deps: Deps, *, python: str, home: Path | None = None):
        self.deps = deps
        self.python = python
        self.home = home or layout.logs_dir()
        cfg = deps.load_config()
        self.port = port_of(cfg)
        self.schedule = Schedule(started_at=deps.now())
        self._apply_config(cfg)
        self.server = _Server()
        self.job: _Job | None = None
        self.jobs_done: dict[str, dict[str, Any]] = {}
        self.restarts: list[float] = []
        self.restarts_alerted_at: float | None = None
        self.wake = WakeDetector()
        self.stopping: dict[str, Any] | None = None
        self.stop_deadline = 0.0
        self.finished = False
        self._facts: dict[str, Any] = {}
        self._facts_at = -1e18
        self._config_at = deps.monotonic()
        self._status_written: dict[str, Any] | None = None
        self._watchdog_thread: Any = None
        self._schedule_file = layout.schedule_path()
        self._persisted = layout.read_json(self._schedule_file)
        attempts = self._persisted.get("timer_attempts")
        self.schedule.timer_attempts = attempts if isinstance(attempts, dict) else {}
        self._control_dir = layout.control_dir()  # polled every second: resolved once
        self._status_file = layout.status_path()

    # --- configuration and facts -------------------------------------------------------------

    def _apply_config(self, cfg: dict[str, Any]) -> None:
        from tow.clock import local_zone

        self.schedule.interval_sec = interval_sec_of(cfg)
        self.schedule.backup_enabled = as_bool(cfg.get("backup_enabled"), True)
        try:
            self.schedule.backup_at = parse_backup_time(cfg.get("backup_time"))
        except ValueError as exc:
            LOG.warning("%s; using 03:30", exc)
        self.schedule.zone = local_zone()

    def _refresh(self, mono: float) -> None:
        if mono - self._config_at >= FACTS_EVERY_SEC:
            self._config_at = mono
            try:
                self._apply_config(self.deps.load_config())
            except Exception as exc:  # noqa: BLE001 - a config being edited: keep the last good one (logged)
                LOG.warning("config not reloaded: %s", type(exc).__name__)
        if mono - self._facts_at >= FACTS_EVERY_SEC:
            self._facts_at = mono
            try:
                self._facts = self.deps.facts()
                self.schedule.topic_timers = dict(self._facts.get("topic_timers") or {})
            except Exception as exc:  # noqa: BLE001 - the supervisor loop never dies on a state read (logged)
                LOG.warning("state not read: %s", type(exc).__name__)

    # --- start / stop ------------------------------------------------------------------------

    def preflight(self) -> str | None:
        """Why this supervisor must not start (another program holds the port), or None.

        A web server this install's previous supervisor started and left behind (it was killed
        or crashed) is stopped first: it is the pid that supervisor recorded in status.json, and
        it answers /healthz as this install or its command line is ``-m tow serve`` with this
        install's log (a copy of the folder has its own). Anything else on the port is never
        touched.
        """
        if not self.deps.port_open(self.port):
            return None
        if self._take_over_orphan():
            return None
        from tow.i18n import t

        # Another TOW folder (a copy, an older install) is named as such: "another program"
        # sent the owner looking for one that is not there.
        key = "supervisor.port_busy_tow" if self.deps.tow_server(self.port) else "supervisor.port_busy"
        return t(key, port=self.port)

    def _own_server_command(self, command: str) -> bool:
        return layout.own_server_command(command, self.home)

    def _take_over_orphan(self) -> bool:
        recorded = layout.read_json(self._status_file).get("server")
        pid = recorded.get("pid") if isinstance(recorded, dict) else None
        if not isinstance(pid, int) or pid <= 0:
            return False
        owner = self.deps.port_owner(self.port) or {}
        # On Windows the venv's python.exe is a launcher: the listener is its child.
        if pid not in (owner.get("pid"), owner.get("parent")):
            return False
        # The answer first: a command line read in another code page may have lost the folder's
        # letters; a hung server is still known by its command line.
        if not self.deps.own_server(self.port) and not self._own_server_command(str(owner.get("cmd") or "")):
            return False
        LOG.warning("a web server of this install was left running (pid %s): stopping it", pid)
        self.deps.stop_pid(pid)
        deadline = self.deps.monotonic() + 15.0
        while self.deps.port_open(self.port):
            if self.deps.monotonic() >= deadline:
                return False
            self.deps.sleep(0.5)
        return True

    def request_stop(self, request: dict[str, Any]) -> None:
        if self.stopping is None:
            self.stopping = request
            self.stop_deadline = self.deps.monotonic() + STOP_JOB_WAIT_SEC
            LOG.info("stop requested by %s", request.get("by") or "?")

    # --- the loop ----------------------------------------------------------------------------

    def tick(self) -> None:
        deps = self.deps
        wall, mono = deps.now(), deps.monotonic()
        if self.wake.observe(wall, mono):
            LOG.info("the machine woke up (about %d s asleep)", int(self.wake.slept_sec))
            self.server.fails = 0
            self.server.next_probe = mono + HEALTH_EVERY_SEC
            if self.server.child is not None and not self.server.answered:
                self.server.started_mono = mono  # a fresh grace after the wake
        self._control()
        self._refresh(mono)
        if self.stopping is not None:
            self._wind_down(mono)
        else:
            self._server_tick(wall, mono)
            self._jobs_tick(wall, mono)
            self._watchdog_tick(wall)
        self._write_status(wall, mono)

    def _control(self) -> None:
        stop = layout.take_request("stop", self._control_dir)
        if stop is not None:
            self.request_stop(stop)
        restart = layout.take_request("restart", self._control_dir)
        if restart is not None and self.stopping is None:
            self._restart_server(restart)

    def _wind_down(self, mono: float) -> None:
        if self.job is not None:
            if self.job.child.poll() is None and mono < self.stop_deadline:
                return  # a running check / night copy finishes first
            if not self._finish_job(self.deps.now(), mono, stopped=self.job.child.poll() is None):
                return
        if self.server.child is not None:
            if mono < self.server.stop_retry_at:
                return
            if not self.stop_child(self.server.child, "web server"):
                self.server.stop_retry_at = mono + STOP_RETRY_SEC
                return
            self.server.child = None
        self.finished = True
        LOG.info("stopped")

    def stop_child(self, child: Child, name: str) -> bool:
        """Confirm exit through the owned child handle, never just a stop command's result."""
        try:
            if child.poll() is not None:
                return True  # already reaped; do not terminate a possibly reused PID
            self.deps.stop(child)
            if child.poll() is not None:
                return True
        except Exception as exc:  # noqa: BLE001 - retain the child and retry; another child still needs cleanup
            LOG.error("%s stop was not confirmed (pid %s): %s", name, child.pid, type(exc).__name__)
            return False
        LOG.error("%s stop was not confirmed (pid %s)", name, child.pid)
        return False

    # --- the web server ----------------------------------------------------------------------

    def _start_server(self, wall: float, mono: float) -> None:
        cfg_port = self.port
        with contextlib.suppress(Exception):
            cfg_port = port_of(self.deps.load_config())
        self.port = cfg_port
        argv = [self.python, "-m", "tow", "serve", "--log-file", str(self.home / "serve.log")]
        argv += ["--parent-pid", str(os.getpid())]  # it ends with this supervisor, however that ends
        try:
            self.server.child = self.deps.spawn(argv, self.home / "serve-stderr.log", True)
        except OSError as exc:
            LOG.error("web server did not start: %s", type(exc).__name__)
            self._schedule_restart(mono, f"spawn failed: {type(exc).__name__}")
            return
        self.server.started_mono, self.server.started_wall = mono, wall
        self.server.stop_retry_at = 0.0
        self.server.healthy_since = None
        self.server.answered = False
        self.server.fails = 0
        self.server.next_probe = mono + 2.0
        LOG.info("web server started (pid %s, port %s)", self.server.child.pid, self.port)

    def _schedule_restart(self, mono: float, why: str) -> None:
        server = self.server
        server.child = None
        server.healthy_since = None
        server.next_start = mono + server.backoff
        LOG.warning("web server %s; starting again in %.0f s", why, server.backoff)
        server.backoff = min(server.backoff * 2, BACKOFF_MAX_SEC)

    def _count_restart(self, wall: float) -> None:
        self.restarts = [ts for ts in self.restarts if wall - ts < RESTART_WINDOW_SEC] + [wall]

    def _server_tick(self, wall: float, mono: float) -> None:
        server = self.server
        if server.child is None:
            if mono >= server.next_start:
                self._start_server(wall, mono)
            return
        code = server.child.poll()
        if code is not None:
            server.last_exit = f"exit code {code}"
            server.pending_cause = "crashed"
            server.pending_crash = self.deps.crash_line(server.started_wall)
            self._count_restart(wall)
            if server.operation is not None:
                # Settings follows the restart on the marker: a server that keeps crashing is a
                # failed restart, not "starting" for ever (it is still started again below).
                server.operation_exits += 1
                if server.operation_exits >= RESTART_OPERATION_ATTEMPTS:
                    self._operation(
                        "failed", error=f"the new web server exited {server.operation_exits} times (exit code {code})"
                    )
            self._schedule_restart(mono, f"exited with code {code}")
            return
        if mono < server.next_probe:
            return
        server.next_probe = mono + HEALTH_EVERY_SEC
        if self.deps.healthy(self.port):
            self._answered(wall, mono)
            return
        server.fails += 1
        server.healthy_since = None
        # Still starting, it gets the start-up grace; once it has answered, a minute of silence.
        hung = server.fails >= HANG_FAILS if server.answered else mono - server.started_mono >= START_GRACE_SEC
        if hung:
            LOG.warning("web server does not answer on port %s: stopping it", self.port)
            if not self.stop_child(server.child, "web server"):
                self._operation("failed", error="web server stop was not confirmed")
                return
            server.last_exit = "hung"
            server.pending_cause = "hung"
            server.pending_crash = ""
            self._count_restart(wall)
            if server.operation is not None:
                self._operation("failed", error="new service health check failed")
            self._schedule_restart(mono, "hung")

    def _answered(self, wall: float, mono: float) -> None:
        server = self.server
        server.fails = 0
        if server.healthy_since is None:
            server.healthy_since, server.up_since = mono, wall
        elif mono - server.healthy_since >= STABLE_SEC:
            server.backoff = BACKOFF_FIRST_SEC
        if server.answered:
            return
        server.answered = True
        LOG.info("web server answers on port %s", self.port)
        if server.operation is not None:
            self._operation("ready", process_pid=server.child.pid if server.child else None)
        if server.pending_cause:
            self._tell_restarted(wall)

    def _restart_server(self, request: dict[str, Any]) -> None:
        mono = self.deps.monotonic()
        LOG.info("restart requested by %s", request.get("by") or "?")
        self.server.operation = request if request.get("operation_id") else None
        self.server.operation_exits = 0
        self._operation("stopping")
        if self.server.child is not None and not self.stop_child(self.server.child, "web server"):
            self._operation("failed", error="web server stop was not confirmed")
            return
        self.server.child = None
        self.server.backoff = BACKOFF_FIRST_SEC
        self.server.next_start = mono
        self.server.pending_cause = ""
        self._operation("starting")
        self._start_server(self.deps.now(), mono)

    def _operation(self, status: str, **fields: Any) -> None:
        operation = self.server.operation
        if operation is None:
            return
        with contextlib.suppress(Exception):
            self.deps.restart_marker(str(operation["operation_id"]), {"status": status, **fields})
        if status in ("ready", "failed"):
            self.server.operation = None

    def _tell_restarted(self, wall: float) -> None:
        """The first restart in a while, and repeated restarts once per window, go to the owner."""
        from tow import i18n
        from tow.i18n import t
        from tow.pulse import duration

        server = self.server
        cause, crash = server.pending_cause, server.pending_crash
        server.pending_cause = server.pending_crash = ""
        try:
            lang = i18n.message_language(self.deps.load_config())
        except Exception as exc:  # noqa: BLE001 - the restart alert goes out even with a broken config
            LOG.warning("config not read for the restart alert: %s", type(exc).__name__)
            lang = i18n.DEFAULT
        lines = []
        if len(self.restarts) == 1:
            if cause == "hung":
                lines.append(t("watchdog.alert.restarted_hung", lang))
            elif crash:
                lines.append(t("watchdog.alert.restarted_crashed", lang, error=crash))
            else:
                lines.append(t("watchdog.alert.restarted", lang))
        window_start = wall - RESTART_WINDOW_SEC
        alerted = self.restarts_alerted_at is not None and self.restarts_alerted_at >= window_start
        if len(self.restarts) >= RESTART_REPEAT_COUNT and not alerted:
            self.restarts_alerted_at = wall
            lines.append(
                t(
                    "watchdog.alert.restarts_repeat",
                    lang,
                    count=len(self.restarts),
                    duration=duration(wall - self.restarts[0], lang),
                )
            )
        if lines:
            text = "\n".join(lines)
            self._background(lambda: self.deps.send(text), "tow-supervisor-alert")

    # --- scheduled jobs ----------------------------------------------------------------------

    def _persisted_ts(self, key: str) -> float:
        from tow.diagnostic_json import epoch

        return epoch(self._persisted.get(key)) or 0.0

    def _facts_now(self) -> dict[str, float]:
        from tow.diagnostic_json import epoch

        # The cadence follows this supervisor's own last scheduled start (schedule.json); the
        # state's auto_at_ts only stands in when there is none yet (an install updated from
        # 1.20 or earlier). Never at_ts: manual checks and progress passes move it every half
        # hour, and the scheduled checks stopped for good behind it.
        check = self._persisted_ts("check_started_at") or (epoch(self._facts.get("last_scheduled_check")) or 0.0)
        return {
            "last_scheduled_check": check,
            "last_backup_ok": epoch(self._facts.get("last_backup_ok")) or 0.0,
            "last_backup_attempt": self._persisted_ts("backup_attempt_at"),
        }

    def _jobs_tick(self, wall: float, mono: float) -> None:
        job = self.job
        if job is not None:
            timeout = JOB_TIMEOUT_SEC.get(job.name, 3600.0)
            if job.child.poll() is not None or mono - job.started_mono > timeout:
                self._finish_job(wall, mono, stopped=job.child.poll() is None)
            return
        for name, _late in self.schedule.due(wall, **self._facts_now()):
            if name in JOB_ARGS:
                self._start_job(name, wall, mono)
                return

    def _start_job(self, name: str, wall: float, mono: float) -> None:
        if name == "timer" and not self._reserve_timers(wall):
            return
        if name == "backup":
            # A switch made after the periodic config refresh must still prevent launch.
            try:
                self.schedule.backup_enabled = as_bool(self.deps.load_config().get("backup_enabled"), True)
            except Exception as exc:  # noqa: BLE001 - do not launch an automatic write with unreadable settings
                self.schedule.backup_enabled = False
                LOG.warning("backup settings not read: %s", type(exc).__name__)
            if not self.schedule.backup_enabled:
                return
        self.schedule.started(name, wall)
        persisted_key = {"check": "check_started_at", "backup": "backup_attempt_at"}.get(name)
        if persisted_key:
            self._persisted[persisted_key] = wall
            with contextlib.suppress(OSError):
                layout.write_json(self._schedule_file, self._persisted)
        argv = [self.python, "-m", "tow", *JOB_ARGS[name]]
        try:
            child = self.deps.spawn(argv, self.home / f"{name}-last.log", False)
        except OSError as exc:
            LOG.error("%s did not start: %s", name, type(exc).__name__)
            self.jobs_done[name] = {"at": _now_iso(wall), "ok": False, "error": type(exc).__name__}
            return
        self.job = _Job(name, child, mono, wall)
        LOG.info("%s started (pid %s)", name, child.pid)

    def _reserve_timers(self, wall: float) -> bool:
        """Persist one coalesced batch before spawn; changed policies are re-read in the child."""
        try:
            self._facts = self.deps.facts()
            self.schedule.topic_timers = dict(self._facts.get("topic_timers") or {})
            batch = {
                tid: {**self.schedule.topic_timers[tid], "started_at": wall}
                for tid, at in self.schedule.timer_due(wall).items()
                if at <= wall
            }
            if not batch:
                return False
            attempts = {
                tid: record
                for tid, record in self.schedule.timer_attempts.items()
                if tid in self._facts.get("timer_policies", self.schedule.topic_timers)
            }
            attempts.update(batch)
            persisted = {**self._persisted, "timer_attempts": attempts, "timer_batch": batch}
            layout.write_json(self._schedule_file, persisted)
        except Exception as exc:  # noqa: BLE001 - no launch without durable facts; retry the observation later
            LOG.warning("individual timers not reserved: %s", type(exc).__name__)
            self.schedule.topic_timers = {}
            self._facts_at = self.deps.monotonic()
            return False
        self._persisted = persisted
        self.schedule.timer_attempts = attempts
        return True

    def _finish_job(self, wall: float, mono: float, *, stopped: bool) -> bool:
        job = self.job
        if job is None:
            return True
        if stopped:
            if mono < job.stop_retry_at:
                return False
            if not self.stop_child(job.child, job.name):
                job.stop_retry_at = mono + STOP_RETRY_SEC
                return False
            LOG.warning("%s stopped after %.0f s", job.name, mono - job.started_mono)
        code = job.child.poll()
        self.jobs_done[job.name] = {
            "at": _now_iso(job.started_wall),
            "took_sec": round(mono - job.started_mono, 1),
            "code": code,
            "stopped": stopped,
        }
        LOG.info("%s finished (code %s)", job.name, code)
        self.job = None
        self._facts_at = -1e18  # re-read the state: the check moved its timestamps
        return True

    # --- watchdog duties ---------------------------------------------------------------------

    def _watchdog_tick(self, wall: float) -> None:
        thread = self._watchdog_thread
        if thread is not None and getattr(thread, "is_alive", lambda: False)():
            return
        if not any(name == "watchdog" for name, _ in self.schedule.due(wall, **self._facts_now())):
            return
        self.schedule.started("watchdog", wall)
        wake = self.wake.woke_at
        self._watchdog_thread = self._background(lambda: self.deps.watchdog_pass(wake), "tow-supervisor-watchdog")

    def _background(self, target: Callable[[], Any], name: str) -> Any:
        def guarded() -> None:
            try:
                target()
            except Exception as exc:  # noqa: BLE001 - a background duty must not end the supervisor
                LOG.error("%s failed: %s", name, type(exc).__name__)

        if self.deps.in_thread is not None:
            return self.deps.in_thread(guarded, name)
        thread = threading.Thread(target=guarded, name=name, daemon=True)
        thread.start()
        return thread

    # --- status ------------------------------------------------------------------------------

    def snapshot(self, wall: float, mono: float) -> dict[str, Any]:
        server = self.server
        if server.child is None:
            state = "stopped" if self.finished else "waiting"
        elif server.healthy_since is not None:
            state = "up"
        else:
            state = "not_answering" if server.answered else "starting"
        next_due = self.schedule.next_due(wall, **self._facts_now())
        return {
            "pid": os.getpid(),
            "version": __version__,
            "port": self.port,
            "at": _now_iso(wall),
            "stopping": self.stopping is not None,
            "server": {
                "state": state,
                "pid": server.child.pid if server.child is not None else None,
                "up_since": _now_iso(server.up_since) if server.healthy_since is not None else None,
                "last_exit": server.last_exit,
                "next_start_at": _now_iso(wall + max(0.0, server.next_start - mono)) if server.child is None else None,
            },
            "restarts": [_now_iso(ts) for ts in self.restarts],
            "job": {"name": self.job.name, "pid": self.job.child.pid, "since": _now_iso(self.job.started_wall)}
            if self.job
            else None,
            "jobs": self.jobs_done,
            "next": {name: _now_iso(ts) for name, ts in next_due.items()},
            "topic_timers": {
                tid: {"at": _now_iso(at), "revision": self.schedule.topic_timers[tid]["revision"]}
                for tid, at in self.schedule.timer_due(wall).items()
            },
            "woke_at": _now_iso(self.wake.woke_at) if self.wake.woke_at else None,
        }

    def _write_status(self, wall: float, mono: float) -> None:
        """status.json when something in it changed - never every tick (no disk wake-ups)."""
        current = self.snapshot(wall, mono)
        stable = {key: value for key, value in current.items() if key != "at"}
        if stable == self._status_written:
            return
        self._status_written = stable
        with contextlib.suppress(OSError):
            layout.write_json(self._status_file, current, durable=False)

    def loop(self) -> None:
        while not self.finished:
            self.tick()
            if not self.finished:
                self.deps.sleep(TICK_SEC)
