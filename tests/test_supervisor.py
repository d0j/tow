"""`tow run` (tow.supervisor): the loop with a fake clock, fake processes and a fake web server."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import pairwise
from zoneinfo import ZoneInfo

import pytest

from tow.config import load_config
from tow.supervisor import layout
from tow.supervisor.core import BACKOFF_MAX_SEC, Deps, Supervisor
from tow.supervisor.schedule import Schedule, WakeDetector, local_slot

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC).timestamp()


class Clock:
    def __init__(self, wall: float = T0):
        self.wall = wall
        self.mono = 1000.0

    def time(self) -> float:
        return self.wall

    def monotonic(self) -> float:
        return self.mono

    def advance(self, seconds: float) -> None:
        self.wall += seconds
        self.mono += seconds

    def doze(self, seconds: float) -> None:
        """The machine sleeps: the wall clock moves, the monotonic one does not."""
        self.wall += seconds


@dataclass
class Child:
    pid: int
    argv: list[str]
    code: int | None = None
    at: float = 0.0

    def poll(self) -> int | None:
        return self.code


@dataclass
class World:
    clock: Clock
    healthy: bool = True
    port_busy: bool = False
    crash_on_start: bool = False
    facts: dict = field(default_factory=lambda: {"last_scheduled_check": T0, "last_backup_ok": T0})
    children: list[Child] = field(default_factory=list)
    stopped: list[int] = field(default_factory=list)
    sent: list[str] = field(default_factory=list)
    passes: list[float | None] = field(default_factory=list)
    markers: list[tuple[str, str]] = field(default_factory=list)

    def spawn(self, argv, _output, _append):
        crash = self.crash_on_start and "serve" in argv
        child = Child(pid=100 + len(self.children), argv=list(argv), code=1 if crash else None, at=self.clock.mono)
        self.children.append(child)
        return child

    def stop(self, child):
        self.stopped.append(child.pid)
        child.code = -9
        return True

    def deps(self) -> Deps:
        return Deps(
            spawn=self.spawn,
            stop=self.stop,
            healthy=lambda _port: self.healthy,
            port_open=lambda _port: self.port_busy,
            watchdog_pass=self.passes.append,
            send=lambda text: self.sent.append(text) or True,
            load_config=load_config,
            facts=lambda: dict(self.facts),
            now=self.clock.time,
            monotonic=self.clock.monotonic,
            sleep=self.clock.advance,
            in_thread=lambda target, _name: target(),
            restart_marker=lambda op, fields: self.markers.append((op, fields["status"])),
        )

    def servers(self) -> list[Child]:
        return [child for child in self.children if "serve" in child.argv]

    def jobs(self) -> list[Child]:
        return [child for child in self.children if "serve" not in child.argv]


def make(tmp_path, **kwargs):
    clock = Clock()
    world = World(clock=clock, **kwargs)
    return world, Supervisor(world.deps(), python="py", home=tmp_path / "logs")


def run_for(supervisor: Supervisor, clock: Clock, seconds: int) -> None:
    for _ in range(seconds):
        supervisor.tick()
        clock.advance(1)


# --- the web server ---------------------------------------------------------------------------


def test_the_web_server_is_started_and_watched(tmp_path):
    world, sup = make(tmp_path)
    run_for(sup, world.clock, 7)

    server = world.servers()[0]
    assert server.argv[:4] == ["py", "-m", "tow", "serve"]
    assert server.argv[4:6] == ["--log-file", str(tmp_path / "logs" / "serve.log")]
    assert server.argv[6:] == ["--parent-pid", str(os.getpid())]  # it ends with this supervisor
    assert sup.server.answered is True
    status = json.loads(layout.status_path().read_text(encoding="utf-8"))
    assert status["server"]["state"] == "up"
    assert status["server"]["pid"] == server.pid


def test_spawn_failure_does_not_expose_exception_text_in_status_or_log(tmp_path, caplog):
    world, sup = make(tmp_path)

    def fail_spawn(_argv, _output, _append):
        raise OSError("password=super-secret")

    sup.deps.spawn = fail_spawn
    sup._start_server(world.clock.wall, world.clock.mono)

    assert "super-secret" not in caplog.text
    assert "super-secret" not in sup.server.last_exit
    assert "OSError" in caplog.text


def test_a_server_that_keeps_exiting_is_restarted_with_a_growing_pause(tmp_path):
    world, sup = make(tmp_path, crash_on_start=True)
    run_for(sup, world.clock, 1500)

    starts = [child.at for child in world.servers()]
    gaps = [b - a for a, b in pairwise(starts)]
    # one tick to notice the exit, then 1 s, 2 s, 4 s ... up to five minutes
    assert gaps[:10] == [2, 3, 5, 9, 17, 33, 65, 129, 257, 301]
    assert set(gaps[9:]) == {1 + BACKOFF_MAX_SEC}
    assert world.sent == []  # it never answered: nothing to celebrate


def test_the_pause_starts_over_after_ten_quiet_minutes(tmp_path):
    world, sup = make(tmp_path)
    run_for(sup, world.clock, 5)
    for _ in range(3):
        world.servers()[-1].code = 1
        run_for(sup, world.clock, 20)
    assert sup.server.backoff == 8
    run_for(sup, world.clock, 700)
    assert sup.server.backoff == 1


def test_a_hung_server_is_stopped_restarted_and_the_owner_told(tmp_path):
    world, sup = make(tmp_path)
    run_for(sup, world.clock, 5)
    first = world.servers()[0]
    world.healthy = False
    run_for(sup, world.clock, 55)
    assert world.stopped == []  # five failed probes: maybe only busy
    run_for(sup, world.clock, 10)
    assert world.stopped == [first.pid]
    world.healthy = True
    run_for(sup, world.clock, 10)

    assert len(world.servers()) == 2
    assert world.sent == ["TOW не отвечал — выполнен перезапуск, TOW снова работает"]
    assert sup.server.last_exit == "hung"


def test_a_server_that_never_answers_gets_the_start_up_grace_only(tmp_path):
    world, sup = make(tmp_path, healthy=False)
    run_for(sup, world.clock, 85)
    assert world.stopped == []
    run_for(sup, world.clock, 10)
    assert world.stopped == [world.servers()[0].pid]


def test_repeated_restarts_are_told_once_per_window(tmp_path):
    world, sup = make(tmp_path)
    run_for(sup, world.clock, 5)
    for _ in range(4):
        world.servers()[-1].code = 1
        run_for(sup, world.clock, 60)
    assert world.sent[0] == "TOW был закрыт или аварийно завершился — выполнен перезапуск, TOW снова работает"
    assert len(world.sent) == 2
    assert world.sent[1].startswith("TOW: перезапусков за ")
    assert ": 3 — что-то раз за разом ломает TOW" in world.sent[1]


def test_a_crash_is_told_with_its_last_error(tmp_path):
    world, sup = make(tmp_path)
    sup.deps.crash_line = lambda _since: "OSError: disk full"
    run_for(sup, world.clock, 5)
    world.servers()[0].code = 1
    run_for(sup, world.clock, 10)
    assert world.sent == [
        "TOW аварийно завершился (последняя ошибка: OSError: disk full) — выполнен перезапуск, TOW снова работает"
    ]


def test_a_spawn_failure_is_retried(tmp_path):
    world, sup = make(tmp_path)
    calls = []

    def failing(argv, output, append):
        calls.append(argv)
        if len(calls) == 1:
            raise OSError("no python")
        return world.spawn(argv, output, append)

    sup.deps.spawn = failing
    run_for(sup, world.clock, 5)
    assert len(calls) == 2
    assert len(world.servers()) == 1


def _orphan(tmp_path, world, sup, *, cmd=None, owner_pid=78, parent=77):
    """A web server the previous supervisor (killed) left on the port: pid 77 in status.json."""
    layout.write_json(layout.status_path(), {"server": {"state": "up", "pid": 77}})
    log = tmp_path / "logs" / "serve.log"
    command = cmd or f'"C:\\TOW\\runtime\\python\\python.exe" -m tow serve --log-file "{log}" --parent-pid 5'
    sup.deps.port_owner = lambda _port: {"pid": owner_pid, "cmd": command, "parent": parent, "parent_cmd": "x"}
    stopped = []

    def stop_pid(pid):
        stopped.append(pid)
        world.port_busy = False
        return True

    sup.deps.stop_pid = stop_pid
    return stopped


def test_a_web_server_left_by_a_killed_supervisor_is_taken_over(tmp_path):
    world, sup = make(tmp_path, port_busy=True)
    stopped = _orphan(tmp_path, world, sup)

    assert sup.preflight() is None
    assert stopped == [77]  # the recorded pid (the venv launcher; the listener is its child)
    run_for(sup, world.clock, 5)
    assert len(world.servers()) == 1  # and this supervisor serves again


@pytest.mark.parametrize(
    ("cmd", "owner_pid", "parent"),
    [
        ("python -m http.server 8787", 78, 77),  # another program, whatever pid it has
        ("python -m tow serve --log-file D:\\Other\\data\\logs\\serve.log", 78, 77),  # another install
        (None, 90, 91),  # this install's command line, but not the server status.json names
    ],
)
def test_nothing_else_on_the_port_is_ever_stopped(tmp_path, cmd, owner_pid, parent):
    world, sup = make(tmp_path, port_busy=True)
    stopped = _orphan(tmp_path, world, sup, cmd=cmd, owner_pid=owner_pid, parent=parent)

    assert "8787" in (sup.preflight() or "")
    assert stopped == []


def test_an_orphan_whose_command_line_came_back_garbled_is_known_by_its_answer(tmp_path):
    # A Cyrillic install folder read back through the ANSI code page lost its letters: the
    # command line no longer named this install's log; the server's own answer still does.
    world, sup = make(tmp_path, port_busy=True)
    log = str(tmp_path / "Иван" / "serve.log")
    garbled = f'"python.exe" -m tow serve --log-file "{log.replace("Иван", "????")}" --parent-pid 5'
    stopped = _orphan(tmp_path, world, sup, cmd=garbled)
    asked = []
    sup.deps.own_server = lambda port: asked.append(port) or True
    assert sup.preflight() is None
    assert (stopped, asked) == ([77], [8787])


def test_a_copy_of_the_install_on_the_port_is_never_taken_over(tmp_path):
    # Its status.json was copied too (the same recorded pid), but it answers as another install
    # and its command line names its own log.
    world, sup = make(tmp_path, port_busy=True)
    stopped = _orphan(tmp_path, world, sup, cmd="python -m tow serve --log-file D:\\Copy\\data\\logs\\serve.log")
    sup.deps.own_server = lambda _port: False
    assert "8787" in (sup.preflight() or "")
    assert stopped == []


def test_an_orphan_that_does_not_let_the_port_go_is_a_refusal(tmp_path):
    world, sup = make(tmp_path, port_busy=True)
    _orphan(tmp_path, world, sup)
    sup.deps.stop_pid = lambda _pid: False  # it stays
    assert "8787" in (sup.preflight() or "")


def test_the_port_held_by_another_tow_is_a_clear_refusal(tmp_path):
    _world, sup = make(tmp_path, port_busy=True)
    message = sup.preflight()
    assert message is not None
    assert "8787" in message
    assert "другой программой" in message


@pytest.mark.parametrize("language", ["en", "ru"])
@pytest.mark.parametrize("cause", ["hung", "crashed", "crashed_no_detail"])
def test_recovery_message_does_not_claim_the_watchdog_restarted_tow(tmp_path, language, cause):
    world, sup = make(tmp_path)
    sup.deps.load_config = lambda: {"language": language}
    sup.server.pending_cause = "hung" if cause == "hung" else "crashed"
    sup.server.pending_crash = "synthetic failure" if cause == "crashed" else ""
    sup._count_restart(world.clock.wall)
    sup._tell_restarted(world.clock.wall)
    assert len(world.sent) == 1
    assert "watchdog" not in world.sent[0].casefold()
    assert "сторож" not in world.sent[0].casefold()
    assert "working again" in world.sent[0] if language == "en" else "снова работает" in world.sent[0]
    if cause == "crashed":
        assert "synthetic failure" in world.sent[0]


# --- scheduled jobs ---------------------------------------------------------------------------


def test_jobs_run_one_at_a_time_with_the_tasks_arguments(tmp_path):
    world, sup = make(tmp_path, facts={"last_scheduled_check": 0, "last_backup_ok": T0})
    run_for(sup, world.clock, 130)
    check = world.jobs()[0]
    assert check.argv == ["py", "-m", "tow", "check", "--apply", "--notify", "--global-only", "--json"]
    assert world.passes == [None]  # the watchdog duty at one minute, in-process

    run_for(sup, world.clock, 300)  # progress is due at five minutes, the check still runs
    assert len(world.jobs()) == 1
    check.code = 0
    run_for(sup, world.clock, 3)
    progress = world.jobs()[1]
    assert progress.argv == ["py", "-m", "tow", "check", "--apply", "--notify", "--progress-only", "--json"]
    assert sup.jobs_done["check"]["code"] == 0


def test_job_and_background_failures_do_not_expose_exception_text(tmp_path, caplog):
    world, sup = make(tmp_path)

    def fail(*_args):
        raise OSError("token=super-secret")

    sup.deps.spawn = fail
    sup._start_job("check", world.clock.wall, world.clock.mono)
    sup._background(fail, "test-duty")

    assert sup.jobs_done["check"]["error"] == "OSError"
    assert "super-secret" not in caplog.text


def test_a_job_that_runs_too_long_is_stopped(tmp_path, monkeypatch):
    from tow.supervisor import core

    monkeypatch.setitem(core.JOB_TIMEOUT_SEC, "check", 300.0)
    world, sup = make(tmp_path, facts={"last_scheduled_check": 0, "last_backup_ok": T0})
    run_for(sup, world.clock, 125)
    check = world.jobs()[0]
    run_for(sup, world.clock, 290)
    assert world.stopped == []
    run_for(sup, world.clock, 20)
    assert check.pid in world.stopped
    assert sup.jobs_done["check"]["stopped"] is True


def test_the_night_copy_catches_up_at_start_and_remembers_the_attempt(tmp_path):
    world, sup = make(tmp_path, facts={"last_scheduled_check": T0, "last_backup_ok": T0 - 30 * 3600})
    run_for(sup, world.clock, 3)
    backup = world.jobs()[0]
    assert backup.argv == ["py", "-m", "tow", "backup", "--json"]
    saved = layout.read_json(layout.run_dir() / "schedule.json")
    assert saved["backup_attempt_at"] == T0

    backup.code = 0
    world2, again = make(tmp_path, facts={"last_scheduled_check": T0, "last_backup_ok": T0 - 30 * 3600})
    run_for(again, world2.clock, 3)
    assert world2.jobs() == []  # a restarted supervisor does not copy again right away


class StateWorld(World):
    """Children that finish after 20 s and write health the way `tow check` does: every check
    moves at_ts, a scheduled one also auto_at_ts. The facts are the real ones
    (``tow.supervisor._facts``), over a state kept in memory (a week of ticks stays fast)."""

    def __init__(self, clock: Clock, health: dict, monkeypatch):
        super().__init__(clock=clock)
        self.health = health
        monkeypatch.setattr("tow.store.load_state", lambda: {"topics": [], "health": dict(self.health)})
        monkeypatch.setattr("tow.snapshots.status", lambda: {"last_ok_at": T0})

    def spawn(self, argv, output, append):
        child = super().spawn(argv, output, append)
        if "check" in argv:
            health = self.health
            health["at_ts"] = int(self.clock.wall)
            if "--progress-only" not in argv:
                health["auto_at_ts"] = int(self.clock.wall)
            self.health = health
        end = self.clock.wall + 20
        world = self

        class Finishing(type(child)):
            def poll(self):
                return 0 if world.clock.wall >= end else None

        return Finishing(**vars(child)) if "serve" not in argv else child

    def deps(self) -> Deps:
        from tow.supervisor import _facts

        deps = super().deps()
        deps.facts = _facts
        return deps

    def checks(self) -> list[Child]:
        return [child for child in self.jobs() if "check" in child.argv and "--progress-only" not in child.argv]


def _hourly() -> None:
    from tow.paths import config_path

    with config_path().open("a", encoding="utf-8") as handle:
        handle.write("interval_sec: 3600\n")


def test_a_week_of_hourly_checks_after_a_manual_one(tmp_path, monkeypatch):
    # 1.21: the cadence came from health.at_ts when there was no auto_at_ts yet; a manual check
    # first, then a progress pass every 30 minutes moved it, and no scheduled check ever ran.
    _hourly()
    clock = Clock()
    world = StateWorld(clock, {"at_ts": int(T0 - 10)}, monkeypatch)  # a manual check, no scheduled one yet
    sup = Supervisor(world.deps(), python="py", home=tmp_path / "logs")
    week = 7 * 24 * 3600
    for _ in range(week // 60):
        sup.tick()
        clock.advance(60)

    starts = [child.at - 1000.0 for child in world.checks()]  # monotonic, from the start
    assert len(starts) == 168
    assert starts[0] == 120  # two minutes after start
    # an hour apart (one tick later when the night copy was running at that moment)
    assert all(3600 <= b - a <= 3660 for a, b in pairwise(starts))
    assert len([child for child in world.jobs() if "--progress-only" in child.argv]) > 300


def test_the_cadence_survives_a_restart_and_ignores_manual_checks(tmp_path, monkeypatch):
    _hourly()
    clock = Clock()
    world = StateWorld(clock, {}, monkeypatch)
    sup = Supervisor(world.deps(), python="py", home=tmp_path / "logs")
    for _ in range(10):  # the first scheduled check at two minutes
        sup.tick()
        clock.advance(30)
    assert len(world.checks()) == 1
    first = clock.wall - 300 + 120
    assert layout.read_json(layout.schedule_path())["check_started_at"] == first

    world.health = {"at_ts": int(clock.wall)}  # a manual check; the state lost its auto_at_ts
    clock.advance(1200)
    again = StateWorld(clock, world.health, monkeypatch)
    restarted = Supervisor(again.deps(), python="py", home=tmp_path / "logs")
    for _ in range(int((first + 3600 - clock.wall) // 30) - 1):
        restarted.tick()
        clock.advance(30)
    assert again.checks() == []  # not two minutes after the restart, not an hour after the manual one
    for _ in range(3):
        restarted.tick()
        clock.advance(30)
    assert len(again.checks()) == 1
    assert again.checks()[0].at == 1000.0 + 120 + 3600  # an hour after the last scheduled start


def test_the_header_counts_down_to_the_real_next_check(tmp_path, monkeypatch):
    from tow.web.templating import header_health

    save_health = {"at_ts": int(T0), "auto_at_ts": int(T0 - 600)}
    from tow.store import save_state

    save_state({"topics": [], "health": save_health})
    interval = int(load_config()["interval_sec"])
    h = header_health()
    assert (h["next_at_ts"], h["next_from_ts"]) == (int(T0 - 600 + interval), int(T0 - 600))

    layout.write_json(layout.schedule_path(), {"check_started_at": T0 - 60})
    h = header_health()
    assert h["next_at_ts"] == int(T0 - 60 + interval)  # the supervisor's own start wins

    monkeypatch.setattr(layout, "running", lambda: {"pid": 1})
    layout.write_json(layout.status_path(), {"next": {"check": "2026-10-01T13:30:00+00:00"}})
    h = header_health()
    assert h["next_at_ts"] == int(datetime(2026, 10, 1, 13, 30, tzinfo=UTC).timestamp())
    assert h["next_from_ts"] == h["next_at_ts"] - interval

    monkeypatch.setattr(layout, "running", lambda: None)
    layout.schedule_path().unlink()
    save_state({"topics": [], "health": {"at_ts": int(T0)}})  # manual checks only
    h = header_health()
    assert (h["next_at_ts"], h["next_from_ts"]) == (0, 0)


# --- control requests -------------------------------------------------------------------------


def test_restart_from_settings_is_reported_on_the_marker(tmp_path):
    world, sup = make(tmp_path)
    run_for(sup, world.clock, 5)
    layout.request("restart", by="settings", operation_id="restart-abc")
    run_for(sup, world.clock, 5)

    assert world.stopped == [world.servers()[0].pid]
    assert len(world.servers()) == 2
    assert world.markers == [("restart-abc", "stopping"), ("restart-abc", "starting"), ("restart-abc", "ready")]
    assert world.sent == []  # a restart asked for is not an incident


def test_a_restart_whose_server_keeps_crashing_is_marked_failed(tmp_path):
    # 1.21: the marker stayed "starting" for ever while the new server crashed again and again.
    world, sup = make(tmp_path)
    run_for(sup, world.clock, 5)
    world.crash_on_start = True
    layout.request("restart", by="settings", operation_id="restart-bad")
    run_for(sup, world.clock, 30)

    statuses = [status for op, status in world.markers if op == "restart-bad"]
    assert statuses == ["stopping", "starting", "failed"]
    assert len([child for child in world.servers() if child.code == 1]) >= 3
    assert sup.server.operation is None
    world.crash_on_start = False
    run_for(sup, world.clock, 120)
    assert sup.server.answered is True  # still started again, as after any crash
    assert [status for _op, status in world.markers][-1] == "failed"  # the marker is not rewritten


def test_stop_waits_for_the_running_job_then_stops_everything(tmp_path):
    world, sup = make(tmp_path, facts={"last_scheduled_check": 0, "last_backup_ok": T0})
    run_for(sup, world.clock, 125)
    check = world.jobs()[0]
    layout.request("stop", by="update")
    run_for(sup, world.clock, 30)
    assert sup.finished is False
    assert world.stopped == []
    check.code = 0
    sup.tick()
    assert sup.finished is True
    assert world.stopped == [world.servers()[0].pid]
    assert not (layout.control_dir() / "stop").exists()


def test_stop_gives_up_on_a_job_after_ten_minutes(tmp_path):
    world, sup = make(tmp_path, facts={"last_scheduled_check": 0, "last_backup_ok": T0})
    run_for(sup, world.clock, 125)
    check = world.jobs()[0]
    layout.request("stop")
    run_for(sup, world.clock, 601)
    assert sup.finished is True
    assert check.pid in world.stopped


@pytest.mark.parametrize("operation", ["restart", "hang", "stop", "timeout", "stop_job"])
@pytest.mark.parametrize("response", ["refused", "ignored", "error"])
def test_unconfirmed_stops_keep_the_child_and_do_not_report_success(tmp_path, caplog, operation, response):
    world, sup = make(tmp_path)
    run_for(sup, world.clock, 5)
    server = world.servers()[0]
    attempts = []

    def no_stop(child):
        attempts.append(child.pid)
        if response == "error":
            raise OSError("secret=must-not-be-displayed")
        return response == "ignored"  # even True without child read-back is not proof

    sup.deps.stop = no_stop
    if operation == "restart":
        sup._restart_server({"by": "settings", "operation_id": "restart-unconfirmed"})
        assert world.markers == [("restart-unconfirmed", "stopping"), ("restart-unconfirmed", "failed")]
    elif operation == "hang":
        world.healthy = False
        run_for(sup, world.clock, 120)
        assert sup.restarts == []
    elif operation == "stop":
        sup.request_stop({"by": "test"})
        run_for(sup, world.clock, 5)
        assert not sup.finished
        assert len(attempts) == 1  # bounded retry, not every tick
    else:
        sup._start_job("check", world.clock.wall, world.clock.mono)
        job = sup.job
        if operation == "stop_job":
            sup.request_stop({"by": "test"})
            world.clock.advance(601)
        else:
            world.clock.advance(3601)
        sup.tick()
        assert sup.job is job
        assert "check" not in sup.jobs_done
        assert not sup.finished
        if operation == "stop_job":
            assert server.pid not in attempts
    assert sup.server.child is server
    assert server.poll() is None
    assert len(world.servers()) == 1
    assert "must-not-be-displayed" not in caplog.text
    assert "stop was not confirmed" in caplog.text or "OSError" in caplog.text
    # A later confirmed stop resumes the normal lifecycle without a second live child.
    sup.deps.stop = world.stop
    if operation == "restart":
        sup._restart_server({"by": "settings", "operation_id": "restart-retry"})
        assert len(world.servers()) == 2
    elif operation == "hang":
        run_for(sup, world.clock, 15)
        assert len(world.servers()) == 2
    else:
        run_for(sup, world.clock, 15)
        if operation in {"stop", "stop_job"}:
            assert sup.finished
        else:
            assert "check" in sup.jobs_done


def test_child_already_exited_is_not_sent_to_os_termination(tmp_path):
    world, sup = make(tmp_path)
    run_for(sup, world.clock, 5)
    child = world.servers()[0]
    child.code = 0
    assert sup.stop_child(child, "web server") is True
    assert world.stopped == []


@pytest.mark.parametrize("response", [True, False])
def test_confirmed_child_exit_wins_over_stop_command_result(tmp_path, response):
    world, sup = make(tmp_path)
    run_for(sup, world.clock, 5)
    child = world.servers()[0]

    def stopped(child):
        child.code = -9
        return response

    sup.deps.stop = stopped
    assert sup.stop_child(child, "web server") is True


@pytest.mark.parametrize("job_name", ["check", "progress", "backup"])
def test_unconfirmed_job_stop_retry_is_bounded_and_natural_exit_is_seen(tmp_path, job_name):
    world, sup = make(tmp_path)
    run_for(sup, world.clock, 5)
    sup._start_job(job_name, world.clock.wall, world.clock.mono)
    job = sup.job
    attempts = []
    sup.deps.stop = lambda child: attempts.append(child.pid) or False
    world.clock.advance(3601)
    run_for(sup, world.clock, 5)
    assert attempts == [job.child.pid]
    assert sup.job is job
    assert job_name not in sup.jobs_done
    # A natural exit between stop attempts is still recorded immediately.
    job.child.code = 0
    sup.tick()
    assert sup.job is None
    assert sup.jobs_done[job_name]["code"] == 0
    assert sup.jobs_done[job_name]["stopped"] is False


# --- sleep and wake ---------------------------------------------------------------------------


def test_a_wake_is_seen_and_handed_to_the_watchdog_duty(tmp_path):
    world, sup = make(tmp_path, facts={"last_scheduled_check": T0, "last_backup_ok": T0})
    run_for(sup, world.clock, 70)
    assert world.passes == [None]
    world.clock.doze(3 * 3600)
    woke = world.clock.wall
    run_for(sup, world.clock, 3)
    assert world.passes == [None, woke]
    assert sup.wake.woke_at == woke


def test_wake_detector_sees_wall_jumps_and_a_stalled_loop():
    detector = WakeDetector()
    assert detector.observe(T0, 10.0) is False
    assert detector.observe(T0 + 1, 11.0) is False
    assert detector.observe(T0 + 3600, 12.0) is True  # monotonic stood still (Linux, macOS)
    assert detector.slept_sec == pytest.approx(3598.0)
    assert detector.observe(T0 + 7300, 3712.0) is True  # both moved: the loop was frozen


# --- the schedule -----------------------------------------------------------------------------


def test_checks_follow_the_interval_and_a_clock_moved_back():
    schedule = Schedule(started_at=T0, interval_sec=3600)
    assert schedule.check_due_at(T0, 0) == T0 + 120
    assert schedule.check_due_at(T0, T0 - 600) == T0 + 3000
    schedule.started("check", T0 + 3000)
    assert schedule.check_due_at(T0 + 3001, T0 - 600) == T0 + 6600
    # the wall clock was set back an hour: the "last check" in the future counts as now
    assert schedule.check_due_at(T0 - 3600, T0) == T0


def test_supervisor_recovers_regular_jobs_after_backward_clock_correction(tmp_path):
    world, supervisor = make(tmp_path)
    # The service starts normally, then the date is corrected by a full day.
    for _ in range(62):
        for child in world.jobs():
            child.code = 0
        supervisor.tick()
        world.clock.advance(60)
    before = len(world.jobs())
    corrected_mono = world.clock.mono
    world.clock.wall -= 86400
    for _ in range(125):
        for child in world.jobs():
            child.code = 0
        supervisor.tick()
        world.clock.advance(60)
    new_jobs = world.jobs()[before:]
    checks = [child for child in new_jobs if "--apply" in child.argv]
    progress = [child for child in new_jobs if "--progress-only" in child.argv]
    assert len(checks) >= 2
    assert checks[0].at - corrected_mono <= 3660
    assert len(progress) >= 4
    assert world.passes


def test_the_night_copy_runs_once_per_local_day():
    zone = ZoneInfo("Asia/Jerusalem")
    day = datetime(2026, 10, 1, tzinfo=zone).date()
    slot = local_slot(day, (3, 30), zone)
    schedule = Schedule(started_at=slot - 3600, zone=zone)
    assert schedule.backup_due_at(slot - 60, last_ok=slot - 86000, last_attempt=0) == slot
    assert schedule.backup_due_at(slot + 1, last_ok=slot - 86000, last_attempt=0) == slot  # due
    schedule.started("backup", slot + 1)
    tomorrow = local_slot(day.replace(day=2), (3, 30), zone)
    assert schedule.backup_due_at(slot + 60, last_ok=slot + 30, last_attempt=slot + 1) == tomorrow


def test_a_missed_night_is_caught_up_and_a_failure_retried_later():
    zone = ZoneInfo("Asia/Jerusalem")
    now = datetime(2026, 10, 2, 9, 0, tzinfo=zone).timestamp()
    slot = datetime(2026, 10, 2, 3, 30, tzinfo=zone).timestamp()
    schedule = Schedule(started_at=now, zone=zone)
    assert schedule.backup_due_at(now, last_ok=now - 30 * 3600, last_attempt=0) == slot  # due: missed at 03:30
    schedule.started("backup", now)  # it failed
    later = now + 3600
    # the real retry time (status.json shows it), not tomorrow's slot
    assert schedule.backup_due_at(later, last_ok=now - 30 * 3600, last_attempt=now) == now + 6 * 3600
    assert schedule.next_due(later, last_backup_ok=now - 30 * 3600, last_backup_attempt=now)["backup"] == now + 6 * 3600


def test_a_retry_never_waits_past_the_next_slot():
    zone = ZoneInfo("Asia/Jerusalem")
    failed = datetime(2026, 10, 2, 23, 0, tzinfo=zone).timestamp()
    schedule = Schedule(started_at=failed, zone=zone, last={"backup": failed})
    tomorrow = datetime(2026, 10, 3, 3, 30, tzinfo=zone).timestamp()
    assert schedule.backup_due_at(failed + 60, last_ok=failed - 40 * 3600, last_attempt=failed) == tomorrow


def test_a_copy_younger_than_a_day_but_older_than_the_slot_is_made_again():
    # A copy by hand yesterday at 10:00; the PC was off at 03:30: caught up at 09:00 (23 h later).
    zone = ZoneInfo("Asia/Jerusalem")
    now = datetime(2026, 10, 2, 9, 0, tzinfo=zone).timestamp()
    by_hand = datetime(2026, 10, 1, 10, 0, tzinfo=zone).timestamp()
    schedule = Schedule(started_at=now, zone=zone)
    assert schedule.backup_due_at(now, last_ok=by_hand, last_attempt=by_hand) <= now


@pytest.mark.parametrize(
    ("at", "day", "expected_hours_apart", "local_time"),
    [
        # the nights of the spring-forward and fall-back switches in Berlin
        ((3, 30), (2026, 3, 28), 23, "03:30"),
        ((3, 30), (2026, 10, 24), 25, "03:30"),
        ((2, 30), (2026, 3, 28), 24, "03:30"),  # 02:30 does not exist that night: an hour later
        ((2, 30), (2026, 10, 24), 24, "02:30"),  # 02:30 happens twice: the first one only
    ],
)
def test_dst_neither_skips_nor_repeats_a_night(at, day, expected_hours_apart, local_time):
    from datetime import date, timedelta

    zone = ZoneInfo("Europe/Berlin")
    first = date(*day)
    slot = local_slot(first, at, zone)
    schedule = Schedule(started_at=slot, zone=zone, backup_at=at)
    schedule.started("backup", slot)
    following = schedule.backup_due_at(slot + 60, last_ok=slot, last_attempt=slot)
    assert following == local_slot(first + timedelta(days=1), at, zone)
    assert (following - slot) / 3600 == expected_hours_apart
    assert datetime.fromtimestamp(following, zone).strftime("%H:%M") == local_time
    schedule.started("backup", following)
    third = schedule.backup_due_at(following + 60, last_ok=following, last_attempt=following)
    assert datetime.fromtimestamp(third, zone).date() == first + timedelta(days=2)


def _copies_around(zone_name: str, switch: tuple[int, int, int], at: tuple[int, int]) -> list[datetime]:
    """Run the night copy schedule minute by minute from three days before a DST switch to three
    days after; every copy succeeds. The local times of the copies."""
    from datetime import timedelta

    zone = ZoneInfo(zone_name)
    start = datetime(*switch, 12, 0, tzinfo=zone) - timedelta(days=3)
    now = start.timestamp()
    schedule = Schedule(started_at=now, zone=zone, backup_at=at)
    last_ok = local_slot(start.date(), at, zone)  # the copy of the first day is made
    copies = []
    end = now + 6 * 86400
    while now < end:
        if schedule.backup_due_at(now, last_ok=last_ok, last_attempt=0) <= now:
            schedule.started("backup", now)
            last_ok = now
            copies.append(datetime.fromtimestamp(now, zone))
        now += 60
    return copies


@pytest.mark.parametrize(
    ("zone", "switch"),
    [
        ("Europe/Berlin", (2026, 3, 29)),  # 02:00 -> 03:00
        ("Europe/Berlin", (2026, 10, 25)),  # 03:00 -> 02:00
        ("Asia/Jerusalem", (2026, 3, 27)),  # 02:00 -> 03:00
        ("Asia/Jerusalem", (2026, 10, 25)),  # 02:00 -> 01:00
    ],
)
@pytest.mark.parametrize("at", [(3, 30), (2, 30), (1, 30), (0, 30)])
def test_one_night_copy_per_local_day_across_dst(zone, switch, at):
    # 1.21: the catch-up "older than 24 hours" fired an hour before the slot of the 25-hour
    # autumn day and the copy ran twice; now only a copy older than the latest slot is due.
    copies = _copies_around(zone, switch, at)
    days = [copy.date() for copy in copies]
    assert len(days) == len(set(days)) == 6
    for copy in copies:
        wall = (copy.hour, copy.minute)
        skipped = wall == (at[0] + 1, at[1])  # the spring switch skipped the slot: an hour later
        assert wall == at or (skipped and (copy.year, copy.month, copy.day) == switch), copy


def test_machine_zone_slots_come_from_the_c_library():
    from datetime import date

    slot = local_slot(date(2026, 10, 1), (3, 30), None)
    assert time.strftime("%H:%M", time.localtime(slot)) == "03:30"


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, (3, 30)), ("", (3, 30)), ("04:15", (4, 15)), (" 4:05 ", (4, 5)), (210, (3, 30))],
)
def test_backup_time_is_read_leniently(value, expected):
    from tow.config import parse_backup_time

    assert parse_backup_time(value) == expected


@pytest.mark.parametrize("value", ["25:00", "3h", "03:61", True])
def test_a_wrong_backup_time_is_named(value):
    from tow.config import ConfigError, parse_backup_time
    from tow.paths import config_path

    if value is True:
        assert parse_backup_time(value) == (3, 30)
        return
    with pytest.raises(ValueError, match="backup_time"):
        parse_backup_time(value)
    config_path().write_text(f'backup_time: "{value}"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="backup_time"):
        load_config()


def test_the_supervisor_reads_backup_time_and_interval_from_the_config(tmp_path):
    from tow.paths import config_path

    with config_path().open("a", encoding="utf-8") as handle:
        handle.write('backup_time: "05:10"\ninterval_sec: 7200\n')
    _world, sup = make(tmp_path)
    assert sup.schedule.backup_at == (5, 10)
    assert sup.schedule.interval_sec == 7200
