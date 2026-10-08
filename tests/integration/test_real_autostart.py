"""`tow autostart` for real: the OS entry of a throwaway install is made, the OS starts TOW from
it, and `tow autostart off` removes it again.

Skipped unless TOW_REAL_AUTOSTART=1: .github/workflows/real.yml installs the commit on a runner
the way the owner does (install.sh on Linux and macOS, the bundle with install.ps1 on Windows;
TOW refuses autostart for a development checkout) and sets

    TOW_REAL_AUTOSTART_ROOT   the install's folder
    TOW_REAL_AUTOSTART_PORT   its port (never 8787)
    TOW_REAL_AUTOSTART_SKIP   (Linux) why systemd --user cannot work on this runner: skipped so

Never on a development machine: the task, unit and agent names are one per user and would take
over the owner's TOW. The OS is asked directly (schtasks, launchctl, systemctl), not only TOW.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.request import ProxyHandler, build_opener

import pytest

from tow.autostart import AGENT_LABEL, TASK_NAME, UNIT_NAME
from tow.platform import this_os

pytestmark = [pytest.mark.real_system("TOW_REAL_AUTOSTART"), pytest.mark.timeout(900)]

# The job's environment as the run started: the runner's own HOME, XDG_RUNTIME_DIR and session
# bus (tests/conftest.py gives every test a HOME of its own, which the OS service never sees).
_ENVIRONMENT = dict(os.environ)
OS = this_os()
START_SEC = 180  # the first start of an install also writes its data folder
STOP_SEC = 120  # a stop lets a running job finish for 20 s, then stops the web server


@dataclass(frozen=True)
class Install:
    root: Path
    port: int

    @property
    def launcher(self) -> Path:
        return self.root / "app" / "scripts" / ("tow.cmd" if OS == "windows" else "tow")

    @property
    def program(self) -> Path:
        """What the OS entry runs (tow.autostart: the task's pythonw.exe, the unit's and agent's tow)."""
        venv = self.root / "app" / ".venv"
        return venv / "Scripts" / "pythonw.exe" if OS == "windows" else venv / "bin" / "tow"

    @property
    def version(self) -> str:
        text = (self.root / "app" / "pyproject.toml").read_text(encoding="utf-8")
        found = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
        return found.group(1) if found else ""


def the_install() -> Install:
    if reason := os.environ.get("TOW_REAL_AUTOSTART_SKIP"):
        pytest.skip(reason)
    root = os.environ.get("TOW_REAL_AUTOSTART_ROOT", "")
    port = int(os.environ.get("TOW_REAL_AUTOSTART_PORT") or 0)
    if not root or not port or port == 8787:
        pytest.fail("TOW_REAL_AUTOSTART=1 needs TOW_REAL_AUTOSTART_ROOT and TOW_REAL_AUTOSTART_PORT (not 8787)")
    install = Install(Path(root), port)
    assert install.launcher.is_file(), f"no TOW install in {root}"
    return install


def clean_environment() -> dict[str, str]:
    """TOW runs as the owner starts it: no TOW, uv or virtual-environment variables of this run."""
    env = {
        name: value
        for name, value in _ENVIRONMENT.items()
        if not name.upper().startswith(("TOW_", "UV_"))
        and name.upper() not in {"VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH"}
    }
    env["TOW_NO_BROWSER"] = "1"
    return env


def run(argv: list[str], *, timeout: float = 180) -> subprocess.CompletedProcess[str]:
    """A command, bounded; what it said is printed (pytest shows it with a failure)."""
    if argv[0].lower().endswith(".cmd"):
        argv = ["cmd.exe", "/d", "/c", "call", *argv]
    done = subprocess.run(
        argv,
        env=clean_environment(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    print(f"$ {' '.join(argv)}  -> {done.returncode}\n{done.stdout}{done.stderr}", flush=True)
    return done


def tow_json(install: Install, *args: str) -> tuple[int, dict[str, Any]]:
    done = run([str(install.launcher), *args, "--json"])
    try:
        value = json.loads(done.stdout[done.stdout.index("{") :])
    except ValueError:
        raise AssertionError(f"`tow {' '.join(args)} --json` printed no JSON:\n{done.stdout}{done.stderr}") from None
    return done.returncode, value


def health(port: int) -> dict[str, Any] | None:
    try:
        with build_opener(ProxyHandler({})).open(f"http://127.0.0.1:{port}/healthz", timeout=3) as response:
            value = json.loads(response.read().decode("utf-8"))
    except OSError, ValueError:
        return None
    return value if isinstance(value, dict) and value.get("ok") is True else None


def wait_health(port: int, *, answering: bool, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if (health(port) is not None) == answering:
            return True
        time.sleep(1)
    return (health(port) is not None) == answering


# --- the OS, asked directly -----------------------------------------------------------------------


def uid() -> int:
    return os.getuid()  # Linux and macOS only


def os_entry() -> tuple[bool, str]:
    """Whether the OS has TOW's entry, and what it says about it."""
    if OS == "windows":
        done = run(["schtasks", "/Query", "/TN", TASK_NAME, "/XML"])
        return done.returncode == 0, done.stdout + done.stderr
    if OS == "macos":
        done = run(["launchctl", "print", f"gui/{uid()}/{AGENT_LABEL}"])
        return done.returncode == 0, done.stdout + done.stderr
    enabled = run(["systemctl", "--user", "is-enabled", UNIT_NAME])
    unit = run(["systemctl", "--user", "cat", UNIT_NAME])
    return enabled.stdout.strip() == "enabled", enabled.stdout + unit.stdout + unit.stderr


def trigger() -> subprocess.CompletedProcess[str]:
    """Start TOW the way the OS does at sign-in (Task Scheduler, launchd, the user's systemd)."""
    if OS == "windows":
        return run(["schtasks", "/Run", "/TN", TASK_NAME])
    if OS == "macos":
        return run(["launchctl", "kickstart", f"gui/{uid()}/{AGENT_LABEL}"])
    return run(["systemctl", "--user", "start", UNIT_NAME])


def started_by_the_os() -> tuple[bool, str]:
    if OS == "windows":
        script = f"(Get-ScheduledTask -TaskName '{TASK_NAME}' -TaskPath '\\').State"
        done = run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script])
        return done.stdout.strip() == "Running", done.stdout + done.stderr
    if OS == "macos":
        done = run(["launchctl", "print", f"gui/{uid()}/{AGENT_LABEL}"])
        return "state = running" in done.stdout, done.stdout + done.stderr
    done = run(["systemctl", "--user", "is-active", UNIT_NAME])
    return done.stdout.strip() == "active", done.stdout + done.stderr


def diagnostics(install: Install) -> str:
    """What the OS and TOW's logs say, for a failure message."""
    if OS == "windows":
        parts = [run(["schtasks", "/Query", "/TN", TASK_NAME, "/V", "/FO", "LIST"]).stdout]
    elif OS == "macos":
        parts = [run(["launchctl", "print", f"gui/{uid()}/{AGENT_LABEL}"]).stdout]
    else:
        parts = [
            run(["systemctl", "--user", "status", UNIT_NAME, "--no-pager"]).stdout,
            run(["journalctl", "--user", "-u", UNIT_NAME, "-n", "60", "--no-pager"]).stdout,
        ]
    for name in ("run.log", "launchd.log", "serve-stderr.log"):
        log = install.root / "data" / "logs" / name
        if log.is_file():
            lines = log.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]
            parts.append(f"--- data/logs/{name} (end)\n" + "\n".join(lines))
    return "\n".join(parts)


def stop_tow(install: Install) -> None:
    """`tow stop` (the documented way) when TOW answers, and its end confirmed."""
    if health(install.port) is None:
        return
    run([str(install.launcher), "stop"])
    assert wait_health(install.port, answering=False, seconds=STOP_SEC), "TOW answers after `tow stop`\n" + (
        diagnostics(install)
    )


def leave_nothing(install: Install) -> None:
    """After a failure too: no OS entry and no TOW running."""
    if os_entry()[0]:
        run([str(install.launcher), "autostart", "off"])
    stop_tow(install)


# --- the tests -----------------------------------------------------------------------------------


def test_autostart_on_the_os_starts_tow_and_autostart_off_removes_it():
    install = the_install()
    stop_tow(install)  # install.ps1 starts TOW: below, only the OS may start it
    assert health(install.port) is None, f"something answers on port {install.port}"
    code, status = tow_json(install, "autostart", "status")
    assert (code, status.get("on")) == (0, False), status
    assert not os_entry()[0], "an autostart entry exists before the test"
    try:
        code, result = tow_json(install, "autostart", "on")
        assert (code, result.get("ok")) == (0, True), result
        assert result["status"]["on"] is True, result
        present, said = os_entry()
        assert present, said
        if OS == "windows":
            assert "pythonw.exe" in said.lower(), said
            assert "-m tow run" in said, said
        else:
            assert str(install.program) in said, said  # the unit's ExecStart, the agent's program

        # The OS starts it (systemd's `enable --now` and launchd's RunAtLoad already may have).
        started = trigger()
        assert started.returncode == 0, started.stdout + started.stderr + diagnostics(install)
        assert wait_health(install.port, answering=True, seconds=START_SEC), (
            f"/healthz does not answer on {install.port} within {START_SEC} s\n" + diagnostics(install)
        )
        answer = health(install.port) or {}
        assert answer.get("version") == install.version, answer
        running, said = started_by_the_os()
        assert running, said + diagnostics(install)
        code, status = tow_json(install, "autostart", "status")
        assert (code, status.get("on")) == (0, True), status
        code, view = tow_json(install, "status")
        assert (code, view.get("running"), view.get("autostart")) == (0, True, True), view

        code, result = tow_json(install, "autostart", "off")
        assert (code, result.get("ok")) == (0, True), result
        present, said = os_entry()
        assert not present, said
        if OS == "macos":
            # Unloading the agent stops the TOW launchd started (tow.autostart.launchd).
            assert wait_health(install.port, answering=False, seconds=STOP_SEC), (
                "TOW still answers after `tow autostart off` unloaded the agent\n" + diagnostics(install)
            )
        else:
            if OS == "linux":  # `disable`, not `--now`: a running TOW keeps running
                assert health(install.port) is not None, "TOW stopped although autostart off leaves it running"
            stop_tow(install)
        code, status = tow_json(install, "autostart", "status")
        assert (code, status.get("on")) == (0, False), status
        assert health(install.port) is None
    finally:
        leave_nothing(install)


def test_autostart_without_signing_in():
    """Windows: the S4U task with a boot trigger; Linux: the unit with lingering on (the workflow
    turns it on); macOS: refused (it would need a system LaunchDaemon)."""
    install = the_install()
    stop_tow(install)
    if OS == "macos":
        code, result = tow_json(install, "autostart", "on", "--without-login")
        assert (code, result.get("refused")) == (2, True), result
        assert not os_entry()[0]
        return
    try:
        code, result = tow_json(install, "autostart", "on", "--without-login")
        assert (code, result.get("ok")) == (0, True), result
        assert result["status"].get("without_login") is True, result
        present, said = os_entry()
        assert present, said
        if OS == "windows":
            assert "S4U" in said, said
            assert "BootTrigger" in said, said
        else:
            # `enable --now` started it: proved, then stopped below.
            assert wait_health(install.port, answering=True, seconds=START_SEC), diagnostics(install)
        code, result = tow_json(install, "autostart", "off")
        assert (code, result.get("ok")) == (0, True), result
        assert not os_entry()[0]
        stop_tow(install)
    finally:
        leave_nothing(install)
