"""`tow run` as a process: single instance, control files, CLI, environment and OS adapter."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_supervisor import Clock, World

from tow import cli
from tow.supervisor import _os, layout, request_restart, request_stop, run_supervisor, wait_stopped


@pytest.fixture(autouse=True)
def run_env(monkeypatch):
    # tow run/stop/restart export the layout to their own environment: restored after each test.
    monkeypatch.setenv("TOW_ROOT", os.environ.get("TOW_ROOT", str(Path.cwd())))
    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")


def _world_that_stops_after(ticks: int) -> World:
    clock = Clock()
    world = World(clock=clock)
    calls = {"n": 0}

    def sleep(seconds):
        calls["n"] += 1
        clock.advance(seconds)
        if calls["n"] == ticks:
            layout.request("stop", by="test")

    world.sleep = sleep  # type: ignore[attr-defined]
    return world


def test_run_starts_serves_and_stops_on_request(run_env):
    world = _world_that_stops_after(5)
    deps = world.deps()
    deps.sleep = world.sleep  # type: ignore[attr-defined]

    assert run_supervisor(deps) == 0

    assert [child.argv[3] for child in world.children] == ["serve"]
    assert world.stopped == [world.children[0].pid]
    assert not layout.pid_path().exists()  # released
    assert layout.running() is None
    assert "started" in (layout.logs_dir() / "run.log").read_text(encoding="utf-8")


def test_closing_the_terminal_of_tow_run_stops_it_cleanly(run_env, monkeypatch):
    # SIGHUP (Linux, macOS: the terminal of `tow run` closed) ended it at once, leaving its web
    # server and a running job; it is now a stop like SIGTERM, and both are restored afterwards.
    import signal

    hangup = getattr(signal, "SIGHUP", 1)
    monkeypatch.setattr(signal, "SIGHUP", hangup, raising=False)
    handlers: dict[int, object] = {}
    installed = []

    def fake_signal(number, handler):
        installed.append((number, handler))
        previous = handlers.get(number, signal.SIG_DFL)
        handlers[number] = handler
        return previous

    monkeypatch.setattr(signal, "signal", fake_signal)
    monkeypatch.setattr(signal, "getsignal", lambda number: handlers.get(number, signal.SIG_DFL))
    world = World(clock=Clock())
    deps = world.deps()
    ticks = {"n": 0}

    def sleep(seconds):
        ticks["n"] += 1
        world.clock.advance(seconds)
        if ticks["n"] == 3:
            handlers[hangup](hangup, None)  # the terminal closes

    deps.sleep = sleep
    assert run_supervisor(deps) == 0
    assert world.stopped == [world.servers()[0].pid]  # its web server stopped, not left behind
    assert {number for number, _ in installed} == {signal.SIGTERM, hangup}
    assert handlers == {signal.SIGTERM: signal.SIG_DFL, hangup: signal.SIG_DFL}  # restored


def test_variables_that_lead_a_portable_install_elsewhere_are_named(monkeypatch, tmp_path):
    # A TOW_HOME / TOW_CONFIG / TOW_MASTER_KEY_FILE set for the whole account (left from another
    # install) wins in every start - launcher, Task Scheduler, systemd: said in run.log.
    root = tmp_path / "TOW"
    monkeypatch.setattr(layout, "install_root", lambda: root)
    monkeypatch.setattr(layout, "repo_root", lambda: root / "app")
    env = {
        "TOW_HOME": str(root / "data"),
        "TOW_CONFIG": str(tmp_path / "Other" / "config.yaml"),
        "TOW_MASTER_KEY_FILE": str(tmp_path / "old" / "master.key"),
    }
    assert layout.outside_overrides(env) == ["TOW_CONFIG", "TOW_MASTER_KEY_FILE"]
    assert layout.outside_overrides({"TOW_MASTER_KEY_FILE": "master.key"}) == []  # relative: inside data
    monkeypatch.setattr(layout, "repo_root", lambda: root)  # a development checkout
    assert layout.outside_overrides(env) == []


def test_run_logs_a_variable_leading_out_of_the_install(run_env, monkeypatch):
    monkeypatch.setattr(layout, "outside_overrides", lambda: ["TOW_CONFIG"])
    world = _world_that_stops_after(2)
    deps = world.deps()
    deps.sleep = world.sleep  # type: ignore[attr-defined]
    assert run_supervisor(deps) == 0
    assert "TOW_CONFIG=" in (layout.logs_dir() / "run.log").read_text(encoding="utf-8")


def test_a_second_run_is_refused_while_one_holds_the_lock(run_env, capsys):
    held = layout.InstanceLock()
    assert held.acquire()
    layout.write_json(layout.pid_path(), {"pid": 4242})
    try:
        assert layout.running() == {"pid": 4242}
        assert run_supervisor(World(clock=Clock()).deps()) == 0  # nothing to do, not a failure
        assert "4242" in capsys.readouterr().out
    finally:
        held.release()
    assert layout.running() is None


def test_a_start_by_the_os_resolves_the_install_before_touching_data(run_env, monkeypatch, tmp_path):
    # Task Scheduler / systemd / launchd start `python -m tow run` without the launcher's
    # variables: TOW_HOME and TOW_CONFIG must be known before the lock in data/run is taken.
    seen = {}

    class Lock:
        def acquire(self):
            seen["home"] = os.environ.get("TOW_HOME")
            return False

    resolved = str(tmp_path / "TOW" / "data")
    monkeypatch.setattr(layout, "child_env", lambda: {**os.environ, "TOW_HOME": resolved})
    monkeypatch.setattr(layout, "InstanceLock", Lock)
    monkeypatch.setenv("TOW_HOME", os.environ["TOW_HOME"])  # restored after the test
    assert run_supervisor(World(clock=Clock()).deps()) == 0
    assert seen == {"home": resolved}


def test_a_supervisor_that_fails_does_not_leave_its_web_server(run_env):
    # Repro (1.21): the loop died on an error; the child `tow serve` kept the port, the next
    # `tow run` exited 3 "port busy" and `tow stop` said TOW was not running.
    world = World(clock=Clock())
    deps = world.deps()
    ticks = {"n": 0}

    def sleep(seconds):
        ticks["n"] += 1
        world.clock.advance(seconds)
        if ticks["n"] == 5:
            raise RuntimeError("the loop broke")

    deps.sleep = sleep
    with pytest.raises(RuntimeError, match="the loop broke"):
        run_supervisor(deps)
    assert world.stopped == [world.servers()[0].pid]
    assert layout.running() is None


@pytest.mark.parametrize("job_name", ["check", "progress", "backup"])
@pytest.mark.parametrize("stop_failure", [None, "refused", "ignored", "error"])
@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt])
def test_abnormal_exit_attempts_every_child_before_releasing_the_lock(
    run_env, monkeypatch, caplog, tmp_path, job_name, stop_failure, failure
):
    from tow.supervisor.core import Supervisor

    world = World(clock=Clock())
    deps = world.deps()
    stopped = []
    expected_pid = tmp_path / "run" / "run.pid"

    def broken_loop(supervisor):
        if supervisor.server.child is None:
            supervisor._start_server(world.clock.wall, world.clock.mono)
            supervisor._start_job(job_name, world.clock.wall, world.clock.mono)
        raise failure("original loop failure")

    def stop(child):
        assert expected_pid.is_file()
        stopped.append((child.pid, layout.running() is not None))
        if "serve" not in child.argv:
            if stop_failure == "refused":
                return False
            if stop_failure == "ignored":
                return True
            if stop_failure == "error":
                raise OSError("secret=must-not-be-displayed")
        return world.stop(child)

    monkeypatch.setattr(Supervisor, "loop", broken_loop)
    deps.stop = stop
    with pytest.raises(failure, match="original loop failure"):
        run_supervisor(deps)
    assert stopped == [(world.jobs()[0].pid, True), (world.servers()[0].pid, True)]
    assert world.servers()[0].poll() == -9
    if stop_failure is None:
        assert world.jobs()[0].poll() == -9
    else:
        assert "stop was not confirmed" in caplog.text or "OSError" in caplog.text
    assert "must-not-be-displayed" not in caplog.text
    assert not expected_pid.exists()
    assert layout.running() is None


def test_the_next_run_takes_over_a_server_left_by_a_killed_supervisor(run_env, tmp_path):
    # A supervisor killed outright (no finally) left its server; the next one stops it and runs.
    first = World(clock=Clock())
    deps = first.deps()
    layout.write_json(layout.status_path(), {"server": {"state": "up", "pid": 4100}})
    log = layout.logs_dir() / "serve.log"
    deps.port_owner = lambda _port: {"pid": 4100, "cmd": f"python -m tow serve --log-file {log}", "parent": 1}
    first.port_busy = True
    taken = []

    def stop_pid(pid):
        taken.append(pid)
        first.port_busy = False
        return True

    deps.stop_pid = stop_pid
    world = _world_that_stops_after(3)
    world.port_busy = True
    deps.port_open = lambda _port: first.port_busy
    deps.spawn, deps.stop, deps.sleep = world.spawn, world.stop, world.sleep  # type: ignore[attr-defined]
    assert run_supervisor(deps) == 0
    assert taken == [4100]
    assert len(world.servers()) == 1


def test_run_refuses_when_another_server_holds_the_port(run_env, capsys):
    world = World(clock=Clock(), port_busy=True)
    assert run_supervisor(world.deps()) == 3
    assert "порт 8787 уже занят другой программой" in capsys.readouterr().out
    assert world.children == []


def test_under_launchd_a_busy_port_is_not_a_failure_to_retry(run_env, monkeypatch, capsys):
    # launchd's KeepAlive retries a failed agent without a limit: said once, exit 0.
    monkeypatch.setenv("TOW_AUTOSTART", "launchd")
    world = World(clock=Clock(), port_busy=True)
    assert run_supervisor(world.deps()) == 0
    assert "8787" in capsys.readouterr().out
    assert "8787" in (layout.logs_dir() / "run.log").read_text(encoding="utf-8")


def test_the_terminal_gets_the_log_only_when_there_is_one(run_env, monkeypatch):
    from tow.supervisor import LOG, _interactive, _logging

    class Stream:
        def __init__(self, tty):
            self.tty = tty

        def isatty(self):
            return self.tty

        def write(self, _text):
            return 0

        def flush(self):
            pass

    assert _interactive(Stream(True)) is True
    assert _interactive(Stream(False)) is False  # the journal, launchd.log: run.log has it all
    assert _interactive(None) is False  # pythonw
    monkeypatch.setattr("sys.stderr", Stream(False))
    handlers = _logging()
    try:
        assert len(handlers) == 1
    finally:
        for handler in handlers:
            LOG.removeHandler(handler)
            handler.close()


def test_old_requests_do_not_stop_a_new_supervisor(run_env):
    layout.request("stop", by="an update that crashed")
    world = _world_that_stops_after(3)
    deps = world.deps()
    deps.sleep = world.sleep  # type: ignore[attr-defined]
    assert run_supervisor(deps) == 0
    assert len(world.children) == 1  # it served until the new request, not stopped at once


def test_stop_and_restart_without_a_supervisor_say_so(capsys):
    assert request_stop() is None
    assert request_restart() is None
    assert cli.main(["stop"]) == 0
    assert cli.main(["restart"]) == 3  # nothing to restart: cannot run
    out = capsys.readouterr().out
    assert out.count("TOW (tow run) не запущен.") == 2


def test_stop_and_restart_write_control_requests(capsys):
    held = layout.InstanceLock()
    assert held.acquire()
    try:
        assert cli.main(["restart"]) == 0
        assert layout.read_json(layout.control_dir() / "restart")["by"] == "cli"
        assert cli.main(["stop", "--wait", "0"]) == 0
        assert layout.read_json(layout.control_dir() / "stop")["action"] == "stop"
        ticks = iter(range(100))
        assert wait_stopped(1.0, sleep=lambda _s: None, monotonic=lambda: next(ticks) / 10) is False
        assert cli.main(["stop", "--wait", "0.001"]) == 2
    finally:
        held.release()
    assert wait_stopped(1.0) is True
    out = capsys.readouterr().out
    assert "TOW перезапускает веб-сервер" in out
    assert "TOW ещё останавливается" in out


def test_a_control_request_is_taken_once():
    payload = layout.request("restart", by="settings", operation_id="restart-1")
    assert payload["operation_id"] == "restart-1"
    assert layout.take_request("restart") == payload
    assert layout.take_request("restart") is None
    with pytest.raises(ValueError, match="unknown control action"):
        layout.request("reboot")


# --- the layout -------------------------------------------------------------------------------


def test_the_install_root_is_the_parent_of_a_runtime_clone(tmp_path, monkeypatch):
    app = tmp_path / "TOW" / "app"
    app.mkdir(parents=True)
    (app / "pyproject.toml").write_text("[project]\nname = 'tow'\n", encoding="utf-8")  # a checkout
    (app / "src" / "tow").mkdir(parents=True)
    monkeypatch.setattr("tow.paths.repo_root", lambda: app)
    monkeypatch.delenv("TOW_ROOT", raising=False)
    assert layout.install_root() == app  # a development checkout: nothing next to it
    (tmp_path / "TOW" / "config.yaml").write_text("port: 8787\n", encoding="utf-8")
    assert layout.install_root() == tmp_path / "TOW"
    monkeypatch.setenv("TOW_ROOT", str(app))  # what tow-env.cmd set before 1.18: the code folder
    assert layout.install_root() == tmp_path / "TOW"
    monkeypatch.setenv("TOW_ROOT", str(tmp_path / "elsewhere"))
    assert layout.install_root() == tmp_path / "elsewhere"


def test_children_get_the_runtime_layout(tmp_path, monkeypatch):
    root = tmp_path / "TOW"
    (root / "app").mkdir(parents=True)
    (root / "config.yaml").write_text("port: 8787\n", encoding="utf-8")
    monkeypatch.setattr("tow.paths.repo_root", lambda: root / "app")
    monkeypatch.setattr(layout, "repo_root", lambda: root / "app")
    monkeypatch.delenv("TOW_ROOT", raising=False)
    env = layout.child_env({"PATH": "x"})
    assert env["TOW_HOME"] == str(root / "data")
    assert env["TOW_CONFIG"] == str(root / "config.yaml")
    assert env["TOW_ROOT"] == str(root)
    assert env["PYTHONIOENCODING"] == "utf-8"
    kept = layout.child_env({"TOW_HOME": "explicit"})
    assert kept["TOW_HOME"] == "explicit"


# --- the default dependencies -----------------------------------------------------------------


def test_facts_come_from_the_state_and_the_night_copies():
    from tow.snapshots import _record
    from tow.store import save_state
    from tow.supervisor import _facts

    save_state({"topics": [], "health": {"auto_at_ts": 1234}})
    assert _facts() == {"last_scheduled_check": 1234.0, "last_backup_ok": 0.0, "topic_timers": {}, "timer_policies": {}}
    _record(last_ok_at=99.5)
    assert _facts()["last_backup_ok"] == 99.5


def test_the_watchdog_duty_gets_the_wake_and_never_probes_the_port(monkeypatch):
    from tow import watchdog
    from tow.supervisor import _watchdog_pass

    seen = {}
    monkeypatch.setattr(watchdog, "run_watchdog", lambda **kwargs: seen.update(kwargs) or {"ok": True})
    assert _watchdog_pass(123.0) == {"ok": True}
    assert set(seen) == {"wake_ts", "probes", "data_id"}  # 1.21: the watchdog restarts nothing at all
    assert seen["wake_ts"] == 123.0
    assert seen["probes"].port_listening(8787) is False


def test_the_watchdog_duty_removes_expired_undo_secrets(monkeypatch):
    # 1.21: an expired "Undo" kept its saved secrets until somebody opened a page.
    from tow import watchdog
    from tow.store import load_state, save_secret_undo, save_state, secret_undo_path
    from tow.supervisor import _watchdog_pass

    reference = save_secret_undo({"telegram": {"token": "old"}})
    save_state(
        {"topics": [], "undo": {"kind": "settings", "secrets_undo_ref": reference, "ts": "2026-01-01T00:00:00+00:00"}}
    )
    assert secret_undo_path().exists()
    monkeypatch.setattr(watchdog, "run_watchdog", lambda **_kwargs: {"ok": True})

    _watchdog_pass(None)

    assert not secret_undo_path().exists()
    assert "undo" not in load_state()


def test_a_failing_undo_cleanup_never_stops_the_watchdog_duty(monkeypatch):
    from tow import undo, watchdog
    from tow.supervisor import _watchdog_pass

    def broken():
        raise OSError("disk gone")

    monkeypatch.setattr(undo, "cleanup_needed", lambda: True)
    monkeypatch.setattr(undo, "cleanup", broken)
    monkeypatch.setattr(watchdog, "run_watchdog", lambda **_kwargs: {"ok": True})
    assert _watchdog_pass(None) == {"ok": True}


def test_default_deps_wire_the_real_pieces():
    from tow import lifecycle
    from tow.supervisor import default_deps
    from tow.watchdog import healthy

    deps = default_deps()
    assert deps.healthy is healthy
    assert deps.restart_marker is lifecycle.update_restart_marker
    assert deps.crash_line(0) == ""


class Processes:
    """tow.platform's process calls, recorded."""

    name = "windows"

    def __init__(self, alive=True):
        self.alive = alive
        self.calls = []

    def process_alive(self, pid):
        return self.alive

    def terminate(self, pid, timeout=10.0):
        self.calls.append(("terminate", pid, timeout))
        return True

    def popen_options(self, *, new_group=False, hidden=True):
        return {"creationflags": 0x08000200 if new_group and hidden else 0, "close_fds": True}


def test_spawned_children_write_to_their_log_and_are_stopped_whole(monkeypatch, tmp_path):
    from tow import platform
    from tow.supervisor import _spawn, _stop

    calls = []

    def fake_spawn(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(pid=77, poll=lambda: None, wait=lambda timeout: 0)

    monkeypatch.setattr(_os, "spawn", fake_spawn)
    child = _spawn(["py", "-m", "tow", "serve"], tmp_path / "logs" / "serve-stderr.log", True)
    assert child.pid == 77
    assert calls[0][1]["env"]["PYTHONIOENCODING"] == "utf-8"
    assert (tmp_path / "logs" / "serve-stderr.log").exists()
    processes = Processes()
    with platform.use(processes):  # type: ignore[arg-type]
        assert _stop(child) is True
    assert processes.calls == [("terminate", 77, 10.0)]
    gone = Processes(alive=False)
    with platform.use(gone):  # type: ignore[arg-type]
        assert _stop(child) is True  # nothing to stop counts as stopped
    assert gone.calls == []


def test_default_stop_does_not_terminate_a_reused_pid(monkeypatch):
    from tow.supervisor import _stop

    def forbidden(*_args):
        pytest.fail("an exited child's PID must not be terminated")

    monkeypatch.setattr("tow.supervisor._stop_pid", forbidden)
    assert _stop(SimpleNamespace(pid=77, poll=lambda: 0)) is True


# --- the OS adapter ---------------------------------------------------------------------------


def _free_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_a_free_port_is_free_at_once_without_a_connection(monkeypatch):
    # A refused loopback connection costs a second and a half on Windows: every `tow run` and
    # `tow setup` waited for it. A port that can be bound needs no connection at all.
    import time

    def forbidden(*_args, **_kwargs):
        pytest.fail("a bindable port must not be probed with a connection")

    monkeypatch.setattr(_os.socket, "create_connection", forbidden)
    port = _free_port()
    started = time.perf_counter()
    assert _os.port_open(port) is False
    assert time.perf_counter() - started < 0.5


def test_a_listening_port_is_open():
    import socket

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = int(listener.getsockname()[1])
        assert _os.port_free(port) is False
        assert _os.port_open(port) is True


def test_a_port_that_cannot_be_bound_is_confirmed_by_a_connection(monkeypatch):
    import contextlib

    asked = []

    def refused(address, timeout):
        asked.append((address, timeout))
        raise ConnectionRefusedError

    monkeypatch.setattr(_os, "port_free", lambda _port: False)  # e.g. bound but not listening
    monkeypatch.setattr(_os.socket, "create_connection", refused)
    assert _os.port_open(8790) is False
    assert asked == [(("127.0.0.1", 8790), 1.5)]
    monkeypatch.setattr(_os.socket, "create_connection", lambda address, timeout: contextlib.nullcontext())
    assert _os.port_open(8790) is True


@pytest.mark.parametrize("name", ["windows", "linux"])
def test_the_bind_probe_shares_no_port_on_windows(monkeypatch, name):
    # Windows: SO_REUSEADDR would let the probe bind a port another program listens on.
    from tow import platform

    options = []

    class Probe:
        def __init__(self, *_args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def setsockopt(self, *args):
            options.append(args)

        def bind(self, address):
            options.append(("bind", address[0]))

    monkeypatch.setattr(_os.socket, "socket", Probe)
    with platform.use(platform.backend_for(name)):
        assert _os.port_free(8790) is True
    binds = [item for item in options if item[0] == "bind"]
    assert binds == [("bind", "127.0.0.1"), ("bind", "0.0.0.0")]  # wildcard listeners too
    assert (len(options) == 2) is (name == "windows")


@pytest.mark.parametrize("name", ["windows", "linux", "macos"])
def test_children_are_hidden_and_in_their_own_group(monkeypatch, tmp_path, name):
    from tow import platform

    seen = []
    monkeypatch.setattr(
        subprocess, "Popen", lambda argv, **kwargs: seen.append((argv, kwargs)) or SimpleNamespace(pid=5)
    )
    with platform.use(platform.backend_for(name)):
        _os.spawn(["x"], cwd=tmp_path, env={"A": "1"})
    ((argv, kwargs),) = seen
    assert argv == ["x"]
    assert kwargs["env"] == {"A": "1"}
    if name == "windows":
        assert kwargs["creationflags"] == 0x08000000 | 0x00000200  # no window, own process group
    else:
        assert kwargs["start_new_session"] is True


def test_a_pythonw_supervisor_gives_children_the_console_interpreter(monkeypatch, tmp_path):
    (tmp_path / "pythonw.exe").write_bytes(b"")
    (tmp_path / "python.exe").write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "pythonw.exe"))
    assert _os.child_python() == str(tmp_path / "python.exe")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python.exe"))
    assert _os.child_python() == str(tmp_path / "python.exe")


def test_busy_sees_the_supervisor_or_an_old_server_on_the_port(monkeypatch):
    from tow.supervisor import _os

    asked = []
    monkeypatch.setattr(layout, "running", lambda: {"pid": 1})
    monkeypatch.setattr(_os, "port_open", lambda port: asked.append(port) or False)
    assert layout.busy() is True
    assert asked == []  # the lock answers first
    monkeypatch.setattr(layout, "running", lambda: None)
    assert layout.busy() is False
    assert asked == [8787]
    monkeypatch.setattr(_os, "port_open", lambda port: True)  # a web server left on the port
    monkeypatch.setattr("tow.watchdog.healthy", lambda port, install=None: install == layout.install_id())
    assert layout.busy() is True


class _PortOwner:
    def __init__(self, command: str):
        self.command = command

    def port_owner(self, port):
        return {"pid": 50, "cmd": self.command, "parent": 49, "parent_cmd": ""}


def test_another_program_or_tow_folder_on_the_port_is_not_this_tow_running(monkeypatch, capsys):
    # 2.10.2026 (QA): a copy of the folder still running on the port made `tow setup` say
    # "TOW is running. Stop it first" - it was not, and stopping this TOW changed nothing.
    from tow import platform
    from tow.supervisor import _os

    monkeypatch.setattr(layout, "running", lambda: None)
    monkeypatch.setattr(_os, "port_open", lambda port: True)
    other_copy = []
    monkeypatch.setattr(
        "tow.watchdog.healthy", lambda port, install=None: other_copy.append(install) or install is None
    )
    with platform.use(_PortOwner('"C:\\Other TOW\\python.exe" -m tow serve --log-file C:\\Other TOW\\serve.log')):
        assert layout.port_holder(8787) == "other"
        assert layout.busy() is False
        assert layout.setup_check() == 0
    assert other_copy[0] == layout.install_id()  # asked as this install, answered as another
    out = capsys.readouterr().out
    assert "8787" in out
    assert "другой папкой TOW" in out  # the terminal language (the tests run in Russian)


def test_a_hung_web_server_of_this_install_still_counts_as_running(monkeypatch):
    from tow import platform
    from tow.supervisor import _os

    monkeypatch.setattr(layout, "running", lambda: None)
    monkeypatch.setattr(_os, "port_open", lambda port: True)
    monkeypatch.setattr("tow.watchdog.healthy", lambda port, install=None: False)  # no answer at all
    own = f'"python.exe" -m tow serve --log-file "{layout.logs_dir() / "serve.log"}" --parent-pid 1'
    with platform.use(_PortOwner(own)):
        assert layout.port_holder(8787) == "ours"
        assert layout.setup_check() == 4
    monkeypatch.setattr(_os, "port_open", lambda port: False)
    assert layout.port_holder(8787) is None
    assert layout.setup_check() == 0


def test_the_install_id_names_the_folder_without_showing_it(monkeypatch, tmp_path):
    first = layout.install_id()
    assert first == layout.install_id()  # stable
    assert len(first) == 16
    assert str(layout.install_root()) not in first
    monkeypatch.setattr(layout, "repo_root", lambda: tmp_path / "copy" / "app")
    assert layout.install_id() != first  # a copy of the folder is another install


# --- a web server ends with its supervisor (tow serve --parent-pid) ----------------------------


def test_the_server_stops_when_its_parent_is_gone():
    from tow.supervisor.parent import end_with_parent

    answers = iter([True, True, False])
    server = SimpleNamespace(should_exit=False)
    exits, sleeps = [], []
    thread = end_with_parent(
        4242,
        server,
        alive=lambda pid: pid == 4242 and next(answers),
        sleep=sleeps.append,
        exit_process=exits.append,
        grace=7.0,
    )
    thread.join(timeout=5)
    assert server.should_exit is True
    assert sleeps == [2.0, 2.0, 7.0]  # two polls while it lived, then the grace
    assert exits == [0]


def test_on_linux_and_macos_a_reused_parent_pid_does_not_hide_its_end(monkeypatch):
    # The parent ended and its pid went to a new process: "is 4242 alive" says yes for ever; the
    # server's own parent pid (re-parented to init / a subreaper) says it is gone.
    from tow.supervisor.parent import _parent_check

    monkeypatch.setattr("tow.platform.current", lambda: SimpleNamespace(process_alive=lambda pid: True))
    parent = {"pid": 4242}
    check = _parent_check(4242, parent=lambda: parent["pid"], windows=False)
    assert check(4242) is True
    parent["pid"] = 1  # re-parented: tow run has ended, whatever runs as 4242 now
    assert check(4242) is False
    # Started through something in between, or on Windows (the creator's pid stays): asked by pid.
    assert _parent_check(4242, parent=lambda: 777, windows=False)(4242) is True
    assert _parent_check(4242, parent=lambda: 4242, windows=True)(4242) is True


def test_serve_with_a_parent_watches_it(monkeypatch):
    import uvicorn

    from tow import cli
    from tow.supervisor import parent

    ran, watched = [], []

    class Server:
        def __init__(self, config):
            self.config = config

        def run(self):
            ran.append(self.config[1]["port"])

    monkeypatch.setattr(uvicorn, "Config", lambda app, **kwargs: (app, kwargs))
    monkeypatch.setattr(uvicorn, "Server", Server)
    monkeypatch.setattr(parent, "end_with_parent", lambda pid, server: watched.append((pid, type(server))))
    assert cli.main(["serve", "--port", "18999", "--parent-pid", "4242"]) == 0
    assert ran == [18999]
    assert watched == [(4242, Server)]
    assert cli.main(["serve", "--port", "18998"]) == 0  # by hand: nothing to watch
    assert watched == [(4242, Server)]
