"""Update this TOW install to a release tag (or commit) and start it again; roll back on failure.

    <python> <TOW>/app/scripts/update.py --ref v1.18.0

Run it with the install's base Python, not the one in app/.venv: ``uv sync`` replaces the
venv's files, which Windows cannot do while they run. ``tow update --ref <tag>`` prints the
exact command; on Windows ``scripts\\deploy.ps1 -Ref <tag>`` runs it. Standard library only, and
Python 3.11 syntax (deploy.ps1 may fall back to any Python 3.11+): no ``except A, B:``.

uv, Python and uv's cache come from the install, exactly as the launchers (scripts/tow,
scripts/tow-env.cmd) give them: ``launcher_env``.

Steps (each one checked; nothing is reported as done without its read-back):

1. one update at a time (``<TOW>/.update.lock``); refuse local edits of the code and an install
   that still runs the five Windows tasks of 1.17 (it switches with v1.20.0 first); fetch;
   refuse a target older than v1.18.0 (no ``tow run`` to start);
2. stop TOW: ``tow run`` gets the stop request (it lets a running check finish); only if it does
   not stop in time is it stopped forcibly, with its web server and job (status.json). With
   autostart on Linux or macOS the OS manager stops it too (``systemctl --user stop``,
   ``launchctl bootout``), so it does not start TOW again in the middle of the update;
3. snapshot ``data/`` and ``config.yaml`` into ``<TOW>/backup/update-<time>-before-<tag>``
   (no keys, LAN token, browser profiles, sign-in sessions or logs);
4. check out the target, ``uv sync --frozen --no-dev``;
5. start TOW (its autostart if it is on - ``systemctl --user start``, ``launchctl bootstrap``,
   the task "TOW" - else ``tow run`` in the background);
6. ``/healthz`` must report the target version and ``/health.json`` must read the state;
7. on any failure each rollback step runs on its own: stop the new one, check out and sync the
   previous code, put back data and config if the new version changed them, start the previous
   one and check it - and the report says which step failed;
8. on success prune old update snapshots (the newest 5; night copies, key copies and anything
   else in backup/ are never touched).

``<TOW>/update-state.json`` records the run (the watchdog holds back for 30 minutes while it says
``in_progress``).
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.request import ProxyHandler, build_opener

APP = Path(__file__).resolve().parents[1]
# Never in an update snapshot: credentials, keys, browser profiles, sign-in sessions, locks,
# the supervisor's run files, temporary files and the logs. A rollback leaves what the snapshot
# leaves out as it is: the failed version's logs are what tells why it failed.
SKIP_FILES = frozenset({"lan-auth.token", "master.key", "sessions.json"})
SKIP_DIRS = frozenset({"browser-auth", "run", "tmp", "keys", "logs"})
SNAPSHOT_RE = re.compile(r"^(?:update|data)-(\d{8}-\d{6})-before-")
# The oldest version an install that runs as one process can go to: older ones have no `tow run`.
MINIMUM_TARGET = (1, 18, 0)
UNIT_NAME = "tow.service"
AGENT_LABEL = "io.tow"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# English texts; the install's language file (src/tow/locales, section "update") wins when it
# has them. Read once at the start: the checkout replaces those files on the way.
TEXTS = {
    "not_runtime": (
        "{app} is not a runtime install (config.yaml and data/ next to it); development checkouts are not updated"
    ),
    "local_edits": "the code in {app} has local changes; nothing was updated",
    "busy": "another update is running ({lock})",
    "five_tasks": (
        "this install still runs the five Windows tasks of 1.17 (TOW-serve, TOW-check, ...): update it to"
        " v1.20.0 first and run `tow autostart migrate --apply` there; nothing was updated"
    ),
    "too_old": (
        "{ref} is TOW {version}: an install that runs as one process goes no further back than v1.18.0"
        " (older versions have no `tow run`); nothing was updated"
    ),
    "start": "TOW update: {previous} -> {target} ({ref}) in {root}",
    "stopping": "stopping TOW...",
    "not_stopped": "TOW did not stop within {minutes} min; it was stopped forcibly",
    "port_busy": "port {port} is still in use after stopping TOW",
    "snapshot": "data and config snapshot: {path}",
    "snapshot_failed": "the snapshot failed ({error}); nothing was updated",
    "sync_failed": "uv sync failed: {error}",
    "unhealthy": "the new version did not answer as {version} within {seconds} s",
    "failed": "update failed: {error}; restoring {previous}",
    "rollback_step": "rollback - {step}: {result}",
    "rolled_back": "the previous version is back and answers",
    "rollback_failed": "the previous version did not come back: start TOW by hand (tow run) and look at data/logs",
    "data_restored": "data and config were put back from the snapshot (the new version had changed them)",
    "ok": "TOW {version} is running and answers on 127.0.0.1:{port}",
    "aborted": "update aborted: {error}",
}


class UpdateError(RuntimeError):
    pass


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _messages(app: Path, root: Path) -> dict[str, str]:
    """The update texts in the owner's language (config.yaml ``language``), English otherwise."""
    lang = "en"
    with contextlib.suppress(OSError, UnicodeError):
        match = re.search(r"(?m)^language:\s*['\"]?([A-Za-z-]+)", (root / "config.yaml").read_text(encoding="utf-8"))
        if match and match.group(1).lower() != "auto":
            lang = match.group(1).lower()
    texts = dict(TEXTS)
    for code in dict.fromkeys(["en", lang]):
        with contextlib.suppress(OSError, UnicodeError, ValueError):
            catalog = json.loads((app / "src" / "tow" / "locales" / f"{code}.json").read_text(encoding="utf-8"))
            section = catalog.get("update") if isinstance(catalog, dict) else None
            if isinstance(section, dict):
                texts.update({key: value for key, value in section.items() if isinstance(value, str)})
    return texts


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _kept(relative: Path) -> bool:
    if any(part in SKIP_DIRS for part in relative.parts[:-1]):
        return False
    return relative.name not in SKIP_FILES and not relative.name.endswith(".lock")


def data_files(data: Path) -> dict[str, str]:
    """Every file an update snapshot keeps, by relative path, with its SHA-256."""
    files = {}
    if data.is_dir():
        for path in sorted(data.rglob("*")):
            relative = path.relative_to(data)
            if path.is_file() and not path.is_symlink() and _kept(relative):
                files[relative.as_posix()] = _hash(path)
    return files


def launcher_env(
    app: Path, base: Mapping[str, str] | None = None, *, uv: str | None = None
) -> tuple[str, dict[str, str]]:
    """uv and the environment the launchers give TOW for this install (``<TOW>/app`` = ``app``).

    The same as scripts/tow and scripts/tow-env.cmd for a runtime install (tests/test_launchers.py
    keeps the three in step): the install's runtime/bin/uv before uv on PATH; Python, its
    downloads and uv's cache inside <TOW>/runtime, only uv-managed Pythons; the environment in
    app/.venv; TOW_ROOT; UTF-8 output.
    """
    root = app.parent
    runtime = root / "runtime"
    env = dict(os.environ if base is None else base)
    env.update(
        {
            "TOW_ROOT": str(root),
            "PYTHONIOENCODING": "utf-8",
            "UV_PYTHON_INSTALL_DIR": str(runtime / "python"),
            "UV_PYTHON_BIN_DIR": str(runtime / "bin"),
            "UV_CACHE_DIR": str(runtime / "cache"),
            "UV_PROJECT_ENVIRONMENT": str(app / ".venv"),
            "UV_MANAGED_PYTHON": "1",
        }
    )
    own = runtime / "bin" / ("uv.exe" if os.name == "nt" else "uv")
    return uv or (str(own) if own.is_file() else "uv"), env


def version_tuple(text: str) -> tuple[int, ...] | None:
    match = re.match(r"^\s*v?(\d+)\.(\d+)\.(\d+)", text or "")
    return tuple(int(part) for part in match.groups()) if match else None


def same_path(left: str | Path, right: str | Path) -> bool:
    def norm(value: str | Path) -> str:
        return os.path.normcase(os.path.normpath(str(value).strip().strip('"')))

    return norm(left) == norm(right)


def task_info(raw: str | None) -> dict[str, Any] | None:
    """The command and the enabled flag of a task's XML (None: no such task)."""
    if not raw:
        return None
    try:
        root = ET.fromstring(raw.strip())
    except ET.ParseError:
        return None
    command, enabled = "", True
    for element in root.iter():
        name = element.tag.rsplit("}", 1)[-1]
        if name == "Command" and element.text and not command:
            command = element.text.strip().strip('"')
    settings = next((child for child in root if child.tag.rsplit("}", 1)[-1] == "Settings"), None)
    for child in settings if settings is not None else []:
        if child.tag.rsplit("}", 1)[-1] == "Enabled" and child.text:
            enabled = child.text.strip().lower() == "true"
    return {"command": command, "enabled": enabled}


class System:
    """Every effect on the machine. Tests replace all of it except git (a throwaway clone)."""

    windows = os.name == "nt"

    def __init__(self, app: Path, uv: str | None = None):
        self.app = app
        self.root = app.parent
        self.uv, self.env = launcher_env(app, uv=uv)
        self.home = Path.home()
        self.uid = os.getuid() if hasattr(os, "getuid") else 0

    # --- processes and time ------------------------------------------------------------------

    def _run(self, argv: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None, timeout=600):
        try:
            done = subprocess.run(
                argv,
                cwd=str(cwd) if cwd else None,
                env=env,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout,
                check=False,
                creationflags=NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return 127, str(exc)
        return done.returncode, (done.stdout or "") + (done.stderr or "")

    def git(self, *args: str) -> str:
        code, output = self._run(["git", "-C", str(self.app), *args])
        if code != 0:
            raise UpdateError(f"git {' '.join(args)} failed: {output.strip()[:300]}")
        return output.strip()

    def uv_sync(self) -> None:
        code, output = self._run([self.uv, "sync", "--frozen", "--no-dev"], cwd=self.app, env=self.env, timeout=1800)
        if code != 0:
            raise UpdateError(output.strip()[-300:] or f"exit code {code}")

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def monotonic(self) -> float:
        return time.monotonic()

    def http_json(self, url: str, timeout: float = 3.0) -> dict[str, Any] | None:
        # No proxy: a system proxy must never stand between the updater and this machine's TOW.
        try:
            with build_opener(ProxyHandler({})).open(url, timeout=timeout) as response:
                value = json.loads(response.read().decode("utf-8"))
                return value if response.status == 200 and isinstance(value, dict) else None
        except (OSError, ValueError, UnicodeError):
            return None

    def port_open(self, port: int) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1.5):
                return True
        except OSError:
            return False

    def kill_tree(self, pid: int) -> bool:
        if self.windows:
            code, _ = self._run(["taskkill", "/PID", str(pid), "/T", "/F"], timeout=60)
            return code == 0
        kill = getattr(signal, "SIGKILL", 9)
        try:
            os.killpg(pid, kill)  # type: ignore[attr-defined,unused-ignore]  # its whole session
        except OSError:
            with contextlib.suppress(OSError):
                os.kill(pid, kill)
        return True

    def command_line(self, pid: int) -> str:
        """The command line of a running process ("" when it is gone or cannot be read)."""
        if self.windows:
            script = f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine"
            code, output = self._run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], timeout=60)
        else:
            code, output = self._run(["ps", "-o", "command=", "-p", str(int(pid))], timeout=30)
        return output.strip() if code == 0 else ""

    def status_pids(self) -> list[int]:
        """The web server and the job ``tow run`` last reported (data/run/status.json)."""
        try:
            status = json.loads((self.run_dir() / "status.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            return []
        pids = []
        for key in ("server", "job"):
            entry = status.get(key) if isinstance(status, dict) else None
            pid = entry.get("pid") if isinstance(entry, dict) else None
            if isinstance(pid, int) and pid > 0:
                pids.append(pid)
        return pids

    # --- tow run (1.18) ------------------------------------------------------------------------

    def run_dir(self) -> Path:
        return self.root / "data" / "run"

    def supervisor_running(self) -> dict[str, Any] | None:
        """``tow run`` holds data/run/run.lock while it runs (a crash releases it)."""
        lock = self.run_dir() / "run.lock"
        if not lock.exists():
            return None
        try:
            handle = lock.open("a+b")
        except OSError:
            return None
        try:
            if not _try_lock(handle):
                pid = {}
                with contextlib.suppress(OSError, ValueError):
                    pid = json.loads((self.run_dir() / "run.pid").read_text(encoding="utf-8"))
                return pid if isinstance(pid, dict) else {}
            _unlock(handle)
            return None
        finally:
            handle.close()

    def request_stop(self) -> None:
        control = self.run_dir() / "control"
        control.mkdir(parents=True, exist_ok=True)
        payload = {
            "action": "stop",
            "by": "update",
            "operation_id": f"stop-update-{int(time.time())}",
            "at": _now_iso(),
        }
        (control / "stop").write_text(json.dumps(payload) + "\n", encoding="utf-8")

    def unit_path(self) -> Path:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        config = Path(xdg) if xdg and Path(xdg).is_absolute() else self.home / ".config"
        return config / "systemd" / "user" / UNIT_NAME

    def agent_path(self) -> Path:
        return self.home / "Library" / "LaunchAgents" / f"{AGENT_LABEL}.plist"

    def autostart_kind(self) -> str | None:
        """How this install starts with the computer, if it does (task / systemd / launchd)."""
        venv = self.app / ".venv"
        if self.windows:
            info = task_info(self.task_xml("TOW"))
            return "task" if info and same_path(info["command"], venv / "Scripts" / "pythonw.exe") else None
        for kind, path in (("systemd", self.unit_path()), ("launchd", self.agent_path())):
            with contextlib.suppress(OSError, UnicodeError):
                if str(venv / "bin" / "tow") in path.read_text(encoding="utf-8"):
                    return kind
        return None

    def stop_autostart(self, kind: str) -> bool:
        """The OS manager stops TOW and will not start it again by itself (systemd, launchd)."""
        argv = {
            "systemd": ["systemctl", "--user", "stop", UNIT_NAME],
            "launchd": ["launchctl", "bootout", f"gui/{self.uid}/{AGENT_LABEL}"],
        }.get(kind)
        return bool(argv) and self._run(argv, timeout=600)[0] == 0

    def start_autostart(self, kind: str) -> bool:
        if kind == "launchd":
            target = f"gui/{self.uid}/{AGENT_LABEL}"
            if self._run(["launchctl", "print", target], timeout=60)[0] == 0:
                argv = ["launchctl", "kickstart", target]
            else:  # unloaded (by the stop above, or never loaded in this session): load it
                argv = ["launchctl", "bootstrap", f"gui/{self.uid}", str(self.agent_path())]
        else:
            argv = {
                "task": ["schtasks", "/Run", "/TN", "TOW"],
                "systemd": ["systemctl", "--user", "start", UNIT_NAME],
            }[kind]
        return self._run(argv, timeout=60)[0] == 0

    def spawn_supervisor(self) -> None:
        """``tow run`` in the background, outliving this script, with no console window."""
        venv = self.app / ".venv"
        env = self.env
        if self.windows:
            argv = [str(venv / "Scripts" / "pythonw.exe"), "-m", "tow", "run"]
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            for extra in (0x01000000, 0):  # CREATE_BREAKAWAY_FROM_JOB when the job allows it
                try:
                    subprocess.Popen(
                        argv,
                        cwd=str(self.root),
                        env=env,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=flags | NO_WINDOW | extra,
                        close_fds=True,
                    )
                    return
                except OSError:
                    if not extra:
                        raise
            return
        subprocess.Popen(
            [str(venv / "bin" / "tow"), "run"],
            cwd=str(self.root),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )

    def task_xml(self, name: str) -> str | None:
        code, output = self._run(["schtasks", "/Query", "/TN", name, "/XML"], timeout=60)
        return output if code == 0 else None


def _try_lock(handle) -> bool:
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        with contextlib.suppress(OSError):
            handle.write(b"\0")
            handle.flush()
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined,unused-ignore]
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined,unused-ignore]
    except OSError:
        return False
    return True


def _unlock(handle) -> None:
    with contextlib.suppress(OSError):
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined,unused-ignore]
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined,unused-ignore]


class Update:
    def __init__(
        self,
        system: System,
        ref: str,
        *,
        health_timeout: float = 90.0,
        wait_minutes: float = 15.0,
        keep: int = 5,
        say: Callable[[str], None] = print,
    ):
        self.sys = system
        self.app = system.app
        self.root = system.root
        self.ref = ref
        self.health_timeout = health_timeout
        self.wait_minutes = wait_minutes
        self.keep = keep
        self.texts = _messages(self.app, self.root)
        self._say = say
        self.state: dict[str, Any] = {}
        self.port = 8787
        self.autostart: str | None = None  # how this install starts with the computer, if it does
        self.stopped = False  # TOW was stopped by this update and is not running yet
        self.snapshot: Path | None = None
        self.manifest: dict[str, str] = {}

    # --- helpers -----------------------------------------------------------------------------

    def text(self, key: str, **params: Any) -> str:
        template = self.texts.get(key, key)
        return re.sub(r"\{([a-z_]+)\}", lambda m: str(params.get(m.group(1), m.group(0))), template)

    def say(self, key: str, **params: Any) -> None:
        self._say(self.text(key, **params))

    def write_state(self, **fields: Any) -> None:
        self.state.update(fields)
        path = self.root / "update-state.json"
        temporary = path.with_name(".update-state.json.tmp")
        with contextlib.suppress(OSError):
            temporary.write_text(json.dumps(self.state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(temporary, path)

    def wait(self, condition: Callable[[], bool], seconds: float, step: float = 1.0) -> bool:
        deadline = self.sys.monotonic() + seconds
        while self.sys.monotonic() < deadline:
            if condition():
                return True
            self.sys.sleep(step)
        return condition()

    def version(self) -> str:
        with contextlib.suppress(OSError, UnicodeError):
            match = re.search(
                r'(?m)^version\s*=\s*"([^"]+)"', (self.app / "pyproject.toml").read_text(encoding="utf-8")
            )
            if match:
                return match.group(1)
        return ""

    # --- preconditions -----------------------------------------------------------------------

    def check_install(self) -> None:
        if not (self.root / "config.yaml").is_file() or not (self.root / "data").is_dir():
            raise UpdateError(self.text("not_runtime", app=self.app))
        if self.sys.git("status", "--porcelain", "--untracked-files=no"):
            raise UpdateError(self.text("local_edits", app=self.app))
        with contextlib.suppress(OSError, UnicodeError):
            config = (self.root / "config.yaml").read_text(encoding="utf-8")
            match = re.search(r"(?m)^port:\s*['\"]?(\d+)", config)
            if match:
                self.port = int(match.group(1))

    def refuse_old_layout(self) -> None:
        """The five Windows tasks of 1.17 are switched to one process by 1.18-1.20, not here."""
        if not self.sys.windows:
            return
        info = task_info(self.sys.task_xml("TOW-serve"))
        if info and same_path(info["command"], self.app / "scripts" / "tow-serve.cmd"):
            raise UpdateError(self.text("five_tasks"))

    # --- stopping and starting ---------------------------------------------------------------

    def _wait_port_free(self) -> None:
        if not self.wait(lambda: not self.sys.port_open(self.port), 30.0):
            raise UpdateError(self.text("port_busy", port=self.port))

    def stop(self) -> None:
        self.say("stopping")
        self._stop(self.wait_minutes * 60, say=True)

    def _stop(self, seconds: float, *, say: bool) -> None:
        """The stop request first (a running check finishes); forcibly only after ``seconds``."""
        self.autostart = self.sys.autostart_kind()
        running = self.sys.supervisor_running()
        self.stopped = True
        if running is not None:
            self.sys.request_stop()
            if not self.wait(lambda: self.sys.supervisor_running() is None, seconds, 2.0):
                if say:
                    self.say("not_stopped", minutes=self.wait_minutes)
                if self.autostart in ("systemd", "launchd"):
                    self.sys.stop_autostart(self.autostart)  # systemd/launchd would start it again
                owner = running.get("pid")
                if isinstance(owner, int) and self.sys.supervisor_running() is not None:
                    self.sys.kill_tree(owner)
                self._stop_leftovers()
        if self.autostart in ("systemd", "launchd"):
            # Inactive / unloaded: the manager does not start TOW in the middle of the update.
            self.sys.stop_autostart(self.autostart)
        if self.sys.port_open(self.port):
            self._stop_leftovers()  # a supervisor that died earlier left its web server
        self._wait_port_free()

    def _stop_leftovers(self) -> None:
        """The web server and job a stopped (or dead) ``tow run`` recorded, if they still run TOW
        of this install: on Linux and macOS they have their own sessions, so stopping the
        supervisor's never reached them. A pid that now runs anything else is left alone."""
        for pid in self.sys.status_pids():
            command = os.path.normcase(self.sys.command_line(pid).replace('"', ""))
            ours = any(os.path.normcase(str(path)) in command for path in (self.app, self.root))
            if "-m tow" in command and ours:
                self.sys.kill_tree(pid)

    def start(self) -> None:
        kind = self.sys.autostart_kind()
        if not (kind and self.sys.start_autostart(kind)):
            self.sys.spawn_supervisor()

    def healthy(self, version: str) -> bool:
        def answers() -> bool:
            health = self.sys.http_json(f"http://127.0.0.1:{self.port}/healthz")
            if not health or health.get("ok") is not True or (version and health.get("version") != version):
                return False
            state = self.sys.http_json(f"http://127.0.0.1:{self.port}/health.json", timeout=10.0)
            return bool(state and state.get("ok") is True)

        return self.wait(answers, self.health_timeout)

    def start_and_check(self, version: str) -> bool:
        self.start()
        ok = self.healthy(version)
        if ok:
            self.stopped = False
        return ok

    # --- snapshot ----------------------------------------------------------------------------

    def take_snapshot(self, target: str) -> None:
        stamp = datetime.now(UTC).astimezone().strftime("%Y%m%d-%H%M%S")
        safe_ref = re.sub(r"[^\w.-]", "_", self.ref)[:60]
        folder = self.root / "backup" / f"update-{stamp}-before-{safe_ref}"
        data = self.root / "data"
        try:
            self.manifest = data_files(data)
            for relative in self.manifest:
                destination = folder / "data" / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(data / relative, destination)
            shutil.copy2(self.root / "config.yaml", folder / "config.yaml")
            self.manifest["../config.yaml"] = _hash(self.root / "config.yaml")
            (folder / "SNAPSHOT.json").write_text(
                json.dumps(
                    {"ref": self.ref, "target": target, "created_at": _now_iso(), "files": self.manifest}, indent=2
                )
                + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            raise UpdateError(self.text("snapshot_failed", error=exc)) from exc
        self.snapshot = folder
        self.say("snapshot", path=folder)
        self.write_state(snapshot=str(folder))

    def data_changed(self) -> bool:
        current = data_files(self.root / "data")
        with contextlib.suppress(OSError):
            current["../config.yaml"] = _hash(self.root / "config.yaml")
        return current != self.manifest

    def restore_snapshot(self) -> bool:
        """Put data and config back exactly as they were (what the snapshot leaves out stays)."""
        if self.snapshot is None or not self.data_changed():
            return False
        data = self.root / "data"
        for relative in set(data_files(data)) - set(self.manifest):
            (data / relative).unlink(missing_ok=True)
        for relative in self.manifest:
            source = self.snapshot / ("config.yaml" if relative == "../config.yaml" else f"data/{relative}")
            destination = self.root / "config.yaml" if relative == "../config.yaml" else data / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        return True

    def prune(self) -> None:
        """Only update snapshots (and deploy.ps1's data-*-before-*), newest ``keep`` stay."""
        backup = self.root / "backup"
        if not backup.is_dir():
            return
        found = []
        for folder in backup.iterdir():
            match = SNAPSHOT_RE.match(folder.name)
            if folder.is_dir() and match and "pre-runtime" not in folder.name:
                found.append((match.group(1), folder))
        for _stamp, folder in sorted(found, reverse=True)[self.keep :]:
            shutil.rmtree(folder, ignore_errors=True)

    # --- the run -----------------------------------------------------------------------------

    def refuse_too_old(self, target: str) -> None:
        try:
            text = self.sys.git("show", f"{target}:pyproject.toml")
        except UpdateError:
            return  # no version to read: the health check decides
        match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
        version = version_tuple(match.group(1)) if match else None
        if match and version is not None and version < MINIMUM_TARGET:
            raise UpdateError(self.text("too_old", ref=self.ref, version=match.group(1)))

    def run(self) -> int:
        self.check_install()
        self.refuse_old_layout()
        previous = self.sys.git("rev-parse", "HEAD")
        previous_version = self.version()
        self.sys.git("fetch", "--tags", "--prune", "origin")
        target = self.sys.git("rev-parse", "--verify", f"{self.ref}^{{commit}}")
        self.refuse_too_old(target)
        self.say("start", previous=previous[:7], target=target[:7], ref=self.ref, root=self.root)
        self.write_state(
            status="in_progress",
            ref=self.ref,
            target=target,
            previous=previous,
            previous_version=previous_version,
            started_at=_now_iso(),
            snapshot=None,
            error=None,
            rollback=[],
        )
        result, error = "failed", None
        try:
            self.stop()
            self.take_snapshot(target)
            try:
                self.sys.git("checkout", "--quiet", "--detach", target)
                try:
                    self.sys.uv_sync()
                except UpdateError as exc:
                    raise UpdateError(self.text("sync_failed", error=exc)) from exc
                version = self.version()
                self.write_state(target_version=version)
                if not self.start_and_check(version):
                    raise UpdateError(self.text("unhealthy", version=version, seconds=int(self.health_timeout)))
                result = "ok"
            except Exception as exc:  # noqa: BLE001 - a failed step after the switch is rolled back, whatever it was
                error = str(exc)
                self.say("failed", error=error, previous=previous[:7])
                result = self.roll_back(previous, previous_version)
        except Exception as exc:  # noqa: BLE001 - the update boundary: any failure is reported and recorded in update-state.json
            error = str(exc)
            self.say("aborted", error=error)
        finally:
            if self.stopped:  # stopped but never started again: the code did not change
                with contextlib.suppress(Exception):
                    self.start_and_check(previous_version)
            running = bool(self.sys.http_json(f"http://127.0.0.1:{self.port}/healthz"))
            self.write_state(status=result, error=error, finished_at=_now_iso(), service_running=running)
        if result != "ok":
            return 1
        self.prune()
        self.say("ok", version=self.version(), port=self.port)
        return 0

    def roll_back(self, previous: str, previous_version: str) -> str:
        steps: list[dict[str, Any]] = []

        def step(name: str, action: Callable[[], Any]) -> Any:
            try:
                value = action()
            except Exception as exc:  # noqa: BLE001 - each rollback step runs on its own; a failure is recorded and the next step still runs
                steps.append({"step": name, "ok": False, "error": str(exc)[:300]})
                self.say("rollback_step", step=name, result=str(exc)[:200])
                return None
            steps.append({"step": name, "ok": value is not False})
            return value

        def stop_new() -> bool:
            self.stop_quietly()
            return True

        step("stop the new version", stop_new)
        step("check out the previous code", lambda: self.sys.git("checkout", "--quiet", "--detach", previous))
        step("uv sync", self.sys.uv_sync)
        # True: put back; None: the new version had changed nothing (both fine).
        restored = step("put back data and config", lambda: self.restore_snapshot() or None)
        if restored:
            self.say("data_restored")
        self.write_state(data_restored=bool(restored), rollback=steps)
        back = step("start the previous version", lambda: self.start_and_check(previous_version))
        self.write_state(rollback=steps)
        if back:
            self.say("rolled_back")
            return "rolled_back"
        self.say("rollback_failed")
        return "failed"

    def stop_quietly(self) -> None:
        """Stop whatever the failed attempt started."""
        self._stop(120.0, say=False)


def update(ref: str, *, system: System | None = None, app: Path = APP, **options: Any) -> int:
    """Run one update; the exit code (0 ok, 1 failed or rolled back, 2 refused)."""
    system = system or System(app)
    work = Update(system, ref, **options)
    lock_path = system.root / ".update.lock"
    try:
        handle = lock_path.open("a+b")
    except OSError as exc:
        work._say(f"{exc}")
        return 2
    try:
        if not _try_lock(handle):
            work.say("busy", lock=lock_path)
            return 2
        try:
            return work.run()
        except UpdateError as exc:  # refused before anything changed
            work._say(str(exc))
            return 2
        finally:
            _unlock(handle)
    finally:
        handle.close()


def main(argv: list[str] | None = None) -> int:
    if sys.version_info < (3, 11):  # noqa: UP036 - deploy.ps1 may fall back to any Python 3
        print(f"update.py needs Python 3.11 or newer (this is {sys.version.split()[0]})")
        return 2
    parser = argparse.ArgumentParser(description="Update this TOW install to a git tag or commit.")
    parser.add_argument("--ref", required=True, help="release tag (or commit) to install, e.g. v1.18.0")
    parser.add_argument("--health-timeout", type=float, default=90.0, help="seconds for the new version to answer")
    parser.add_argument("--wait-minutes", type=float, default=15.0, help="how long a running check may finish")
    parser.add_argument("--keep", type=int, default=5, help="update snapshots to keep")
    parser.add_argument("--uv", default=None, help="the uv program (default: <TOW>/runtime/bin/uv, else uv on PATH)")
    args = parser.parse_args(argv)
    return update(
        args.ref,
        system=System(APP, uv=args.uv),
        health_timeout=args.health_timeout,
        wait_minutes=args.wait_minutes,
        keep=args.keep,
    )


if __name__ == "__main__":
    sys.exit(main())
