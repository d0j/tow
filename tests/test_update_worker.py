from __future__ import annotations

import io
import json
import multiprocessing
import os
import queue
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_update as fixture_module
from helpers import reaped
from test_update import Fake, _load

from tow import update_worker
from tow.platform import locks

pytestmark = pytest.mark.allow_git
origin = fixture_module.origin
git_install = fixture_module.install


@pytest.mark.parametrize("encoding", ["cp1251", "cp1252", "ascii", "utf-8"])
def test_worker_main_writes_utf8_log_before_handoff(tmp_path, monkeypatch, encoding):
    job_id = "b" * 32
    script = tmp_path / job_id / "worker.py"
    path = tmp_path / "job.json"
    text = "Обновление: папка Тест — готово\n"
    buffers = [io.BytesIO(), io.BytesIO()]
    streams = [io.TextIOWrapper(buffer, encoding=encoding, newline="\n", write_through=True) for buffer in buffers]

    def handoff(*_args):
        print(text, end="")
        print(text, end="", file=sys.stderr)
        return 0

    monkeypatch.setattr(update_worker, "__file__", str(script))
    monkeypatch.setattr(update_worker, "handoff", handoff)
    monkeypatch.setattr(sys, "argv", [str(script), str(tmp_path / "app"), str(path), job_id, "1.23.3"])
    with monkeypatch.context() as stdio:
        stdio.setattr(sys, "stdout", streams[0])
        stdio.setattr(sys, "stderr", streams[1])
        assert update_worker.main() == 0
    for stream, buffer in zip(streams, buffers, strict=True):
        stream.flush()
        assert buffer.getvalue().decode("utf-8") == text
        assert stream.write_through


@pytest.mark.parametrize("stream", [None, io.StringIO()])
def test_worker_main_accepts_output_without_reconfigure(monkeypatch, stream):
    monkeypatch.setattr(sys, "argv", ["worker.py"])
    with monkeypatch.context() as stdio:
        stdio.setattr(sys, "stdout", stream)
        stdio.setattr(sys, "stderr", stream)
        assert update_worker.main() == 2


@pytest.mark.parametrize("error", [AttributeError, OSError, ValueError])
def test_worker_output_failure_does_not_skip_other_stream(monkeypatch, error):
    calls = []

    def unavailable(**_kwargs):
        calls.append("unavailable")
        raise error("diagnostic stream unavailable")

    def available(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(sys, "argv", ["worker.py"])
    with monkeypatch.context() as stdio:
        stdio.setattr(sys, "stdout", SimpleNamespace(reconfigure=unavailable))
        stdio.setattr(sys, "stderr", SimpleNamespace(reconfigure=available))
        assert update_worker.main() == 2
    assert calls == ["unavailable", {"encoding": "utf-8", "errors": "replace"}]


@pytest.mark.parametrize(
    ("scenario", "result"),
    [
        ({}, "ok"),
        ({"bad_target": True, "new_version_migrates": True}, "rolled_back"),
        ({"uv_fails_on": (1,)}, "rolled_back"),
    ],
)
@pytest.mark.parametrize("use_lease", [False, True])
def test_detached_runner_uses_real_update_and_rollback_on_throwaway_git(
    git_install, monkeypatch, scenario, result, use_lease
):
    updater = _load()

    class Machine(Fake):
        def __init__(self, app):
            super().__init__(app, **scenario)

    updater.System = Machine
    path = git_install["root"] / "worker-job.json"
    job_id = "a" * 32
    job = {"id": job_id, "status": "queued", "target": "1.21.0"}
    if use_lease:
        job["lease_version"] = 1
        _prepare_lease(path, job_id)
    path.write_text(json.dumps(job))
    phases = []
    real_write = update_worker.write_job

    def record(target, job):
        if use_lease:
            with (path.parent / job_id / "worker.lock").open("r+b") as handle:
                assert not locks.lock(handle, wait=False)
        phases.append(job["status"])
        real_write(target, job)

    monkeypatch.setattr(update_worker, "write_job", record)
    methods = dict(vars(updater.Update)), dict(vars(updater.System))
    code = update_worker.run(git_install["app"], path, job_id, "1.21.0", updater)
    assert (dict(vars(updater.Update)), dict(vars(updater.System))) == methods  # progress comes by callback
    assert code == (0 if result == "ok" else 1)
    assert json.loads(path.read_text())["status"] == result
    assert phases[0] == "preparing"
    assert "backup" in phases
    assert "installing" in phases
    assert ("checking" in phases) is ("uv_fails_on" not in scenario)  # a failed uv sync starts nothing new
    if result == "rolled_back":
        assert phases[-2:] == ["rolling_back", "rolled_back"]  # the whole rollback shows as one phase
    assert (git_install["data"] / "state.json").exists()
    if use_lease:
        with (path.parent / job_id / "worker.lock").open("r+b") as handle:
            assert locks.lock(handle, wait=False)
            locks.unlock(handle)


def _prepare_lease(path: Path, job_id: str) -> Path:
    folder = path.parent / job_id
    folder.mkdir()
    (folder / "locks.py").write_bytes(Path(locks.__file__).read_bytes())
    lease = folder / "worker.lock"
    lease.write_bytes(b"\0")
    return lease


def _leased_child(path: str, job_id: str, ready, finish, abrupt: bool) -> None:
    with update_worker.worker_lease(Path(path), {"id": job_id, "lease_version": 1}) as acquired:
        ready.put(acquired)
        try:
            finish.get(timeout=120)
        except queue.Empty:
            raise RuntimeError("test lease holder was not released") from None
        if abrupt:
            os._exit(17)


@pytest.mark.parametrize("abrupt", [False, True])
def test_os_releases_the_worker_lease_after_normal_or_abrupt_process_exit(tmp_path, abrupt):
    path = tmp_path / "job.json"
    job_id = "d" * 32
    lease = _prepare_lease(path, job_id)
    context = multiprocessing.get_context("spawn")
    # Queues, not a multiprocessing Event: Event.set() waits for every waiter to wake, and
    # a child killed while waiting never does - the test then hung for good. A put never waits.
    ready, finish = context.Queue(), context.Queue()
    process = context.Process(target=_leased_child, args=(str(path), job_id, ready, finish, abrupt))
    try:
        with reaped(process):
            process.start()
            assert ready.get(timeout=120) is True  # generous: spawning is slow under a parallel suite
            with lease.open("r+b") as handle:
                assert not locks.lock(handle, wait=False)
            finish.put(True)
            process.join(120)
            assert process.exitcode == (17 if abrupt else 0)
            with lease.open("r+b") as handle:
                assert locks.lock(handle, wait=False)
                locks.unlock(handle)
    finally:
        finish.put(True)
        for channel in (ready, finish):
            channel.close()
            channel.join_thread()


@pytest.mark.parametrize("damage", ["missing", "empty", "module", "syntax", "version"])
def test_worker_refuses_an_unreadable_lease_before_running_the_updater(tmp_path, damage):
    path = tmp_path / "job.json"
    job_id = "e" * 32
    lease = _prepare_lease(path, job_id)
    job = {"id": job_id, "status": "queued", "lease_version": 1}
    if damage == "missing":
        lease.unlink()
    elif damage == "empty":
        lease.write_bytes(b"")
    elif damage == "module":
        (lease.parent / "locks.py").unlink()
    elif damage == "syntax":
        (lease.parent / "locks.py").write_text("def broken(:\n")
    else:
        job["lease_version"] = 2
    path.write_text(json.dumps(job))
    before = path.read_bytes()
    assert update_worker.run(tmp_path, path, job_id, "1.22.21", None) == 2
    # An unreadable lease is not ownership, even to publish a refusal.
    assert path.read_bytes() == before


def test_second_worker_cannot_take_over_a_held_job_lease(tmp_path):
    path = tmp_path / "job.json"
    job_id = "f" * 32
    lease = _prepare_lease(path, job_id)
    path.write_text(json.dumps({"id": job_id, "status": "queued", "lease_version": 1}))
    before = path.read_bytes()
    with lease.open("r+b") as handle:
        assert locks.lock(handle, wait=False)
        try:
            assert update_worker.run(tmp_path, path, job_id, "1.22.21", None) == 2
            assert path.read_bytes() == before
        finally:
            locks.unlock(handle)


def test_worker_never_runs_a_job_that_was_replaced(tmp_path):
    path = tmp_path / "job.json"
    path.write_text(json.dumps({"id": "other", "status": "queued"}))
    assert update_worker.run(tmp_path, path, "requested", "1.22.21", None) == 2


def test_a_target_that_cannot_read_the_data_is_refused_before_stop(git_install, monkeypatch):
    # The check ran on the first stop(), which can be a recovery's: there it failed with an
    # OSError or a KeyError and the job showed a generic "failed". update.py now refuses it.
    updater = _load()

    class Machine(Fake):
        pass

    updater.System = Machine
    (git_install["data"] / "state.json").write_text('{"schema_version": 5}', encoding="utf-8")
    path = git_install["root"] / "worker-job.json"
    job_id = "e" * 32
    path.write_text(json.dumps({"id": job_id, "status": "queued", "target": "1.21.0"}))
    assert update_worker.run(git_install["app"], path, job_id, "1.21.0", updater) == 2
    assert json.loads(path.read_text())["status"] == "refused"
    assert not (git_install["root"] / "update-state.json").exists()  # nothing was stopped


def test_progress_failure_cannot_prevent_automatic_rollback(git_install, monkeypatch):
    updater = _load()

    class Machine(Fake):
        pass

    updater.System = Machine
    path = git_install["root"] / "worker-job.json"
    job_id = "b" * 32
    path.write_text(json.dumps({"id": job_id, "status": "queued", "target": "1.21.0"}))
    real_write = update_worker.write_job

    def fail_install_progress(target: Path, job):
        if job["status"] in {"installing", "rolling_back"}:
            raise OSError("synthetic full disk")
        real_write(target, job)

    monkeypatch.setattr(update_worker, "write_job", fail_install_progress)
    assert update_worker.run(git_install["app"], path, job_id, "1.21.0", updater) == 1
    assert json.loads(path.read_text())["status"] == "rolled_back"


def test_failure_after_health_check_is_not_reported_as_success(git_install, monkeypatch):
    updater = _load()

    class Machine(Fake):
        pass

    updater.System = Machine

    def fail_cleanup(_work):
        raise OSError("synthetic cleanup failure")

    monkeypatch.setattr(updater.Update, "prune", fail_cleanup)
    path = git_install["root"] / "worker-job.json"
    job_id = "c" * 32
    path.write_text(json.dumps({"id": job_id, "status": "queued", "target": "1.21.0"}))
    assert update_worker.run(git_install["app"], path, job_id, "1.21.0", updater) == 1
    assert json.loads(path.read_text())["status"] == "failed"


def test_handoff_failure_is_terminal_and_does_not_replace_another_job(tmp_path):
    path = tmp_path / "job.json"
    path.write_text(json.dumps({"id": "owned", "status": "queued"}))
    assert update_worker.refuse_handoff(path, "other") == 2
    assert json.loads(path.read_text())["status"] == "queued"
    assert update_worker.refuse_handoff(path, "owned") == 2
    assert json.loads(path.read_text())["status"] == "failed"
    assert json.loads(path.read_text())["error"] == "releases.launch_failed"


def test_handoff_failure_does_not_overwrite_a_running_job(tmp_path):
    path = tmp_path / "job.json"
    path.write_text(json.dumps({"id": "owned", "status": "preparing"}))
    update_worker.refuse_handoff(path, "owned")
    assert json.loads(path.read_text())["status"] == "preparing"


@pytest.mark.parametrize(("mode", "waited"), [("--handoff", True), ("--after-parent", True), ("--after-parent", False)])
def test_main_handoff_waits_before_running_or_refuses(tmp_path, monkeypatch, mode, waited):
    path = tmp_path / "job.json"
    path.write_text(json.dumps({"id": "owned", "status": "queued"}))
    monkeypatch.setattr(update_worker, "__file__", str(tmp_path / "owned" / "worker.py"))
    calls = []
    machine = SimpleNamespace(
        spawn_handoff=lambda args, log: calls.append(("spawn", args, log)),
        wait_process_exit=lambda pid, timeout: calls.append(("wait", pid, timeout)) or waited,
        updater_independent=lambda: True,
    )
    module = SimpleNamespace(System=lambda app: machine)
    loader = SimpleNamespace(exec_module=lambda _: None)
    spec = SimpleNamespace(name="isolated_handoff", loader=loader)
    monkeypatch.setattr(update_worker.importlib.util, "spec_from_file_location", lambda *args: spec)
    monkeypatch.setattr(update_worker.importlib.util, "module_from_spec", lambda _: module)
    monkeypatch.setattr(update_worker, "run", lambda *args: calls.append(("run",)) or 0)
    arguments = [mode, *(["42"] if mode == "--after-parent" else []), str(tmp_path), str(path), "owned", "1.22.21"]
    monkeypatch.setattr(update_worker.sys, "argv", ["worker.py", *arguments])
    assert update_worker.main() == (0 if waited else 2)
    if mode == "--handoff":
        assert [entry[0] for entry in calls] == ["spawn"]
        assert "--after-parent" in calls[0][1]
    else:
        assert calls[0] == ("wait", 42, 10.0)
        assert [entry[0] for entry in calls] == (["wait", "run"] if waited else ["wait"])
        assert json.loads(path.read_text())["status"] == ("queued" if waited else "failed")


def test_main_launch_failure_preserves_a_terminal_result(tmp_path, monkeypatch):
    path = tmp_path / "job.json"
    path.write_text(json.dumps({"id": "owned", "status": "queued"}))
    monkeypatch.setattr(update_worker, "__file__", str(tmp_path / "owned" / "worker.py"))

    def fail(*_args):
        raise OSError("synthetic launch failure")

    module = SimpleNamespace(System=lambda _: SimpleNamespace(spawn_handoff=fail, updater_independent=lambda: True))
    spec = SimpleNamespace(name="isolated_handoff_failure", loader=SimpleNamespace(exec_module=lambda _: None))
    monkeypatch.setattr(update_worker.importlib.util, "spec_from_file_location", lambda *args: spec)
    monkeypatch.setattr(update_worker.importlib.util, "module_from_spec", lambda _: module)
    monkeypatch.setattr(
        update_worker.sys, "argv", ["worker.py", "--handoff", str(tmp_path), str(path), "owned", "1.22.21"]
    )
    assert update_worker.main() == 2
    assert json.loads(path.read_text())["error"] == "releases.launch_failed"


@pytest.mark.parametrize(
    ("handle", "error", "wait", "expected"), [(12, 0, 0, True), (12, 0, 258, False), (0, 87, 0, True), (0, 5, 0, False)]
)
def test_windows_parent_wait_checks_exit_and_closes_handle(monkeypatch, handle, error, wait, expected):
    import ctypes

    module = _load()
    machine = object.__new__(module.System)
    machine.windows = True
    calls = []

    def open_process(access, inherit, pid):
        calls.append(("open", access, inherit, pid))
        return handle

    def await_exit(value, timeout):
        calls.append(("wait", value, timeout))
        return wait

    def close(value):
        calls.append(("close", value))

    kernel = SimpleNamespace(OpenProcess=open_process, WaitForSingleObject=await_exit, CloseHandle=close)
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: kernel, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: error, raising=False)
    assert machine.wait_process_exit(42, 10) is expected
    assert calls[0] == ("open", 0x00100000, False, 42)
    assert calls[1:] == ([("wait", 12, 10000), ("close", 12)] if handle else [])


def test_posix_parent_wait_never_signals_or_accepts_uncertain_access(monkeypatch):
    module = _load()
    machine = object.__new__(module.System)
    machine.windows = False
    calls = []

    def missing(pid, sig):
        calls.append((pid, sig))
        raise ProcessLookupError

    monkeypatch.setattr(module.os, "kill", missing)
    assert machine.wait_process_exit(42, 1)
    assert calls == [(42, 0)]

    def denied(*_args):
        raise PermissionError

    monkeypatch.setattr(module.os, "kill", denied)
    assert not machine.wait_process_exit(42, 1)
    assert not machine.wait_process_exit(0, 1)


@pytest.mark.parametrize("windows", [True, False])
def test_handoff_spawn_is_hidden_logged_and_outside_replaceable_cwd(tmp_path, monkeypatch, windows):
    module = _load()
    machine = object.__new__(module.System)
    machine.windows = windows
    machine.root = tmp_path
    calls = []
    monkeypatch.setattr(module.subprocess, "Popen", lambda args, **options: calls.append((args, options)))
    machine.spawn_handoff(["synthetic-python", "worker.py"], tmp_path / "update.log")
    args, options = calls[0]
    assert args == ["synthetic-python", "worker.py"]
    assert options["cwd"] == str(tmp_path)
    assert options["close_fds"] is True
    assert options["start_new_session"] is (not windows)
    assert options["creationflags"] == module.NO_WINDOW
    assert options["stdout"] is options["stderr"]


@pytest.mark.parametrize(
    ("windows", "success", "in_job", "expected"),
    [(False, 0, 1, True), (True, 1, 0, True), (True, 1, 1, False), (True, 0, 0, False)],
)
def test_outer_job_is_a_safe_refusal_before_stopping(monkeypatch, windows, success, in_job, expected):
    import ctypes

    module = _load()
    machine = object.__new__(module.System)
    machine.windows = windows

    def current():
        return 12

    def member(process, job, output):
        assert process == 12
        assert job is None
        ctypes.cast(output, ctypes.POINTER(ctypes.c_int)).contents.value = in_job
        return success

    kernel = SimpleNamespace(GetCurrentProcess=current, IsProcessInJob=member)
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: kernel, raising=False)
    assert machine.updater_independent() is expected
