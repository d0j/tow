"""TOW's PowerShell probes under Constrained Language Mode (an AppLocker or App Control policy).

Measured on Windows PowerShell 5.1 (docs/PORTABLE.md, "PowerShell Constrained Language Mode"):
setting ``[Console]::OutputEncoding`` fails there and the probe runs on in the code page, .NET
methods outside the core types fail, and the system's CDXML modules (NetTCPIP,
ScheduledTasks) do not load in a session switched to the mode. Each probe answers what it
read, or "unknown" - never a garbled path, a task that is "absent" or an update "cut off" that
is still running. The real-PowerShell tests switch a child session to the mode
(``$ExecutionContext.SessionState.LanguageMode``) and check that it says so: the
``__PSLockdownPolicy`` variable of a process is not honoured by 5.1 on Windows 11.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from tow import platform
from tow.platform import _common, windows

ROOT = Path(__file__).resolve().parents[1]
CYRILLIC = r"C:\Users\Иван\TOW\app\.venv\Scripts\python.exe -m tow serve"
# What the probes of 1.33.0 answered in that mode for such a line (no console: the ANSI page).
GARBLED = CYRILLIC.replace("Иван", "\ufffd" * 4)
ESCAPED = json.dumps(CYRILLIC, ensure_ascii=True)  # what _ASCII_JSON writes for it
CLM = "$ExecutionContext.SessionState.LanguageMode = 'ConstrainedLanguage'; "
PROBE = "TOW_TEST_CLM_PROBE"


def _load_updater():
    spec = importlib.util.spec_from_file_location("tow_update_clm_under_test", ROOT / "scripts" / "update.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


updater = _load_updater()


# --- the answers, from a runner fake ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("reply", "cmd"),
    [
        ('{"cmd":' + ESCAPED + ',"pid":4300,"parent":4200,"parent_cmd":null}', CYRILLIC),
        ('{"cmd":' + json.dumps(GARBLED, ensure_ascii=False) + ',"pid":4300,"parent":4200}', None),  # a guess
        ("", None),  # the mode: NetTCPIP not loaded, nothing written, exit 0
    ],
)
def test_the_port_owner_is_read_whole_or_unknown(monkeypatch, reply, cmd):
    monkeypatch.setattr(windows, "_run", lambda args, timeout=20: reply)
    owner = windows.port_owner(8787)
    assert (owner or {}).get("cmd") == cmd
    if cmd is None:
        assert owner is None  # unknown: never a command line some code page made up


@pytest.mark.parametrize(
    ("reply", "command"),
    [
        ('{"ProcessId":4300,"CommandLine":' + ESCAPED + "}", CYRILLIC),
        ('{"ProcessId":4300,"CommandLine":' + json.dumps(GARBLED, ensure_ascii=False) + "}", None),
    ],
)
def test_a_process_command_line_is_read_whole_or_unknown(monkeypatch, reply, command):
    # Before: the U+FFFD line came back as the process's command line.
    monkeypatch.setattr(windows, "_run", lambda args, timeout=20: reply)
    assert windows.process_command(4300) == command


def test_the_windows_probes_need_no_net_call_to_be_read(monkeypatch):
    scripts: list[list[str]] = []
    monkeypatch.setattr(windows, "_run", lambda args, timeout=20: scripts.append(args) or "")
    windows.port_owner(8787)
    windows.process_command(4300)
    for args in scripts:
        assert args[4].startswith("try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }; ")
        assert "ConvertTo-Json -Compress | ForEach-Object" in args[4]


def _escaped(line: str) -> str:
    """A command line as the updater's PowerShell writes it after "cmd:" (``System.command_line``)."""
    return "".join(f"\\u{ord(c):04x}" if ord(c) > 126 or ord(c) < 32 or c in '"\\' else c for c in line)


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("cmd:" + _escaped(CYRILLIC) + "\r\n", CYRILLIC),
        (GARBLED + "\r\n", ""),  # before: the garbled line, never this install's - now said unknown
        ("cmd:" + GARBLED + "\r\n", ""),
        ("Get-CimInstance : Access denied\r\n", ""),  # an error (stderr is read with it): not a line
        ("cmd:" + _escaped(CYRILLIC) + "\r\nGet-CimInstance : Access denied\r\n", ""),
    ],
)
def test_the_updater_reads_a_command_line_whole_or_as_unknown(output, expected):
    class Windows(updater.System):
        windows = True

        def _run(self, argv, *, cwd=None, env=None, timeout=600):
            return 0, output

    assert Windows(ROOT / "app").command_line(4300) == expected


# --- the probes themselves, in a PowerShell session switched to the mode -----------------------------
# Windows PowerShell 5.1 itself (allow_system), on temp files, a name no task has and a child of
# this test that only sleeps.


def _in_mode(argv: list[str], monkeypatch, mode: str) -> list[str]:
    """``argv`` (a probe: ... -Command <script>) with its script run by a session switched to
    ``mode`` first: Invoke-Expression makes the script's block there, so the mode holds for it."""
    assert argv[-2] == "-Command", argv
    monkeypatch.setenv(PROBE, argv[-1])
    return [*argv[:-1], (CLM if mode == "clm" else "") + f"Invoke-Expression $env:{PROBE}"]


@pytest.fixture(scope="module")
def mode_checked():
    """The switch works on this computer: the session says ConstrainedLanguage."""
    if os.name != "nt":
        return
    env = {**os.environ, PROBE: "$ExecutionContext.SessionState.LanguageMode"}
    done = subprocess.run(
        [
            platform.powershell_program(),
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            CLM + f"Invoke-Expression $env:{PROBE}",
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert done.stdout.strip() == "ConstrainedLanguage", done.stdout + done.stderr


@pytest.mark.parametrize("mode", ["full", "clm"])
@pytest.mark.usefixtures("mode_checked")
@pytest.mark.allow_system
@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell")
def test_a_cyrillic_command_line_is_read_whole_in_both_modes(monkeypatch, mode):
    # Before, in the mode: "C:\Users\????" / U+FFFD - a server of this install was never recognised.
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "Иван"])
    try:
        monkeypatch.setattr(
            windows,
            "_run",
            lambda args, timeout=20: _common.run_quiet(
                _in_mode(args, monkeypatch, mode), timeout=60, creationflags=windows.CREATE_NO_WINDOW
            ),
        )
        assert windows.process_command(child.pid).endswith(" Иван")

        class Windows(updater.System):
            windows = True

        system = Windows(ROOT / "app")
        real = system._run
        system._run = lambda argv, **kwargs: real(_in_mode(argv, monkeypatch, mode), timeout=60)
        assert system.command_line(child.pid).endswith(" Иван")
    finally:
        child.kill()
        child.wait(timeout=30)


@pytest.mark.parametrize(("mode", "state"), [("full", "absent"), ("clm", "error")])
@pytest.mark.usefixtures("mode_checked")
@pytest.mark.allow_system
@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell")
def test_an_autostart_task_that_cannot_be_read_is_never_absent(monkeypatch, mode, state):
    # Before, in the mode: the ScheduledTasks module did not load ("ObjectNotFound" too) and an
    # existing autostart task read back as absent. A read-only query of a name no task has.
    from tow.autostart import default_runner
    from tow.autostart import windows as autostart

    task = autostart.WindowsTask.__new__(autostart.WindowsTask)
    task.run = lambda argv: default_runner(_in_mode(list(argv), monkeypatch, mode))
    found = task.query(f"TOW-test-{uuid.uuid4().hex}")
    assert found["state"] == state, found


@pytest.mark.parametrize("mode", ["full", "clm"])
@pytest.mark.parametrize(
    ("record", "locked", "start"),
    [
        ('{"phase": "switching"}', False, False),  # cut off: refused
        ('{"phase": "switching"}', True, True),  # an update runs and starts TOW itself
        ('{"phase": "accepted"}', False, True),
    ],
)
@pytest.mark.usefixtures("mode_checked")
@pytest.mark.allow_system
@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell")
@pytest.mark.parametrize("source", ["scripts/tow-start.cmd", "scripts/tow.cmd", "scripts/root_files.py"])
def test_the_start_files_tell_a_running_update_from_a_cut_one_in_both_modes(
    tmp_path, monkeypatch, mode, record, locked, start, source
):
    # Before, in the mode: [IO.File]::Open failed and a running update read as cut off.
    from tow.platform import locks
    from tow.store import init_lock_file

    text = (ROOT / source).read_text(encoding="utf-8")
    line = next(row for row in text.splitlines() if row.startswith("if defined TOW_CUT") and "-Command" in row)
    script = line.split('-Command "', 1)[1].rsplit('" >nul', 1)[0]
    (tmp_path / ".update-switch.json").write_text(record, encoding="utf-8")
    monkeypatch.setenv("TOW_ROOT", str(tmp_path))
    monkeypatch.setenv("TOW_DIR", str(tmp_path))
    with (tmp_path / ".update.lock").open("a+b") as handle:
        init_lock_file(handle)
        if locked:
            assert locks.lock(handle, wait=False)
        try:
            argv = _in_mode([platform.powershell_program(), "-NoProfile", "-Command", script], monkeypatch, mode)
            done = subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False)
        finally:
            if locked:
                locks.unlock(handle)
    assert done.returncode == (0 if start else 3), done.stdout + done.stderr
