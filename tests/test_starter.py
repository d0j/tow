"""`tow start`: TOW in the background and its page in the browser (the start files of every
install run it), and the start scripts that prepare the environment first."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

from tow import cli
from tow.supervisor import layout, starter

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def run_env(monkeypatch):
    # tow start exports the layout to its own environment: restored after each test.
    monkeypatch.setenv("TOW_ROOT", os.environ.get("TOW_ROOT", str(Path.cwd())))
    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")
    monkeypatch.delenv("TOW_NO_BROWSER", raising=False)


class Machine:
    """tow run, its web server and the browser, on a fake clock."""

    def __init__(self, *, running: bool = False, answers_after: float | None = 2.0, dies_after: float | None = None):
        self.t = 0.0
        self.is_running = running
        self.answers_after = answers_after
        self.dies_after = dies_after
        self.spawned: list[int] = []
        self.opened: list[str] = []
        self.browser_works = True

    def deps(self) -> starter.Deps:
        return starter.Deps(
            running=lambda: {"pid": 7} if self.is_running else None,
            spawn=self.spawn,
            alive=lambda pid: self.dies_after is None or self.t < self.dies_after,
            healthy=lambda port: self.answers_after is not None and self.t >= self.answers_after,
            open_url=self.open_url,
            sleep=self.sleep,
            monotonic=lambda: self.t,
        )

    def spawn(self) -> int:
        self.spawned.append(4242)
        return 4242

    def open_url(self, url: str) -> bool:
        self.opened.append(url)
        return self.browser_works

    def sleep(self, seconds: float) -> None:
        self.t += seconds


def test_a_stopped_tow_is_started_and_its_page_opened_once_it_answers():
    machine = Machine(answers_after=3.0)
    result = starter.start(18990, deps=machine.deps())
    assert result == {"ok": True, "state": "started", "url": "http://127.0.0.1:18990/", "pid": 4242, "browser": True}
    assert machine.spawned == [4242]
    assert machine.opened == ["http://127.0.0.1:18990/"]
    assert machine.t >= 3.0  # not before /healthz answered


def test_a_running_tow_only_gets_its_page_opened():
    machine = Machine(running=True, answers_after=0.0)
    result = starter.start(18990, deps=machine.deps())
    assert (result["state"], result["pid"]) == ("running", None)
    assert machine.spawned == []
    assert machine.opened == ["http://127.0.0.1:18990/"]


def test_no_browser_is_opened_when_asked_not_to():
    machine = Machine()
    result = starter.start(18990, browser=False, deps=machine.deps())
    assert result["ok"] is True
    assert result["browser"] is False
    assert machine.opened == []


def test_a_tow_run_that_ends_before_it_answers_is_reported_at_once():
    machine = Machine(answers_after=None, dies_after=1.0)
    result = starter.start(18990, wait=120, deps=machine.deps())
    assert result["state"] == "exited"
    assert result["ok"] is False
    assert machine.t < 5  # not after the whole wait
    assert machine.opened == []


def test_another_tow_run_that_took_over_is_waited_for():
    # The new tow run found one already running (started in between) and ended: not a failure.
    machine = Machine(answers_after=4.0, dies_after=1.0)
    deps = machine.deps()
    deps.running = lambda: {"pid": 9} if machine.t > 0 else None
    result = starter.start(18990, deps=deps)
    assert result["state"] == "started"
    assert result["ok"] is True


def test_a_page_that_never_answers_times_out():
    machine = Machine(answers_after=None)
    result = starter.start(18990, wait=10, deps=machine.deps())
    assert result["state"] == "timeout"
    assert 10 <= machine.t <= 11


def test_tow_run_starts_windowless_from_this_environment(tmp_path, monkeypatch):
    scripts = tmp_path / "venv" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "python.exe").write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(scripts / "python.exe"))
    assert starter.run_argv()[1:] == ["-m", "tow", "run"]
    assert starter.run_argv()[0] == str(scripts / "python.exe")  # no pythonw.exe next to it
    (scripts / "pythonw.exe").write_bytes(b"")
    assert starter.run_argv()[0] == str(scripts / "pythonw.exe")  # no console window
    monkeypatch.setattr(sys, "executable", "/opt/tow/app/.venv/bin/python")
    assert starter.run_argv()[0] == "/opt/tow/app/.venv/bin/python"


def test_the_spawn_is_detached_hidden_and_logs_inside_the_install(monkeypatch):
    seen: dict[str, Any] = {}

    class Backend:
        def spawn_detached(self, argv, **options):
            seen.update(argv=argv, **options)
            return 31

    monkeypatch.setattr("tow.platform.current", lambda: Backend())
    assert starter._spawn() == 31
    assert seen["hidden"] is True
    assert seen["argv"][-3:] == ["-m", "tow", "run"]
    assert seen["log_path"] == layout.logs_dir() / "run-stderr.log"
    assert seen["cwd"] == layout.install_root()
    assert seen["env"]["TOW_ROOT"] == str(layout.install_root())


def _fake_start(monkeypatch, result: dict[str, Any], seen: dict[str, Any]) -> None:
    def fake(port, *, wait, browser):
        seen.update(port=port, wait=wait, browser=browser)
        return {"url": f"http://127.0.0.1:{port}/", "pid": 1, **result}

    monkeypatch.setattr(starter, "start", fake)


def test_the_cli_starts_and_says_where(monkeypatch, capsys):
    seen: dict[str, Any] = {}
    _fake_start(monkeypatch, {"ok": True, "state": "started", "browser": True}, seen)
    assert cli.main(["start"]) == 0
    out = capsys.readouterr().out
    assert "TOW" in out
    assert "http://127.0.0.1:8787/" in out
    assert seen == {"port": 8787, "wait": 120.0, "browser": True}


def test_tow_no_browser_keeps_the_browser_closed(monkeypatch, capsys):
    seen: dict[str, Any] = {}
    _fake_start(monkeypatch, {"ok": True, "state": "started", "browser": False}, seen)
    monkeypatch.setenv("TOW_NO_BROWSER", "1")
    assert cli.main(["start", "--wait", "5"]) == 0
    assert seen["browser"] is False
    assert seen["wait"] == 5.0
    assert cli.main(["start", "--no-browser"]) == 0
    assert seen["browser"] is False


def test_a_browser_that_did_not_open_gets_the_address_to_open_by_hand(monkeypatch, capsys):
    seen: dict[str, Any] = {}
    _fake_start(monkeypatch, {"ok": True, "state": "running", "browser": False}, seen)
    assert cli.main(["start"]) == 0
    out = capsys.readouterr().out
    assert out.count("http://127.0.0.1:8787/") == 2  # running at ..., open ... by hand


@pytest.mark.parametrize("state", ["exited", "timeout"])
def test_a_failed_start_names_the_log(monkeypatch, capsys, state):
    seen: dict[str, Any] = {}
    _fake_start(monkeypatch, {"ok": False, "state": state, "browser": False}, seen)
    assert cli.main(["start"]) == 3
    assert "run.log" in capsys.readouterr().out


# --- an update cut off while it switched the code ------------------------------------------------


def _switch_record(phase: str = "switching", *, restored: bool = False) -> Path:
    record = layout.install_root() / ".update-switch.json"
    extra = ', "restored": true' if restored else ""
    record.write_text(f'{{"format": "tow-update-switch/v1", "phase": "{phase}", "old": [], "new": []{extra}}}')
    return record


@pytest.mark.parametrize("command", ["run", "start"])
def test_an_interrupted_update_is_not_started_over(monkeypatch, capsys, command):
    # Before: Start TOW, autostart and `tow run` ran the half-switched code of an update that was
    # cut off; only the next update noticed the record.
    monkeypatch.setattr("tow.supervisor.run_supervisor", lambda: pytest.fail("half-switched code started"))
    monkeypatch.setattr(starter, "start", lambda *_a, **_k: pytest.fail("half-switched code started"))
    _switch_record()
    assert cli.main([command]) == cli.EXIT_CANNOT_RUN
    out = capsys.readouterr().out
    assert "Update TOW" in out  # in the owner's language: what to run
    assert str(layout.install_root()) in out


def test_the_update_itself_starts_tow_while_it_holds_its_lock(monkeypatch):
    from tow.platform import locks
    from tow.store import init_lock_file

    monkeypatch.setattr("tow.supervisor.run_supervisor", lambda: 0)
    _switch_record()
    with (layout.install_root() / ".update.lock").open("a+b") as handle:
        init_lock_file(handle)
        assert locks.lock(handle, wait=False)
        try:
            assert cli.main(["run"]) == 0  # the updater's own start of the new version (health check)
        finally:
            locks.unlock(handle)
    assert cli.main(["run"]) == cli.EXIT_CANNOT_RUN  # the updater is gone, its record is not
    _switch_record("accepted")  # the switch was finished; only the record's removal was cut off
    assert cli.main(["run"]) == 0
    _switch_record(restored=True)  # a rollback put code and data back; only the removal was cut off
    assert cli.main(["run"]) == 0


# --- the start scripts ---------------------------------------------------------------------------


def test_the_start_scripts_refuse_an_interrupted_update_before_preparing_anything():
    windows = (ROOT / "scripts" / "tow-start.cmd").read_text(encoding="utf-8")
    refusal = windows.index("if defined TOW_CUT exit /b 3")
    assert windows.index('call "%~dp0tow-env.cmd"') < refusal < windows.index("tow-setup.cmd")
    assert "$env:TOW_ROOT" in windows  # the path never inside the command
    posix = (ROOT / "scripts" / "tow-start").read_text(encoding="utf-8")
    refusal = posix.index("if interrupted; then")
    assert refusal < posix.index("exit 3") < posix.index('"$scripts/tow" setup')


def _start_script(root: Path) -> str:
    """Run the platform's start script of the temp install ``root``: its output."""
    import subprocess

    env = {key: value for key, value in os.environ.items() if not key.startswith(("TOW_", "UV_"))}
    if os.name == "nt":
        system = os.environ.get("SYSTEMROOT", r"C:\Windows")
        env["PATH"] = os.path.join(system, "System32")  # no uv, no Python: a missed refusal fails at once
        argv = ["cmd.exe", "/d", "/c", str(root / "app" / "scripts" / "tow-start.cmd")]
    else:
        env["PATH"] = "/usr/bin:/bin"
        argv = ["/bin/sh", str(root / "app" / "scripts" / "tow-start")]
    done = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=120, check=False)
    assert done.returncode == 3, done.stdout + done.stderr  # refused, or no uv to prepare with
    assert not (root / "app" / ".venv").exists()
    return done.stdout + done.stderr


@pytest.mark.allow_system  # the platform's start script in a temp install; it ends before setup or uv
@pytest.mark.parametrize(
    ("record", "locked", "refused"),
    [
        ("{}", False, True),
        ('{"format": "tow-update-switch/v1", "phase": "switching", "old": [], "new": []}', False, True),
        # Before: refused whenever the record existed, unlike `tow run` and `tow start`.
        ('{"format": "tow-update-switch/v1", "phase": "accepted", "old": [], "new": []}', False, False),
        ('{"format": "tow-update-switch/v1", "phase": "switching", "restored": true}', False, False),
        ('{"format": "tow-update-switch/v1", "phase": "switching", "old": [], "new": []}', True, False),
    ],
)
def test_the_start_script_follows_the_interrupted_update_rule(tmp_path, record, locked, refused):
    import shutil

    from tow.platform import locks
    from tow.store import init_lock_file

    root = tmp_path / "TOW"
    shutil.copytree(ROOT / "scripts", root / "app" / "scripts")
    (root / "data").mkdir()
    (root / "config.yaml").write_text("port: 18999\n", encoding="utf-8")
    (root / ".update-switch.json").write_text(record, encoding="utf-8")
    with (root / ".update.lock").open("a+b") as handle:
        init_lock_file(handle)
        if locked:  # an update runs: it starts TOW itself
            assert locks.lock(handle, wait=False)
        try:
            output = _start_script(root)
        finally:
            if locked:
                locks.unlock(handle)
    assert ("update was cut off" in output) is refused, output
    if not refused:
        assert "could not be prepared" in output  # it went on to prepare TOW (no uv here)


def test_the_windows_start_script_prepares_offline_first_then_online():
    text = (ROOT / "scripts" / "tow-start.cmd").read_text(encoding="utf-8")
    assert text.isascii()
    assert not any(line.startswith(":") for line in text.splitlines())  # no labels in an LF file
    assert 'call "%~dp0tow-env.cmd"' in text
    offline = text.index('set "UV_OFFLINE=1"')
    first = text.index('call "%~dp0tow-setup.cmd"', offline)
    cleared = text.index('set "UV_OFFLINE="', first)
    second = text.index('call "%~dp0tow-setup.cmd"', cleared)
    assert offline < first < cleared < second < text.index('"%TOW_EXE%" start %*')
    assert "Unblock-File" in text
    assert "$env:TOW_HERE" in text  # the path never inside the command
    assert text.rstrip().splitlines()[-1] == "exit /b 0"


def test_the_posix_start_script_sets_up_then_starts():
    text = (ROOT / "scripts" / "tow-start").read_text(encoding="utf-8")
    assert text.startswith("#!/bin/sh\n")
    assert "\r" not in text
    assert text.index('"$scripts/tow" setup') < text.index('exec "$scripts/tow" start "$@"')
    assert 'os.environ["TOW_HERE"]' in text  # the path never inside the Python code
