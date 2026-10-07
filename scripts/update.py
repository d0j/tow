"""Update this TOW install to a release tag (or commit) and start it again; roll back on failure.

    <python> <TOW>/app/scripts/update.py --ref v1.18.0
    <python> <TOW>/app/scripts/update.py --ref latest      (an install without git)

Run it with the install's base Python, not the one in app/.venv: ``uv sync`` replaces the
venv's files, which Windows cannot do while they run. ``tow update --ref <tag>`` prints the
exact command; on Windows ``scripts\\deploy.ps1 -Ref <tag>`` runs it. Standard library only, and
Python 3.11 syntax (deploy.ps1 may fall back to any Python 3.11+): no ``except A, B:``.

uv, Python and uv's cache come from the install, exactly as the launchers (scripts/tow,
scripts/tow-env.cmd) give them: ``launcher_env``.

Two kinds of install, told apart by ``app/.git``:

- a git clone: the code is switched with ``git checkout`` (as before 1.22);
- an archive install (the Windows bundle, install.ps1, install.sh: no git): the release's
  source archive is downloaded from GitHub over HTTPS (``--ref`` is a release tag or
  ``latest``), checked against the release's SHA256SUMS, and unpacked into
  ``app.new`` before TOW stops; the switch moves the code into ``app.prev`` and the new code into
  ``app`` (entry by entry, so a terminal open in ``app`` does not block it); a rollback moves it
  back. One ``app.prev`` is kept. Such an install updates to v1.22.0 or newer only (older
  versions cannot update it again).

Steps (each one checked; nothing is reported as done without its read-back):

1. one update at a time (``<TOW>/.update.lock``); undo an archive update that was cut off
   mid-switch; refuse local edits of the code; fetch (or download and unpack the archive);
   ``latest`` that is installed changes nothing, an older one is refused; refuse a target older
   than v1.18.0 (no ``tow run`` to start; v1.22.0 for an archive install) and one that cannot
   read data/state.json (its
   ``STATE_SCHEMA_VERSION`` is lower than the file's format: v1.22 after v1.23 ran);
2. stop TOW: ``tow run`` gets the stop request (it lets a running check finish); only if it does
   not stop in time is it stopped forcibly, with its web server and job (status.json). With
   autostart on Linux or macOS the OS manager stops it too (``systemctl --user stop``,
   ``launchctl bootout``), so it does not start TOW again in the middle of the update;
3. snapshot ``data/`` and ``config.yaml`` into ``<TOW>/backup/update-<time>-before-<tag>``
   (no keys, LAN token, browser profiles, sign-in sessions or logs);
4. check out the target (or move the unpacked code into ``app``), ``uv sync --frozen --no-dev``;
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
import math
import os
import re
import shutil
import signal
import socket
import ssl
import stat
import subprocess
import sys
import tarfile
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

# Not `from datetime import UTC`: Python 3.10 has none, and must reach the version check in main().
UTC = timezone.utc  # noqa: UP017
HERE = Path(__file__).resolve()
# <TOW>/app/scripts/update.py, or the copy an archive switch keeps at <TOW>/runtime/update.py: the
# start files run that copy while a switch is unfinished, when app/scripts may be gone.
APP = HERE.parents[1] / "app" if HERE.parent.name == "runtime" else HERE.parents[1]
# Where an install without git gets its releases (the source archive and SHA256SUMS of a tag).
GITHUB = "https://github.com"
REPO = "d0j/tow"
SOURCE_ASSET = "tow-source.tar.gz"
SUMS_ASSET = "SHA256SUMS"
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024
MAX_ARCHIVE_FILES = 10_000
MAX_MEMBER_BYTES = 256 * 1024 * 1024
MAX_UNPACKED_BYTES = 1024 * 1024 * 1024
WINDOWS_DEVICE_RE = re.compile(r"(?i)(?:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?")
# The oldest release an install without git can go to: older update.py needs git.
MINIMUM_ARCHIVE_TARGET = (1, 22, 0)
# Never in an update snapshot: credentials, keys, browser profiles, sign-in sessions, locks,
# the supervisor's run files, temporary files and the logs. A rollback leaves what the snapshot
# leaves out as it is: the failed version's logs are what tells why it failed.
SKIP_FILES = frozenset({"lan-auth.token", "master.key", "sessions.json"})
SKIP_DIRS = frozenset({"browser-auth", "run", "tmp", "keys", "logs"})
SNAPSHOT_RE = re.compile(r"^(?:update|data)-(\d{8}-\d{6})-before-")
# The oldest version an install managed by `tow run` can go to: older ones have no supervisor.
MINIMUM_TARGET = (1, 18, 0)
# Seconds after a health check in which a rollback still stops the version it started.
STOP_GRACE = 300.0
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
    "too_old": (
        "{ref} is TOW {version}: an install managed by `tow run` goes no further back than v1.18.0"
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
    "downloading": "downloading {url}",
    "download_failed": "the download failed ({url}): {error}; nothing was updated",
    "no_release": "{ref} is not a release of TOW (nothing at {url}); nothing was updated",
    "not_verified": "the release has no checksum for the source archive; nothing was updated",
    "checksum": "the downloaded archive does not match the release's SHA256SUMS; nothing was updated",
    "bad_archive": "the downloaded archive cannot be unpacked ({error}); nothing was updated",
    "wrong_version": "the archive of {ref} holds TOW {version}; nothing was updated",
    "archive_too_old": (
        "{ref} is TOW {version}: an install without git goes no further back than v1.22.0 (older versions"
        " cannot update it again); nothing was updated"
    ),
    "archive_ref": "an install without git updates to a release tag (for example v1.22.0) or latest, not to {ref}",
    "switch_failed": "the code could not be switched ({error}): close programs and windows that use files in {app}",
    "data_newer": (
        "{ref} cannot read this install's data (state.json format {found}; it reads up to {known}): go back no"
        " further than the version that wrote it; nothing was updated"
    ),
    "data_unverified": (
        "this install's data/state.json cannot be read ({error}), so it is not known whether {ref} can read it;"
        " nothing was updated"
    ),
    "up_to_date": "TOW {version} is the latest release: nothing to update",
    "latest_older": (
        "the latest release, {ref}, is older than the installed TOW {version}; nothing was updated (to go back to it,"
        " name it: --ref {ref})"
    ),
    "recovering": "an earlier update was cut off while it replaced the code: putting back TOW {version} first",
    "recovery_failed": "the cut-off update could not be undone: {error}; run the update again to retry",
    "newer_data": (
        "the data changed after the update was cut off ({path}): putting back the copy from before it ({snapshot})"
        " would lose that, so nothing was changed. To go back to TOW {version} and that copy anyway, run: {command}"
    ),
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


def _held(_signum: int, _frame: Any) -> None:
    """An interruption during the switch is let go: the update finishes or rolls back."""


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

    def __init__(self, app: Path, uv: str | None = None, *, github: str = GITHUB, repo: str = REPO):
        self.app = app
        self.root = app.parent
        self.uv, self.env = launcher_env(app, uv=uv)
        self.home = Path.home()
        self.uid = os.getuid() if hasattr(os, "getuid") else 0
        self.github = github.rstrip("/")
        self.repo = repo

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

    @contextlib.contextmanager
    def shielded(self) -> Iterator[None]:
        """Ctrl+C, Ctrl+Break and a closed terminal wait while the code is switched: the update
        finishes or rolls back first. A handler, not SIG_IGN, so the programs started meanwhile
        (uv, tow run) keep the default; a uv cut off by Ctrl+C fails, and that is rolled back."""
        saved = []
        for name in ("SIGINT", "SIGBREAK", "SIGHUP"):
            number = getattr(signal, name, None)
            if number is not None:
                with contextlib.suppress(ValueError, OSError):  # not the main thread: nothing to hold
                    saved.append((number, signal.signal(number, _held)))
        try:
            yield
        finally:
            for number, handler in saved:
                with contextlib.suppress(ValueError, OSError, TypeError):
                    signal.signal(number, handler)

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

    # --- releases on GitHub (an install without git) ------------------------------------------

    def _tls(self) -> ssl.SSLContext:
        """The system's certificates, plus certifi's from the install's environment when it has
        them (a Python built elsewhere may not find this system's store)."""
        context = ssl.create_default_context()
        venv = self.app / ".venv"
        for pem in [
            *venv.glob("Lib/site-packages/certifi/cacert.pem"),
            *venv.glob("lib/python*/site-packages/certifi/cacert.pem"),
        ]:
            with contextlib.suppress(OSError, ssl.SSLError):
                context.load_verify_locations(cafile=str(pem))
        return context

    def _opener(self, *handlers: Any):
        return build_opener(HTTPSHandler(context=self._tls()), *handlers)

    def download(self, url: str, destination: Path) -> bool:
        """``url`` into ``destination``; False when there is nothing there (404)."""
        request = Request(url, headers={"User-Agent": "tow-update"})
        try:
            with self._opener().open(request, timeout=60) as response, destination.open("wb") as handle:
                size = 0
                for chunk in iter(lambda: response.read(1 << 20), b""):
                    size += len(chunk)
                    if size > MAX_DOWNLOAD_BYTES:
                        raise UpdateError(f"more than {MAX_DOWNLOAD_BYTES} bytes")
                    handle.write(chunk)
        except HTTPError as exc:
            if exc.code == 404:
                return False
            raise UpdateError(f"HTTP {exc.code}") from exc
        except (URLError, OSError, ValueError) as exc:
            raise UpdateError(str(getattr(exc, "reason", exc))) from exc
        return True

    def latest_tag(self) -> str:
        """The tag of the latest release (GitHub answers /releases/latest with a redirect to it)."""
        url = f"{self.github}/{self.repo}/releases/latest"

        class Stay(HTTPRedirectHandler):
            def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
                return None  # the Location header is the answer

        location = ""
        try:
            with self._opener(Stay()).open(Request(url, headers={"User-Agent": "tow-update"}), timeout=60) as response:
                location = response.geturl()
        except HTTPError as exc:
            location = exc.headers.get("Location", "") if exc.code in (301, 302, 303, 307, 308) else ""
            if not location:
                raise UpdateError(f"HTTP {exc.code} ({url})") from exc
        except (URLError, OSError, ValueError) as exc:
            raise UpdateError(f"{getattr(exc, 'reason', exc)} ({url})") from exc
        match = re.search(r"/releases/tag/([^/?#]+)$", location)
        if not match:
            raise UpdateError(f"no release found at {url}")
        return match.group(1)

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

    def spawn_handoff(self, argv: list[str], log: Path) -> None:
        """Start the updater behind an exiting intermediate parent, outside the server's tree.

        Breakaway alone does not protect a descendant against Windows taskkill /T.
        The child waits for this process to exit before stopping TOW.
        """
        with log.open("ab") as output:
            subprocess.Popen(
                argv,
                cwd=str(self.root),
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=output,
                close_fds=True,
                creationflags=NO_WINDOW,
                start_new_session=not self.windows,
            )

    def spawn_broker(self, argv: list[str]) -> None:
        """Local WMI starts outside the caller's jobs; the child still verifies independence.

        The environment travels through stdin, never a command line or an on-disk secret file.
        No task, service configuration, elevation or remote connection is created.
        """
        if not self.windows:
            raise OSError("Windows broker unavailable")
        script = """
$ErrorActionPreference='Stop'
[Console]::InputEncoding=[Text.UTF8Encoding]::new($false)
$request=[Console]::In.ReadToEnd() | ConvertFrom-Json
$startup=New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{
  ShowWindow=[uint16]0; CreateFlags=[uint32]0x09000400
  EnvironmentVariables=[string[]]$request.environment
}
Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
  CommandLine=[string]$request.command; CurrentDirectory=[string]$request.cwd
  ProcessStartupInformation=$startup
} | Select-Object ReturnValue,ProcessId | ConvertTo-Json -Compress
"""
        payload = {
            "command": subprocess.list2cmdline(argv),
            "cwd": str(self.root),
            "environment": [f"{key}={value}" for key, value in self.env.items()],
        }
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                input=json.dumps(payload, ensure_ascii=True),
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=15,
                creationflags=NO_WINDOW,
                check=False,
            )
            answer = json.loads(result.stdout) if result.returncode == 0 else None
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            raise OSError("Windows broker launch failed") from exc
        if (
            not isinstance(answer, dict)
            or type(answer.get("ReturnValue")) is not int
            or answer["ReturnValue"] != 0
            or type(answer.get("ProcessId")) is not int
            or not 0 < answer["ProcessId"] <= 0xFFFFFFFF
        ):
            raise OSError("Windows broker did not confirm process creation")

    def wait_process_exit(self, pid: int, timeout: float) -> bool:
        """Wait for the intermediate parent to disappear; uncertainty refuses the update."""
        if pid <= 0:
            return False
        if self.windows:
            import ctypes

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
            kernel.OpenProcess.restype = ctypes.c_void_p
            kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
            kernel.WaitForSingleObject.restype = ctypes.c_ulong
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE, never termination
            if not handle:
                return ctypes.get_last_error() == 87  # ERROR_INVALID_PARAMETER: PID no longer exists
            try:
                return bool(kernel.WaitForSingleObject(handle, int(timeout * 1000)) == 0)
            finally:
                kernel.CloseHandle(handle)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)  # POSIX existence probe only; never used on Windows
            except ProcessLookupError:
                return True
            except OSError:
                return False
            time.sleep(0.05)
        return False

    def updater_independent(self) -> bool:
        """An inherited outer Windows job must not kill the updater when TOW exits."""
        if not self.windows:
            return True
        import ctypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        kernel.IsProcessInJob.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
        inherited = ctypes.c_int()
        return bool(kernel.IsProcessInJob(kernel.GetCurrentProcess(), None, ctypes.byref(inherited))) and not bool(
            inherited.value
        )

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


def parse_sums(text: str) -> dict[str, str]:
    """``sha256sum`` lines (``<hex>  <name>``, ``*<name>`` for binary mode) by file name."""
    sums = {}
    for line in text.splitlines():
        match = re.match(r"^([0-9a-fA-F]{64})\s+\*?(\S.*?)\s*$", line)
        if match:
            sums[match.group(2)] = match.group(1).lower()
    return sums


def unpack(archive: Path, into: Path) -> Path:
    """Unpack a source tarball (one top folder, as GitHub's and ``git archive --prefix`` make
    them) into ``into``; the top folder. Links and paths that leave it are refused."""
    with tarfile.open(archive, "r:gz") as tar:
        top = ""
        total = 0
        for files, member in enumerate(tar, start=1):
            name = member.name.rstrip("/") if member.isdir() else member.name
            parts = name.split("/")
            if (
                not name
                or member.name.startswith(("/", "\\"))
                or "\\" in member.name
                or any(
                    part in {"", ".", ".."}
                    or ":" in part
                    or part.endswith((" ", "."))
                    or bool(WINDOWS_DEVICE_RE.fullmatch(part))
                    for part in parts
                )
                or not (member.isfile() or member.isdir())
            ):
                raise UpdateError(f"refused entry {member.name}")
            if not top:
                top = parts[0]
            elif parts[0] != top:
                raise UpdateError("more than one top folder")
            total += member.size
            if files > MAX_ARCHIVE_FILES or member.size > MAX_MEMBER_BYTES or total > MAX_UNPACKED_BYTES:
                raise UpdateError("source archive exceeds its unpacked size or file-count limit")
            target = into.joinpath(*parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                source = tar.extractfile(member)
                if source is None:
                    raise UpdateError(f"refused entry {member.name}")
                with source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                target.chmod(member.mode & 0o777)
        if not top:
            raise UpdateError("empty source archive")
    return into / top


def remove_tree(path: Path) -> None:
    """Remove a folder; read-only files too (Windows)."""

    def writable(function: Callable[..., Any], name: str, _error: Any) -> None:
        with contextlib.suppress(OSError):
            os.chmod(name, stat.S_IWRITE)
            function(name)

    if not path.exists():
        return
    if not path.is_dir() or path.is_symlink():
        path.unlink()
        return
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=writable)
    else:  # pragma: no cover - Python 3.11 has no onexc
        shutil.rmtree(path, onerror=writable)


class GitCode:
    """The code is a git clone: ``git checkout`` switches it (installs before 1.22 all are)."""

    kind = "git"

    def __init__(self, work: Update):
        self.work = work
        self.sys = work.sys

    def check(self) -> None:
        if self.sys.git("status", "--porcelain", "--untracked-files=no"):
            raise UpdateError(self.work.text("local_edits", app=self.work.app))

    def current(self) -> str:
        return self.sys.git("rev-parse", "HEAD")

    def prepare(self, ref: str) -> str:
        self.sys.git("fetch", "--tags", "--prune", "origin")
        target = self.sys.git("rev-parse", "--verify", f"{ref}^{{commit}}")
        self.work.refuse_too_old(target)
        return target

    def short(self, value: str) -> str:
        return value[:7]

    def target_text(self, target: str, relative: str) -> str | None:
        code, output = self.sys._run(["git", "-C", str(self.sys.app), "show", f"{target}:{relative}"])
        return output if code == 0 else None

    def switch(self, target: str) -> None:
        self.sys.git("checkout", "--quiet", "--detach", target)

    def switch_back(self, previous: str) -> None:
        self.sys.git("checkout", "--quiet", "--detach", previous)

    def discard(self) -> None:
        """Nothing was unpacked: a fetch leaves the checkout as it is."""

    def allow_data_changes(self, seconds: float) -> None:
        """A checkout keeps no switch record."""


class ArchiveCode:
    """The code came from a release archive (no git): a release's source archive replaces it."""

    kind = "archive"

    def __init__(self, work: Update, *, source: Path | None = None, sums: Path | None = None):
        self.work = work
        self.sys = work.sys
        self.app = work.app
        self.new = work.root / "app.new"
        self.prev = work.root / "app.prev"
        self.failed = work.root / "app.failed"
        self.downloads = work.root / ".update-download"
        self.journal = work.root / ".update-switch.json"
        self.source = source
        self.sums = sums
        self.moved_out: list[str] = []
        self.moved_in: list[str] = []

    def check(self) -> None:
        if self.journal.exists():
            return  # a cut-off switch must be recovered before any staged files are removed
        for leftover in (self.new, self.failed, self.downloads):
            remove_tree(leftover)  # an earlier update that was cut off

    def _journal(self) -> dict[str, Any]:
        try:
            record = json.loads(self.journal.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError) as exc:
            raise UpdateError(f"cannot read the interrupted update record: {exc}") from exc
        if not isinstance(record, dict) or record.get("format") != "tow-update-switch/v1":
            raise UpdateError("invalid interrupted update record")
        for key in ("old", "new"):
            names = record.get(key)
            if not isinstance(names, list) or any(
                not isinstance(name, str) or name in ("", ".", "..") or "/" in name or "\\" in name for name in names
            ):
                raise UpdateError("invalid interrupted update record")
        if record.get("phase") not in ("switching", "accepted"):
            raise UpdateError("invalid interrupted update record")
        return record

    def _write_journal(self, record: dict[str, Any]) -> None:
        temporary = self.journal.with_name(self.journal.name + ".tmp")
        temporary.write_text(json.dumps(record), encoding="utf-8")
        os.replace(temporary, self.journal)

    def recover_pending(self, *, finalize: bool = True) -> str | None:
        """Idempotently put back the old code after a hard interruption."""
        if not self.journal.exists():
            return None
        record = self._journal()
        if record["phase"] == "accepted":
            self.journal.unlink()
            remove_tree(self.new)
            remove_tree(self.failed)
            return "accepted"
        self.failed.mkdir(exist_ok=True)
        for name in record["new"]:
            if name not in record["old"] and (self.app / name).exists():
                remove_tree(self.failed / name)
                os.replace(self.app / name, self.failed / name)
        for name in record["old"]:
            if not (self.prev / name).exists():
                continue  # already in app, or not yet moved out when the process died
            if (self.app / name).exists():
                remove_tree(self.failed / name)
                os.replace(self.app / name, self.failed / name)
            os.replace(self.prev / name, self.app / name)
        remove_tree(self.failed)
        remove_tree(self.prev)
        remove_tree(self.new)
        if finalize:
            self.journal.unlink()
        return "restored"

    def allow_data_changes(self, seconds: float) -> None:
        """TOW, started by this update, may change data in the next ``seconds``: such changes are
        the update's own, and a recovery may put the snapshot back over them; later ones it does not."""
        if self.journal.exists():
            record = self._journal()
            until = record.get("data_until")
            record["data_until"] = max(until if isinstance(until, (int, float)) else 0, time.time() + seconds)
            self._write_journal(record)

    def accept_switch(self) -> None:
        if self.journal.exists():
            record = self._journal()
            record["phase"] = "accepted"
            self._write_journal(record)
            with contextlib.suppress(OSError):
                self.journal.unlink()  # accepted is recoverable even if this delete is interrupted

    def current(self) -> str:
        return f"v{self.work.version()}"

    def short(self, value: str) -> str:
        return value

    def target_text(self, target: str, relative: str) -> str | None:
        """A file of the unpacked target (``app.new``, after ``prepare``)."""
        del target
        try:
            return (self.new / relative).read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None

    # -- the archive ---------------------------------------------------------------------------

    def _fetch(self, url: str, destination: Path, *, required: bool) -> bool:
        self.work.say("downloading", url=url)
        try:
            found = self.sys.download(url, destination)
        except UpdateError as exc:
            raise UpdateError(self.work.text("download_failed", url=url, error=exc)) from exc
        if not found and required:
            raise UpdateError(self.work.text("no_release", ref=self.work.ref, url=url))
        return found

    def _expected(self, tag: str) -> str:
        """The required source archive SHA-256 from the release's SHA256SUMS."""
        path = self.sums
        if path is None:
            path = self.downloads / SUMS_ASSET
            if not self._fetch(
                f"{self.sys.github}/{self.sys.repo}/releases/download/{tag}/{SUMS_ASSET}", path, required=False
            ):
                raise UpdateError(self.work.text("not_verified"))
        try:
            expected = parse_sums(path.read_text(encoding="utf-8")).get(SOURCE_ASSET)
        except (OSError, UnicodeError):
            expected = None
        if expected is None:
            raise UpdateError(self.work.text("not_verified"))
        return expected

    def _download(self, tag: str) -> Path:
        """The tag's source archive, checked against SHA256SUMS: GitHub's
        archive of the tag, else the copy uploaded with the release."""
        if self.source is not None:  # a local archive (--source), checked when --sums is given
            expected = self._expected(tag) if self.sums is not None else None
            if expected is not None and _hash(self.source) != expected:
                raise UpdateError(self.work.text("checksum"))
            return self.source
        expected = self._expected(tag)
        base = f"{self.sys.github}/{self.sys.repo}"
        archive = self.downloads / SOURCE_ASSET
        urls = [f"{base}/archive/refs/tags/{tag}.tar.gz", f"{base}/releases/download/{tag}/{SOURCE_ASSET}"]
        found = False
        for url in urls:
            if not self._fetch(url, archive, required=False):
                continue
            found = True
            if _hash(archive) == expected:
                return archive
        raise UpdateError(self.work.text("checksum" if found else "no_release", ref=tag, url=urls[0]))

    def prepare(self, ref: str) -> str | None:
        """The release tag, unpacked into app.new; None: ``latest`` is the installed version."""
        if ref == "latest":
            try:
                ref = self.sys.latest_tag()
            except UpdateError as exc:
                raise UpdateError(
                    self.work.text("download_failed", url=f"{self.sys.github}/{self.sys.repo}", error=exc)
                ) from exc
            # Only a release named by its tag installs again, or goes back.
            latest, installed = version_tuple(ref), version_tuple(self.work.version())
            if latest is not None and installed is not None and latest < installed:
                raise UpdateError(self.work.text("latest_older", ref=ref, version=self.work.version()))
            if latest is not None and latest == installed:
                return None
        if version_tuple(ref) is None:
            raise UpdateError(self.work.text("archive_ref", ref=ref))
        self.downloads.mkdir(parents=True, exist_ok=True)
        archive = self._download(ref)
        try:
            top = unpack(archive, self.downloads / "unpacked")
        except (OSError, tarfile.TarError, UpdateError) as exc:
            raise UpdateError(self.work.text("bad_archive", error=exc)) from exc
        version = _project_version(top / "pyproject.toml")
        if version_tuple(version) != version_tuple(ref):
            raise UpdateError(self.work.text("wrong_version", ref=ref, version=version or "?"))
        parsed = version_tuple(version)
        if parsed is not None and parsed < MINIMUM_ARCHIVE_TARGET:
            raise UpdateError(self.work.text("archive_too_old", ref=ref, version=version))
        os.replace(top, self.new)
        remove_tree(self.downloads)
        return ref

    # -- the switch ----------------------------------------------------------------------------

    def switch(self, target: str) -> None:
        """app -> app.prev, app.new -> app, entry by entry (a window open in app keeps it)."""
        del target
        stable = self.work.root / "runtime" / "update.py"
        stable.parent.mkdir(parents=True, exist_ok=True)
        if stable.resolve() != HERE:  # not when this is that copy, run by the start files
            shutil.copy2(HERE, stable)  # callable even if app/scripts disappears mid-switch
        remove_tree(self.prev)  # one previous version is kept
        self.prev.mkdir()
        self.moved_out, self.moved_in = [], []
        try:
            self._write_journal(
                {
                    "format": "tow-update-switch/v1",
                    "phase": "switching",
                    "old": sorted(os.listdir(self.app)),
                    "new": sorted(os.listdir(self.new)),
                    "previous_version": self.work.version(),
                    "snapshot": self.work.snapshot.name if self.work.snapshot else None,
                    "data_until": time.time(),  # TOW is stopped: nothing of the update's changes data yet
                }
            )
            for entry in sorted(os.listdir(self.app)):
                os.replace(self.app / entry, self.prev / entry)
                self.moved_out.append(entry)
            for entry in sorted(os.listdir(self.new)):
                os.replace(self.new / entry, self.app / entry)
                self.moved_in.append(entry)
        except OSError as exc:
            raise UpdateError(self.work.text("switch_failed", error=exc, app=self.app)) from exc
        remove_tree(self.new)

    def switch_back(self, previous: str) -> None:
        """The on-disk record also works when the process was cut off mid-switch."""
        del previous
        self.recover_pending(finalize=False)

    def discard(self) -> None:
        for leftover in (self.new, self.downloads):
            remove_tree(leftover)


def _finite(text: str) -> float:
    number = float(text)
    if not math.isfinite(number):
        raise ValueError("non-finite JSON number")
    return number


def state_format(raw: bytes) -> int:
    """The format (``schema_version``) of a state.json, read as tow.store reads it: an object,
    finite numbers, at most 128 levels deep; ValueError/TypeError/RecursionError otherwise."""
    value = json.loads(raw.decode("utf-8"), parse_float=_finite, parse_constant=_finite)
    if not isinstance(value, dict):
        raise TypeError("state.json is not an object")
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        if depth > 128:
            raise ValueError("state.json nesting exceeds limit")
        children = item.values() if isinstance(item, dict) else item if isinstance(item, list) else ()
        stack.extend((child, depth + 1) for child in children)
    found = value.get("schema_version", 0)
    if type(found) is not int or found < 0:
        raise ValueError("invalid state format")
    return found


def _project_version(pyproject: Path) -> str:
    with contextlib.suppress(OSError, UnicodeError):
        match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', pyproject.read_text(encoding="utf-8"))
        if match:
            return match.group(1)
    return ""


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
        source: Path | None = None,
        sums: Path | None = None,
        discard_newer_data: bool = False,
        progress: Callable[[str], None] | None = None,
    ):
        self.sys = system
        self.app = system.app
        self.root = system.root
        self.ref = ref
        # Undoing a cut-off update may put its snapshot back over data changed after it.
        self.discard_newer_data = discard_newer_data
        # Told each phase (stopping, backup, installing, checking, rolling_back) and then the
        # outcome update-state.json records; the web update's worker shows them. A failure to
        # record a phase before the rollback ends the update (rolled back), never a rollback.
        self.progress: Callable[[str], None] = progress or (lambda _name: None)
        self.health_timeout = health_timeout
        self.wait_minutes = wait_minutes
        self.keep = keep
        self.texts = _messages(self.app, self.root)
        self._say = say
        # A git clone, or an install from a release archive (the Windows bundle, the installers).
        self.code: GitCode | ArchiveCode = (
            GitCode(self) if (self.app / ".git").exists() else ArchiveCode(self, source=source, sums=sums)
        )
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

    def report(self, name: str) -> None:
        """``progress`` for a rollback or an outcome: a caller that cannot record it never stops either."""
        with contextlib.suppress(Exception):
            self.progress(name)

    def say(self, key: str, **params: Any) -> None:
        with contextlib.suppress(OSError, ValueError):  # a closed terminal must not stop a rollback
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
        return _project_version(self.app / "pyproject.toml")

    # --- preconditions -----------------------------------------------------------------------

    def check_install(self) -> None:
        if not (self.root / "config.yaml").is_file() or not (self.root / "data").is_dir():
            raise UpdateError(self.text("not_runtime", app=self.app))
        with contextlib.suppress(OSError, UnicodeError):
            config = (self.root / "config.yaml").read_text(encoding="utf-8")
            match = re.search(r"(?m)^port:\s*['\"]?(\d+)", config)
            if match:
                self.port = int(match.group(1))
        self.code.check()

    def recover_archive(self) -> bool:
        """Undo an archive update that was cut off mid-switch: its code, and its data if it changed
        them. An unusable record or snapshot is refused (UpdateError) before anything changes;
        False when the undoing itself failed (``recovery_failed``; TOW is started again)."""
        if not isinstance(self.code, ArchiveCode) or not self.code.journal.exists():
            return True
        record = self.code._journal()
        if record["phase"] == "accepted":
            self.code.recover_pending()
            self.code.check()
            return True
        previous_version = record.get("previous_version")
        if not isinstance(previous_version, str) or not previous_version:
            raise UpdateError("invalid interrupted update record")
        snapshot_name = record.get("snapshot")
        if not isinstance(snapshot_name, str) or not re.fullmatch(r"update-[A-Za-z0-9_.-]+", snapshot_name):
            raise UpdateError("invalid interrupted update record")
        snapshot = self.root / "backup" / snapshot_name
        self.manifest = self._load_snapshot(snapshot)
        self.snapshot = snapshot
        self.refuse_newer_data(record.get("data_until"), previous_version)
        self.say("recovering", version=previous_version)
        self.write_state(
            status="in_progress", ref=self.ref, previous_version=previous_version, started_at=_now_iso(), error=None
        )
        with self.sys.shielded():
            error = self._recover(previous_version)
        running = bool(self.sys.http_json(f"http://127.0.0.1:{self.port}/healthz"))
        status = "recovery_failed" if error else "recovered"
        self.write_state(status=status, error=error, finished_at=_now_iso(), service_running=running)
        if error:
            return False
        self.code.check()
        return True

    def refuse_newer_data(self, until: Any, previous_version: str) -> None:
        """Data changed after the cut-off update let go of TOW (``data_until`` in its record) is not
        the update's own: the snapshot is not put back over it unless the owner says so."""
        if self.discard_newer_data or type(until) not in (int, float):
            return  # the record of an older update.py does not say
        data = self.root / "data"
        try:
            changed = [data / name for name, digest in data_files(data).items() if self.manifest.get(name) != digest]
            if _hash(self.root / "config.yaml") != self.manifest.get("../config.yaml"):
                changed.append(self.root / "config.yaml")
            newer = sorted((path.stat().st_mtime, str(path)) for path in changed if path.stat().st_mtime > until)
        except OSError as exc:
            raise UpdateError(f"the data cannot be compared with the update snapshot: {exc}") from exc
        if newer:
            command = (
                f'"{sys.executable}" "{self.root / "runtime" / "update.py"}" --ref {self.ref} --discard-newer-data'
            )
            raise UpdateError(
                self.text(
                    "newer_data", path=newer[-1][1], snapshot=self.snapshot, version=previous_version, command=command
                )
            )

    def _recover(self, previous_version: str) -> str | None:
        """Stop TOW, put the previous code and the snapshot back, start it; the error, if any."""
        assert isinstance(self.code, ArchiveCode)
        self.report("rolling_back")
        code = "as found"  # -> "mixed" while the entries move -> "previous" once they are back
        was_running = self.sys.supervisor_running() is not None
        try:
            self.stop()
            code = "mixed"
            self.code.recover_pending(finalize=False)
            code = "previous"
            self.restore_snapshot()
            code = "started"
            if not self.start_and_check(previous_version):
                raise UpdateError(self.text("unhealthy", version=previous_version, seconds=int(self.health_timeout)))
            self.code.journal.unlink()
        except Exception as exc:  # noqa: BLE001 - the recovery boundary: recorded, and TOW started again where it can be
            error = str(exc) or type(exc).__name__
            self.say("recovery_failed", error=error)
            if self.stopped and (code == "previous" or (code == "as found" and was_running)):  # never half-moved code
                with contextlib.suppress(Exception):
                    self.start_and_check(previous_version if code == "previous" else "")
            return error
        self.say("rolled_back")
        return None

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
        # Its health check, and a rollback's stop of it after a failed one, are the update's own time.
        self.code.allow_data_changes(self.health_timeout + STOP_GRACE)
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
        self._load_snapshot(folder)  # read back every byte before switching the code
        self.snapshot = folder
        self.say("snapshot", path=folder)
        self.write_state(snapshot=str(folder))

    def data_changed(self) -> bool:
        current = data_files(self.root / "data")
        with contextlib.suppress(OSError):
            current["../config.yaml"] = _hash(self.root / "config.yaml")
        return current != self.manifest

    def _load_snapshot(self, folder: Path) -> dict[str, str]:
        """Read the updater's copy and verify it before it can replace live data."""
        backup = (self.root / "backup").resolve()
        if not folder.resolve().is_relative_to(backup):
            raise UpdateError("the update snapshot is outside the backup folder")
        try:
            record = json.loads((folder / "SNAPSHOT.json").read_text(encoding="utf-8"))
            files = record["files"]
        except (OSError, ValueError, UnicodeError, TypeError, KeyError) as exc:
            raise UpdateError(f"the update snapshot cannot be read: {exc}") from exc
        if not isinstance(files, dict) or "../config.yaml" not in files:
            raise UpdateError("the update snapshot has an invalid file list")
        for relative, digest in files.items():
            if (
                not isinstance(relative, str)
                or not isinstance(digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", digest)
            ):
                raise UpdateError("the update snapshot has an invalid file list")
            if relative != "../config.yaml" and (
                not relative
                or relative.startswith("/")
                or ":" in relative
                or any(part in ("", ".", "..") for part in relative.split("/"))
                or "\\" in relative
            ):
                raise UpdateError("the update snapshot has an unsafe file name")
            source = folder / ("config.yaml" if relative == "../config.yaml" else f"data/{relative}")
            try:
                intact = source.is_file() and not source.is_symlink() and _hash(source) == digest
            except OSError:
                intact = False
            if not intact:
                raise UpdateError(f"the update snapshot is damaged: {relative}")
        return files

    def restore_snapshot(self) -> bool:
        """Put data and config back exactly as they were (what the snapshot leaves out stays)."""
        if self.snapshot is None or not self.data_changed():
            return False
        if self._load_snapshot(self.snapshot) != self.manifest:
            raise UpdateError("the update snapshot changed after it was taken")
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

    def refuse_unreadable_data(self, target: str) -> None:
        """The target must read this install's state.json: an older TOW cannot read a newer state
        format (v1.22 reads format 1, v1.23 writes 2), so going back that far is refused here,
        before TOW stops. No declared format: v1.18, which reads format 1. A newer target whose
        format cannot be read here (written another way) is not refused: it checks the data itself."""
        source = self.code.target_text(target, "src/tow/store.py") or ""
        # `STATE_SCHEMA_VERSION = 2`, also with a type (`: int`, `: Final[int]`) or a comment.
        match = re.search(r"^STATE_SCHEMA_VERSION\s*(?::[^=\n]*)?=\s*([0-9]+)\s*(?:#.*)?$", source, re.MULTILINE)
        if match:
            known = int(match[1])
        else:
            found_version = re.search(
                r'(?m)^version\s*=\s*"([^"]+)"', self.code.target_text(target, "pyproject.toml") or ""
            )
            version = version_tuple(found_version[1]) if found_version else None
            if version is None or version >= (1, 19, 0):
                return
            known = 1
        try:
            found = state_format((self.root / "data" / "state.json").read_bytes())
        except FileNotFoundError:
            return
        except (OSError, ValueError, TypeError, RecursionError) as exc:
            raise UpdateError(self.text("data_unverified", ref=self.ref, error=exc)) from exc
        if found > known:
            raise UpdateError(self.text("data_newer", ref=self.ref, found=found, known=known))

    def run(self) -> int:
        self.check_install()
        if not self.recover_archive():
            return 1
        code = self.code
        previous = code.current()
        previous_version = self.version()
        try:
            target = code.prepare(self.ref)  # fetch, or download and unpack: TOW still runs
            if target is not None:
                self.refuse_unreadable_data(target)
        except BaseException:
            code.discard()
            raise
        if target is None:
            self.say("up_to_date", version=previous_version)
            return 0
        self.say("start", previous=code.short(previous), target=code.short(target), ref=self.ref, root=self.root)
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
            try:
                self.progress("stopping")
                self.stop()
                self.progress("backup")
                self.take_snapshot(target)
            except BaseException:
                code.discard()  # the code was not switched: an unpacked archive goes
                raise
            with self.sys.shielded():
                try:
                    self.progress("installing")
                    code.switch(target)
                    try:
                        self.sys.uv_sync()
                    except UpdateError as exc:
                        raise UpdateError(self.text("sync_failed", error=exc)) from exc
                    version = self.version()
                    self.write_state(target_version=version)
                    self.progress("checking")
                    if not self.start_and_check(version):
                        raise UpdateError(self.text("unhealthy", version=version, seconds=int(self.health_timeout)))
                    if isinstance(code, ArchiveCode):
                        code.accept_switch()
                    result = "ok"
                except BaseException as exc:  # whatever cut the switch off, Ctrl+C too, is rolled back
                    error = str(exc) or type(exc).__name__
                    self.say("failed", error=error, previous=code.short(previous))
                    result = self.roll_back(previous, previous_version)
                    if not isinstance(exc, Exception):
                        raise  # after the rollback: an interruption still ends the run
        except Exception as exc:  # noqa: BLE001 - the update boundary: any failure is reported and recorded in update-state.json
            error = str(exc)
            self.say("aborted", error=error)
        finally:
            if self.stopped:  # stopped but never started again: the code did not change
                with contextlib.suppress(Exception):
                    self.start_and_check(previous_version)
            running = bool(self.sys.http_json(f"http://127.0.0.1:{self.port}/healthz"))
            self.write_state(status=result, error=error, finished_at=_now_iso(), service_running=running)
            self.report(result)
        if result != "ok":
            return 1
        self.prune()
        self.say("ok", version=self.version(), port=self.port)
        return 0

    def roll_back(self, previous: str, previous_version: str) -> str:
        self.report("rolling_back")
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
        step("check out the previous code", lambda: self.code.switch_back(previous))
        step("uv sync", self.sys.uv_sync)
        # True: put back; None: the new version had changed nothing (both fine).
        restored = step("put back data and config", lambda: self.restore_snapshot() or None)
        if restored:
            self.say("data_restored")
        self.write_state(data_restored=bool(restored), rollback=steps)
        back = step("start the previous version", lambda: self.start_and_check(previous_version))
        if back and all(item["ok"] for item in steps) and isinstance(self.code, ArchiveCode):
            step("finish rollback", lambda: self.code.journal.unlink(missing_ok=True))
        self.write_state(rollback=steps)
        if back and all(item["ok"] for item in steps):
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


def _utf8_output() -> None:
    # Pipes on Windows use the ANSI codepage; -I also ignores PYTHONIOENCODING.
    # Configure both diagnostic streams before parsing or touching the install.
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            with contextlib.suppress(AttributeError, OSError, ValueError):
                stream.reconfigure(encoding="utf-8", errors="backslashreplace")


def main(argv: list[str] | None = None) -> int:
    _utf8_output()
    if sys.version_info < (3, 11):  # noqa: UP036 - deploy.ps1 may fall back to any Python 3
        print(f"update.py needs Python 3.11 or newer (this is {sys.version.split()[0]})")
        return 2
    parser = argparse.ArgumentParser(
        description="Update this TOW install to a release tag (or a commit of a git clone)."
    )
    parser.add_argument(
        "--ref", required=True, help="release tag (or commit) to install, e.g. v1.22.0; `latest` without git"
    )
    parser.add_argument("--health-timeout", type=float, default=90.0, help="seconds for the new version to answer")
    parser.add_argument("--wait-minutes", type=float, default=15.0, help="how long a running check may finish")
    parser.add_argument("--keep", type=int, default=5, help="update snapshots to keep")
    parser.add_argument("--uv", default=None, help="the uv program (default: <TOW>/runtime/bin/uv, else uv on PATH)")
    parser.add_argument(
        "--source", type=Path, default=None, help="without git: this source archive (.tar.gz) instead of a download"
    )
    parser.add_argument("--sums", type=Path, default=None, help="without git: the SHA256SUMS to check --source with")
    parser.add_argument(
        "--discard-newer-data",
        action="store_true",
        help="undoing an update that was cut off: put its snapshot back even over data changed after it",
    )
    args = parser.parse_args(argv)
    return update(
        args.ref,
        system=System(APP, uv=args.uv),
        health_timeout=args.health_timeout,
        wait_minutes=args.wait_minutes,
        keep=args.keep,
        source=args.source.resolve() if args.source else None,
        sums=args.sums.resolve() if args.sums else None,
        discard_newer_data=args.discard_newer_data,
    )


if __name__ == "__main__":
    sys.exit(main())
