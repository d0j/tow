"""Autostart backends with fake OS commands and an isolated home."""

from __future__ import annotations

import json
import os
import plistlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tow import cli
from tow.autostart import CommandResult, Install, backend, default_runner, platform_name
from tow.autostart.launchd import LaunchAgent
from tow.autostart.systemd import SystemdUser, _quoted
from tow.autostart.windows import WindowsTask, parse_task, same_path
from tow.paths import data_dir

NOT_FOUND = CommandResult(1, "", "ERROR: The system cannot find the file specified.")


@pytest.fixture(autouse=True)
def _layout_env(monkeypatch):
    # `tow autostart` exports the install layout to its own environment: restored after each test.
    monkeypatch.setenv("TOW_ROOT", os.environ.get("TOW_ROOT", str(Path.cwd())))
    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")


@pytest.fixture
def install(tmp_path) -> Install:
    root = tmp_path / "TOW"
    for name in ("Scripts/pythonw.exe", "bin/tow"):
        path = root / "app" / ".venv" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
    (root / "config.yaml").write_text("port: 8787\n", encoding="utf-8")
    return Install(root=root, app=root / "app", home=tmp_path / "home", user="owner", uid=501)


def old_task_xml(command: Path | str, *, enabled: bool = True) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-16"?>'
        '<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
        f"<Settings><Enabled>{str(enabled).lower()}</Enabled></Settings>"
        f"<Actions><Exec><Command>{command}</Command></Exec></Actions></Task>"
    )


class Scheduler:
    """Task Scheduler as schtasks sees it."""

    def __init__(self):
        self.tasks: dict[str, str] = {}
        self.calls: list[list[str]] = []
        self.create_fails = False

    def __call__(self, argv):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        if argv[0] == "schtasks":
            op = argv[1]
            if op == "/Query":
                xml = self.tasks.get(argv[3])
                return CommandResult(0, xml) if xml else NOT_FOUND
            if op == "/Create":
                if self.create_fails:
                    return CommandResult(1, "", "ERROR: Access is denied.")
                self.tasks[argv[4]] = Path(argv[6]).read_text(encoding="utf-16")
                return CommandResult(0, "SUCCESS")
            if op == "/Delete":
                return CommandResult(0) if self.tasks.pop(argv[3], None) else NOT_FOUND
            return CommandResult(0)
        return CommandResult(0)

    def ran(self, *prefix: str) -> list[list[str]]:
        return [call for call in self.calls if call[: len(prefix)] == list(prefix)]


# --- Windows ----------------------------------------------------------------------------------


def test_the_task_runs_pythonw_at_sign_in_and_is_read_back(install):
    scheduler = Scheduler()
    task = WindowsTask(install, scheduler)

    result = task.enable()

    assert result["ok"] is True
    parsed = parse_task(scheduler.tasks["TOW"])
    assert parsed is not None
    assert same_path(parsed["command"], install.app / ".venv" / "Scripts" / "pythonw.exe")
    assert parsed["arguments"] == "-m tow run"
    assert same_path(parsed["working_directory"], install.root)
    assert parsed["triggers"] == ["LogonTrigger"]
    assert parsed["logon_type"] == "InteractiveToken"
    assert (parsed["execution_limit"], parsed["restart_count"]) == ("PT0S", "999")
    assert result["status"]["without_login"] is False
    assert list((data_dir() / "tmp").iterdir()) == []  # XML removed


def test_without_signing_in_the_task_starts_at_boot_without_a_password(install):
    scheduler = Scheduler()
    result = WindowsTask(install, scheduler).enable(without_login=True)
    parsed = parse_task(scheduler.tasks["TOW"])
    assert parsed is not None
    assert result["ok"] is True
    assert parsed["logon_type"] == "S4U"
    assert parsed["triggers"] == ["BootTrigger", "LogonTrigger"]
    assert result["status"]["without_login"] is True


def test_a_development_checkout_or_a_missing_environment_is_refused(install, tmp_path):
    dev = Install(root=install.app, app=install.app, home=install.home)
    assert WindowsTask(dev, Scheduler()).enable()["refused"] is True
    (install.app / ".venv" / "Scripts" / "pythonw.exe").unlink()
    refused = WindowsTask(install, Scheduler()).enable()
    assert refused["refused"] is True
    assert "uv sync" in refused["error"]


def _other_install(tmp_path, *parts: str) -> Path:
    """A program of another TOW folder that exists."""
    program = tmp_path.joinpath("Other", "app", ".venv", *parts)
    program.parent.mkdir(parents=True, exist_ok=True)
    program.write_bytes(b"")
    return program


def test_another_folders_task_is_never_replaced_or_removed(install, tmp_path):
    scheduler = Scheduler()
    scheduler.tasks["TOW"] = old_task_xml(_other_install(tmp_path, "Scripts", "pythonw.exe"))
    task = WindowsTask(install, scheduler)
    assert task.status()["ours"] is False
    assert task.enable()["refused"] is True
    assert task.disable()["refused"] is True
    assert scheduler.ran("schtasks", "/Create") == []
    assert scheduler.ran("schtasks", "/Delete") == []


def test_a_task_of_a_moved_install_is_taken_over(install, tmp_path):
    # Its program no longer exists (that folder was moved or deleted): nothing to protect.
    scheduler = Scheduler()
    scheduler.tasks["TOW"] = old_task_xml(tmp_path / "Old" / "app" / ".venv" / "Scripts" / "pythonw.exe")
    task = WindowsTask(install, scheduler)
    assert (task.status()["ours"], task.status()["stale"]) == (False, True)
    assert task.enable()["ok"] is True
    assert task.status()["ours"] is True


def test_turning_it_off_deletes_the_task_and_reads_it_back(install):
    scheduler = Scheduler()
    task = WindowsTask(install, scheduler)
    task.enable()
    result = task.disable()
    assert result == {"ok": True, "changed": True, "status": result["status"]}
    assert result["status"]["state"] == "absent"
    again = task.disable()
    assert (again["ok"], again["changed"]) == (True, False)


def test_a_refused_creation_is_not_reported_as_on(install):
    scheduler = Scheduler()
    scheduler.create_fails = True
    result = WindowsTask(install, scheduler).enable()
    assert result["ok"] is False
    assert result["error"] == "ERROR: Access is denied."


def test_a_changed_task_is_not_on(install):
    scheduler = Scheduler()
    task = WindowsTask(install, scheduler)
    task.enable()
    scheduler.tasks["TOW"] = scheduler.tasks["TOW"].replace("<Arguments>-m tow run</Arguments>", "")
    status = task.status()
    assert (status["ours"], status["on"], status["problems"]) == (True, False, ["arguments"])
    assert task.start() is True
    assert scheduler.ran("schtasks", "/Run", "/TN", "TOW")


def test_an_unreadable_query_is_an_error_not_absent(install):
    task = WindowsTask(install, lambda _argv: CommandResult(1, "", "ERROR: RPC server unavailable"))
    assert task.status()["state"] == "error"
    assert WindowsTask(install, lambda _argv: CommandResult(0, "not xml")).status()["state"] == "error"
    assert parse_task("<<") is None


# --- Linux ------------------------------------------------------------------------------------


class Systemd:
    def __init__(self, *, available=True, linger=False):
        self.available = available
        self.linger = linger
        self.enabled = False
        self.calls: list[list[str]] = []

    def __call__(self, argv):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        if argv[:3] == ["systemctl", "--user", "show-environment"]:
            return CommandResult(0 if self.available else 1, "", "" if self.available else "Failed to connect to bus")
        if argv[:3] == ["systemctl", "--user", "enable"]:
            self.enabled = True
        if argv[:3] == ["systemctl", "--user", "disable"]:
            self.enabled = False
        if argv[:3] == ["systemctl", "--user", "is-enabled"]:
            return CommandResult(0 if self.enabled else 1, "enabled\n" if self.enabled else "disabled\n")
        if argv[0] == "loginctl":
            return CommandResult(0, f"Linger={'yes' if self.linger else 'no'}\n")
        return CommandResult(0)


def test_the_systemd_unit_runs_tow_from_the_install(install, monkeypatch):
    from tow.autostart.systemd import TIMEOUT_STOP_SEC
    from tow.supervisor import SIGNAL_STOP_BUDGET_SEC

    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    systemd = Systemd()
    unit_backend = SystemdUser(install, systemd)

    result = unit_backend.enable()

    unit = (install.home / ".config" / "systemd" / "user" / "tow.service").read_text(encoding="utf-8")
    assert result["ok"] is True
    assert "Restart=on-failure\n" in unit
    assert "RestartPreventExitStatus=3\n" in unit
    assert "KillMode=mixed\n" in unit  # SIGTERM to `tow run` alone; it stops its children itself
    assert f"TimeoutStopSec={TIMEOUT_STOP_SEC}\n" in unit
    assert TIMEOUT_STOP_SEC >= SIGNAL_STOP_BUDGET_SEC
    assert "network-online" not in unit  # a system target: the user manager has none
    assert f"WorkingDirectory={install.root}\n" in unit
    assert "Environment=" in unit
    assert "TOW_ROOT=" in unit
    assert unit.count("ExecStart=") == 1
    assert ["systemctl", "--user", "daemon-reload"] in systemd.calls
    assert ["systemctl", "--user", "enable", "--now", "tow.service"] in systemd.calls
    assert "hint" not in result
    assert unit_backend.start() is True


def test_the_unit_tells_tow_that_systemd_started_it(install):
    # Stopping the unit ends every process it started: TOW must know, so that the web page
    # never starts an update the stop would kill halfway (tow.web_update).
    unit_backend = SystemdUser(install, Systemd())
    assert "Environment=TOW_AUTOSTART=systemd\n" in unit_backend.unit()
    assert unit_backend.enable()["ok"] is True
    # A unit written by an older TOW (without the line) still works: it stays "on".
    unit_backend.unit_path.write_text(unit_backend.unit(marked=False), encoding="utf-8")
    assert "TOW_AUTOSTART" not in unit_backend.unit(marked=False)
    assert unit_backend.status()["on"] is True


def test_the_service_manager_is_recognised_also_from_an_older_unit():
    from tow.autostart import service_manager

    in_unit = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/tow.service\n"
    in_terminal = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/gnome-terminal-server.service\n"
    assert service_manager({"TOW_AUTOSTART": "systemd"}, lambda: "") == "systemd"
    assert service_manager({"TOW_AUTOSTART": "launchd"}, lambda: "") == "launchd"
    assert service_manager({"INVOCATION_ID": "abc"}, lambda: in_unit) == "systemd"
    assert service_manager({"INVOCATION_ID": "abc"}, lambda: "") == "systemd"  # unknown: careful
    assert service_manager({"INVOCATION_ID": "abc"}, lambda: in_terminal) is None  # a desktop terminal
    assert service_manager({"XPC_SERVICE_NAME": "io.tow"}, lambda: "") == "launchd"
    assert service_manager({"XPC_SERVICE_NAME": "0"}, lambda: "") is None
    assert service_manager({}, lambda: in_unit) is None


def test_without_login_on_linux_needs_lingering_and_says_how(install):
    result = SystemdUser(install, Systemd(linger=False)).enable(without_login=True)
    assert result["ok"] is True
    assert result["hint"].endswith("loginctl enable-linger owner")
    lingering = SystemdUser(install, Systemd(linger=True))
    assert lingering.enable(without_login=True)["status"]["without_login"] is True


def test_paths_with_percent_and_dollar_name_themselves_in_the_unit(tmp_path):
    from tow.autostart.systemd import registered_command

    root = tmp_path / "50% $HOME TOW"
    path = root / "app" / ".venv" / "bin" / "tow"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"")
    (root / "config.yaml").write_text("port: 8787\n", encoding="utf-8")
    install = Install(root=root, app=root / "app", home=tmp_path / "home", user="owner")
    unit_backend = SystemdUser(install, Systemd())
    unit = unit_backend.unit()
    assert "50%% $$HOME TOW" in unit.split("ExecStart=", 1)[1].splitlines()[0]  # no specifier, no variable
    assert f"WorkingDirectory={str(root).replace('%', '%%')}\n" in unit
    environment = unit.split("Environment=", 1)[1].splitlines()[0]
    assert "TOW_ROOT=" in environment
    assert "50%% $HOME TOW" in environment  # variables do not expand in Environment=
    assert registered_command(unit) == str(path)
    assert unit_backend.enable()["ok"] is True  # read back as its own unit


def test_a_unit_of_a_moved_install_is_taken_over(install, tmp_path):
    unit_backend = SystemdUser(install, Systemd())
    unit_backend.unit_path.parent.mkdir(parents=True)
    unit_backend.unit_path.write_text(
        f"[Service]\nExecStart={_quoted(tmp_path / 'gone' / 'tow')} run\n", encoding="utf-8"
    )
    assert unit_backend.status()["stale"] is True
    assert unit_backend.enable()["ok"] is True
    assert unit_backend.status()["ours"] is True


def test_with_systemd_running_an_unreachable_user_manager_is_an_error(install, tmp_path):
    # e.g. an SSH session without a user manager: a desktop entry would never start TOW there.
    booted = tmp_path / "run-systemd-system"
    booted.mkdir()
    unit_backend = SystemdUser(install, Systemd(available=False))
    unit_backend.systemd_runtime = booted
    result = unit_backend.enable()
    assert result["refused"] is True
    assert "Failed to connect to bus" in result["error"]
    assert not unit_backend.desktop_path.exists()
    assert not unit_backend.unit_path.exists()


def test_without_a_systemd_user_manager_an_autostart_entry_is_used(install, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(install.home / "cfg"))
    fallback = SystemdUser(install, Systemd(available=False))
    fallback.systemd_runtime = tmp_path / "no-systemd-here"  # not a systemd machine at all
    assert fallback.enable(without_login=True)["refused"] is True
    result = fallback.enable()
    desktop = (install.home / "cfg" / "autostart" / "tow.desktop").read_text(encoding="utf-8")
    assert result["ok"] is True
    assert result["status"]["fallback"] is True
    assert "Exec=env " in desktop
    assert fallback.start() is False  # nothing to start it with: the caller starts `tow run`
    off = fallback.disable()
    assert off["ok"] is True
    assert not (install.home / "cfg" / "autostart" / "tow.desktop").exists()


def test_turning_the_unit_off_keeps_a_running_tow(install):
    systemd = Systemd()
    unit_backend = SystemdUser(install, systemd)
    unit_backend.enable()
    result = unit_backend.disable()
    assert result["ok"] is True
    assert ["systemctl", "--user", "disable", "tow.service"] in systemd.calls
    assert not any("--now" in call for call in systemd.calls if call[2:3] == ["disable"])
    assert not unit_backend.unit_path.exists()


def test_another_installs_unit_is_left_alone(install, tmp_path):
    other = _other_install(tmp_path, "bin", "tow")
    unit_backend = SystemdUser(install, Systemd())
    unit_backend.unit_path.parent.mkdir(parents=True)
    unit_backend.unit_path.write_text(f"[Service]\nExecStart={_quoted(other)} run\n", encoding="utf-8")
    assert unit_backend.enable()["refused"] is True
    assert unit_backend.disable()["refused"] is True


# --- macOS ------------------------------------------------------------------------------------


class Launchctl:
    def __init__(self):
        self.loaded = False
        self.calls: list[list[str]] = []

    def __call__(self, argv):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        if argv[1] == "print":
            return CommandResult(0 if self.loaded else 113, "", "" if self.loaded else "Could not find service")
        if argv[1] == "bootstrap":
            self.loaded = True
        if argv[1] == "bootout":
            self.loaded = False
        return CommandResult(0)


def test_the_launch_agent_keeps_tow_alive_only_after_a_failure(install):
    launchctl = Launchctl()
    agent = LaunchAgent(install, launchctl)

    result = agent.enable()

    with agent.plist_path.open("rb") as handle:
        plist = plistlib.load(handle)
    assert result["ok"] is True
    assert plist["ProgramArguments"] == [str(install.app / ".venv" / "bin" / "tow"), "run"]
    assert plist["KeepAlive"] == {"SuccessfulExit": False}
    assert plist["RunAtLoad"] is True
    assert plist["ExitTimeOut"] == 60
    assert plist["ProcessType"] == "Adaptive"
    assert plist["EnvironmentVariables"]["TOW_AUTOSTART"] == "launchd"  # port busy: exit 0, no retry loop
    assert ["launchctl", "bootstrap", "gui/501", str(agent.plist_path)] in launchctl.calls
    assert agent.start() is True
    assert ["launchctl", "kickstart", "gui/501/io.tow"] in launchctl.calls


def test_the_exit_timeout_covers_the_supervisors_stop():
    from tow.autostart.launchd import EXIT_TIMEOUT_SEC
    from tow.supervisor import SIGNAL_STOP_BUDGET_SEC

    assert EXIT_TIMEOUT_SEC >= SIGNAL_STOP_BUDGET_SEC


def test_turning_the_launch_agent_off_unloads_it(install):
    launchctl = Launchctl()
    agent = LaunchAgent(install, launchctl)
    assert agent.disable() == {"ok": True, "changed": False, "status": agent.status()}
    agent.enable()
    result = agent.disable()
    assert result["ok"] is True
    assert not agent.plist_path.exists()
    assert ["launchctl", "bootout", "gui/501/io.tow"] in launchctl.calls  # not left loaded (KeepAlive)
    assert launchctl.loaded is False
    assert "tow run" in result["hint"]
    assert agent.enable(without_login=True)["refused"] is True


def test_a_launch_agent_not_loaded_is_bootstrapped_to_start(install):
    launchctl = Launchctl()
    agent = LaunchAgent(install, launchctl)
    agent.enable()
    launchctl.loaded = False  # e.g. after an update's bootout
    assert agent.start() is True
    assert launchctl.calls[-1] == ["launchctl", "bootstrap", "gui/501", str(agent.plist_path)]


def test_a_launch_agent_of_a_moved_install_is_taken_over(install, tmp_path):
    launchctl = Launchctl()
    agent = LaunchAgent(install, launchctl)
    agent.plist_path.parent.mkdir(parents=True)
    old = {"Label": "io.tow", "ProgramArguments": [str(tmp_path / "gone" / "tow"), "run"]}
    agent.plist_path.write_bytes(plistlib.dumps(old))
    launchctl.loaded = True
    assert agent.status()["stale"] is True
    assert agent.enable()["ok"] is True
    assert launchctl.calls.index(["launchctl", "bootout", "gui/501/io.tow"]) < len(launchctl.calls)
    assert agent.status()["ours"] is True


def test_another_installs_launch_agent_is_left_alone(install, tmp_path):
    launchctl = Launchctl()
    agent = LaunchAgent(install, launchctl)
    agent.plist_path.parent.mkdir(parents=True)
    other = {"Label": "io.tow", "ProgramArguments": [str(_other_install(tmp_path, "bin", "tow")), "run"]}
    agent.plist_path.write_bytes(plistlib.dumps(other))
    assert agent.enable()["refused"] is True
    assert agent.disable()["refused"] is True
    assert not any(call[1] == "bootout" for call in launchctl.calls)


# --- choosing a backend -----------------------------------------------------------------------


def test_each_os_gets_its_backend(install):
    assert isinstance(backend("windows", runner=Scheduler(), install=install), WindowsTask)
    assert isinstance(backend("linux", runner=Scheduler(), install=install), SystemdUser)
    assert isinstance(backend("macos", runner=Scheduler(), install=install), LaunchAgent)
    assert platform_name() in {"windows", "linux", "macos"}
    current = Install.current()
    assert current.is_runtime is False  # tests run from a development checkout
    from tow import paths
    from tow import platform as tow_platform

    # The owner's home and account come from tow.paths and tow.platform, like every location.
    assert (current.home, current.uid, current.user) == (
        paths.user_home(),
        tow_platform.user_id(),
        tow_platform.user_name(),
    )


def test_the_default_runner_never_raises(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout="ok", stderr=""))
    assert default_runner(["x"]) == CommandResult(0, "ok", "")

    def broken(*_a, **_k):
        raise FileNotFoundError("no such program")

    monkeypatch.setattr(subprocess, "run", broken)
    assert default_runner(["x"]).returncode == 127


# --- the CLI ----------------------------------------------------------------------------------


def test_cli_status_on_off_go_to_the_backend(monkeypatch, capsys):
    calls = []

    class Fake:
        def status(self):
            return {"on": True}

        def enable(self, *, without_login=False):
            calls.append(("on", without_login))
            return {"ok": True, "hint": "a hint"}

        def disable(self):
            calls.append(("off",))
            return {"ok": False, "error": "nope"}

    monkeypatch.setattr("tow.autostart.backend", lambda *a, **k: Fake())
    assert cli.main(["autostart", "status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"on": True}
    assert cli.main(["autostart", "on", "--without-login"]) == 0
    assert "a hint" in capsys.readouterr().out
    assert cli.main(["autostart", "off"]) == 2
    assert "Автозапуск не изменён: nope" in capsys.readouterr().out
    assert calls == [("on", True), ("off",)]
