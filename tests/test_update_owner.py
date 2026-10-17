"""The update's own process (X41): a terminal update no longer runs in the terminal's process.

``main`` copies the updater to <TOW>/runtime/update-run/<id>/ and starts that copy as the owner of
the whole update (lock, snapshot, switch record, rollback, result), detached from the console, its
process group and job; the terminal only shows the owner's log and ends with its exit code. The
real console-close, parent-kill, concurrency and job runs are in the release verification
(Windows); here the processes are faked.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_update import SCRIPT, updater


def _install(tmp_path: Path) -> Path:
    app = tmp_path / "TOW" / "app"
    (app / "scripts").mkdir(parents=True)
    (app / "pyproject.toml").write_text('[project]\nversion = "1.33.1"\n', encoding="utf-8")
    return app


class Starter:
    """A System whose spawn_owner plays the owner: it writes the log and the result at once."""

    def __init__(self, app: Path, *, log: str = "", result: dict[str, Any] | None = None, exit_code: int | None = 0):
        self.app = app
        self.root = app.parent
        self.log, self.result, self.code = log, result, exit_code
        self.argv: list[str] = []

    def spawn_owner(self, argv: list[str]) -> Any:
        self.argv = argv
        folder = Path(argv[3]).parent
        (folder / updater.OWNER_LOG).write_text(self.log, encoding="utf-8", newline="\n")  # as run_owner writes it
        if self.result is not None:
            (folder / updater.OWNER_RESULT).write_text(json.dumps(self.result), encoding="utf-8")
        return SimpleNamespace(exit_code=lambda: self.code)


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch):
    monkeypatch.setattr(updater, "OWNER_POLL", 0.0)
    monkeypatch.delenv("TOW_UPDATE_HERE", raising=False)


def test_the_owner_is_a_copy_outside_app_started_with_the_same_arguments(tmp_path, capsys):
    app = _install(tmp_path)
    starter = Starter(app, log="TOW update: v1.33.0 → v1.33.1\nновая версия отвечает\n", result={"code": 0})

    code = updater.start_owner(["--ref", "v1.33.1", "--keep", "2"], starter)  # type: ignore[arg-type]

    assert code == 0
    python, isolated, unbuffered, copy, *rest = starter.argv
    assert (isolated, unbuffered) == ("-I", "-u")
    assert Path(python).is_file()
    # outside the app/ that the update replaces, as a copy of this very file
    assert Path(copy).parent.parent == app.parent / "runtime" / "update-run"
    assert Path(copy).read_bytes() == SCRIPT.read_bytes()
    assert rest == ["--owner", "--app", str(app), "--ref", "v1.33.1", "--keep", "2"]
    out = capsys.readouterr().out
    assert "closing this window no longer stops it" in out
    assert out.endswith("TOW update: v1.33.0 → v1.33.1\nновая версия отвечает\n")  # the owner's log, shown


@pytest.mark.parametrize(
    ("result", "exit_code", "expected"), [({"code": 1}, 1, 1), ({"code": 2}, 2, 2), (None, 97, 97)]
)
def test_the_terminal_ends_with_the_owners_exit_code(tmp_path, result, exit_code, expected):
    # An owner cut off with no result (a power cut, the test cut of the switch): its own exit code.
    starter = Starter(_install(tmp_path), result=result, exit_code=exit_code)
    assert updater.start_owner(["--ref", "v1.33.1"], starter) == expected  # type: ignore[arg-type]


def test_an_owner_in_a_job_it_cannot_leave_hands_the_update_back(tmp_path, monkeypatch, capsys):
    app = _install(tmp_path)
    starter = Starter(app, result={"code": updater.OWNER_IN_JOB, "in_job": True}, exit_code=updater.OWNER_IN_JOB)
    assert updater.start_owner(["--ref", "v1.33.1"], starter) is None  # type: ignore[arg-type]
    assert "it runs in this window" in capsys.readouterr().out

    # main() then runs the update in its own process, as before 1.33.1.
    ran = []
    monkeypatch.setattr(updater, "System", lambda *_args, **_kwargs: starter)
    monkeypatch.setattr(updater, "update", lambda ref, **_options: ran.append(ref) or 0)
    assert updater.main(["--ref", "v1.33.1"]) == 0
    assert ran == ["v1.33.1"]


def test_an_owner_that_cannot_start_hands_the_update_back(tmp_path, capsys):
    starter = Starter(_install(tmp_path))
    starter.spawn_owner = lambda _argv: (_ for _ in ()).throw(OSError("access denied"))  # type: ignore[method-assign]
    assert updater.start_owner(["--ref", "v1.33.1"], starter) is None  # type: ignore[arg-type]
    assert "could not start in its own process (access denied)" in capsys.readouterr().out


def test_ctrl_c_leaves_the_owner_running(tmp_path, monkeypatch, capsys):
    starter = Starter(_install(tmp_path), result=None, exit_code=None)

    def interrupted(_seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(updater.time, "sleep", interrupted)
    assert updater.start_owner(["--ref", "v1.33.1"], starter) == 1  # type: ignore[arg-type]
    assert "it goes on by itself" in capsys.readouterr().out


def test_only_finished_owners_folders_beyond_the_newest_are_removed(tmp_path):
    runs = tmp_path / "update-run"
    for name in ("20261001-1", "20261002-1", "20261003-1", "20261004-1", "20261005-1"):
        (runs / name).mkdir(parents=True)
        (runs / name / updater.OWNER_RESULT).write_text("{}", encoding="utf-8")
    (runs / "20260901-running").mkdir()  # no result: may still run
    current = runs / "20261006-1"
    current.mkdir()
    updater._prune_runs(runs, current)
    assert sorted(path.name for path in runs.iterdir()) == [
        "20260901-running",
        "20261003-1",
        "20261004-1",
        "20261005-1",
        "20261006-1",
    ]


def _copy_in_runs(tmp_path: Path) -> tuple[Any, Path, Path]:
    app = _install(tmp_path)
    folder = app.parent / "runtime" / "update-run" / "20261017-000000-abcd"
    folder.mkdir(parents=True)
    shutil.copyfile(SCRIPT, folder / "update.py")
    spec = importlib.util.spec_from_file_location("tow_update_owner_copy", folder / "update.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, app, folder


def _owner_args(module: Any, app: Path, *extra: str) -> Any:
    parser_args = ["--ref", "v1.33.1", "--owner", "--app", str(app), *extra]
    captured = {}

    def run_owner(args: Any) -> int:
        captured["args"] = args
        return 0

    original = module.run_owner
    module.run_owner = run_owner
    try:
        module.main(parser_args)
    finally:
        module.run_owner = original
    return captured["args"]


def test_the_owner_runs_the_update_and_records_its_result(tmp_path, monkeypatch):
    module, app, folder = _copy_in_runs(tmp_path)
    args = _owner_args(module, app)
    calls = []
    monkeypatch.setattr(module, "System", lambda app, uv=None: SimpleNamespace(updater_independent=lambda: True))
    monkeypatch.setattr(module, "update", lambda ref, **options: calls.append((ref, options["app"])) or 1)
    stdout, stderr = sys.stdout, sys.stderr
    try:
        assert module.run_owner(args) == 1
    finally:
        sys.stdout, sys.stderr = stdout, stderr
    assert calls == [("v1.33.1", app)]
    assert json.loads((folder / module.OWNER_RESULT).read_text(encoding="utf-8")) == {"code": 1}
    assert (folder / module.OWNER_LOG).is_file()


def test_an_owner_in_a_job_changes_nothing(tmp_path, monkeypatch):
    module, app, folder = _copy_in_runs(tmp_path)
    args = _owner_args(module, app)
    monkeypatch.setattr(module, "System", lambda app, uv=None: SimpleNamespace(updater_independent=lambda: False))
    monkeypatch.setattr(module, "update", lambda *_args, **_kwargs: pytest.fail("no update in a job"))
    stdout, stderr = sys.stdout, sys.stderr
    try:
        assert module.run_owner(args) == module.OWNER_IN_JOB
    finally:
        sys.stdout, sys.stderr = stdout, stderr
    assert json.loads((folder / module.OWNER_RESULT).read_text(encoding="utf-8"))["in_job"] is True


def test_the_owner_runs_only_as_the_copy_of_that_install(tmp_path, monkeypatch, capsys):
    module, app, _folder = _copy_in_runs(tmp_path)
    other = tmp_path / "other" / "app"
    other.mkdir(parents=True)
    args = _owner_args(module, other)
    monkeypatch.setattr(module, "update", lambda *_args, **_kwargs: pytest.fail("not this install's copy"))
    assert module.run_owner(args) == 2
    # and never the file in app/scripts itself
    assert updater.run_owner(_owner_args(updater, app)) == 2
    assert "runs only as the copy" in capsys.readouterr().out


def test_the_windows_start_flags_are_the_documented_values_on_every_system():
    # Before: the test read subprocess.DETACHED_PROCESS, which exists only on Windows, so the
    # Windows branch of spawn_owner could not be checked on Linux and macOS.
    assert updater.DETACHED_PROCESS == 0x00000008
    assert updater.NEW_PROCESS_GROUP == 0x00000200
    assert updater.BREAKAWAY_FROM_JOB == 0x01000000
    for name, value in (("DETACHED_PROCESS", 0x00000008), ("CREATE_NEW_PROCESS_GROUP", 0x00000200)):
        assert getattr(subprocess, name, value) == value


@pytest.mark.parametrize(
    ("in_job", "denied", "how"),
    [(False, False, "detached"), (True, True, "broker"), (False, True, "refused")],
)
def test_windows_starts_the_owner_detached_or_through_the_broker(tmp_path, monkeypatch, in_job, denied, how):
    app = _install(tmp_path)
    machine = updater.System(app)
    machine.windows = True
    started: list[tuple[str, Any]] = []

    class Popen:
        def __init__(self, argv: list[str], **options: Any) -> None:
            if denied and options["creationflags"] & updater.BREAKAWAY_FROM_JOB:
                raise PermissionError(5, "Access is denied")
            started.append(("detached", options))
            self.pid = 4321

        def poll(self) -> int | None:
            return None

    monkeypatch.setattr(updater.subprocess, "Popen", Popen)
    monkeypatch.setattr(machine, "updater_independent", lambda: not in_job)
    monkeypatch.setattr(machine, "spawn_broker", lambda argv: started.append(("broker", argv)) or 9876)
    if how == "refused":
        with pytest.raises(OSError, match="denied"):
            machine.spawn_owner(["python", "update.py"])
        return
    owned = machine.spawn_owner(["python", "update.py"])
    assert [kind for kind, _ in started] == [how]
    if how == "detached":
        flags = started[0][1]["creationflags"]
        assert flags & updater.DETACHED_PROCESS
        assert flags & updater.NEW_PROCESS_GROUP
        assert flags & updater.BREAKAWAY_FROM_JOB
        assert started[0][1]["stdin"] is subprocess.DEVNULL
        assert owned.pid == 4321
    else:
        assert owned.pid == 9876
        monkeypatch.setattr(machine, "wait_process_exit", lambda pid, timeout: pid == 9876 and timeout == 0.0)
        assert owned.exit_code() == 1  # gone; a broker's child cannot report its code


def test_posix_starts_the_owner_in_its_own_session(tmp_path, monkeypatch):
    machine = updater.System(_install(tmp_path))
    machine.windows = False
    seen = {}

    class Popen:
        def __init__(self, argv: list[str], **options: Any) -> None:
            seen.update(options)
            self.pid = 1

    monkeypatch.setattr(updater.subprocess, "Popen", Popen)
    machine.spawn_owner(["python3", "update.py"])
    assert seen["start_new_session"] is True  # a hang-up of the terminal does not reach it
    assert seen["stdin"] is subprocess.DEVNULL


def test_the_web_worker_still_runs_the_update_in_its_own_process():
    # The web update's worker is already detached and independent: no second owner beneath it.
    worker = (SCRIPT.parents[1] / "src" / "tow" / "update_worker.py").read_text(encoding="utf-8")
    assert "updater.update(" in worker
    assert "updater.main(" not in worker
    assert "start_owner" not in worker
