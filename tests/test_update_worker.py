from __future__ import annotations

import json
from pathlib import Path

import pytest
import test_update as fixture_module
from test_update import Fake, _load

from tow import update_worker

pytestmark = pytest.mark.allow_git
origin = fixture_module.origin
git_install = fixture_module.install


@pytest.mark.parametrize(
    ("scenario", "result"),
    [
        ({}, "ok"),
        ({"bad_target": True, "new_version_migrates": True}, "rolled_back"),
        ({"uv_fails_on": (1,)}, "rolled_back"),
    ],
)
def test_detached_runner_uses_real_update_and_rollback_on_throwaway_git(git_install, monkeypatch, scenario, result):
    updater = _load()

    class Machine(Fake):
        def __init__(self, app):
            super().__init__(app, **scenario)

    updater.System = Machine
    # These synthetic tagged projects intentionally contain no TOW modules. Schema
    # rejection is independently tested with real source strings in test_web_update.
    monkeypatch.setattr(update_worker, "schema_preflight", lambda *_args: None)
    path = git_install["root"] / "worker-job.json"
    job_id = "a" * 32
    path.write_text(json.dumps({"id": job_id, "status": "queued", "target": "1.21.0"}))
    phases = []
    real_write = update_worker.write_job

    def record(target, job):
        phases.append(job["status"])
        real_write(target, job)

    monkeypatch.setattr(update_worker, "write_job", record)
    code = update_worker.run(git_install["app"], path, job_id, "1.21.0", updater)
    assert code == (0 if result == "ok" else 1)
    assert json.loads(path.read_text())["status"] == result
    assert phases[0] == "preparing"
    assert "backup" in phases
    assert "installing" in phases
    assert "checking" in phases
    if result == "rolled_back":
        assert "rolling_back" in phases
    assert (git_install["data"] / "state.json").exists()


def test_worker_never_runs_a_job_that_was_replaced(tmp_path):
    path = tmp_path / "job.json"
    path.write_text(json.dumps({"id": "other", "status": "queued"}))
    assert update_worker.run(tmp_path, path, "requested", "1.22.21", None) == 2


def test_schema_missing_is_a_refusal_before_stop(tmp_path):
    from types import SimpleNamespace

    work = SimpleNamespace(
        code=SimpleNamespace(kind="git"),
        state={"target": "synthetic"},
        sys=SimpleNamespace(git=lambda *_args: "# no schema declaration\n"),
    )
    with pytest.raises(RuntimeError, match="cannot be verified"):
        update_worker.schema_preflight(work, RuntimeError)


def test_progress_failure_cannot_prevent_automatic_rollback(git_install, monkeypatch):
    updater = _load()

    class Machine(Fake):
        pass

    updater.System = Machine
    monkeypatch.setattr(update_worker, "schema_preflight", lambda *_args: None)
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
    monkeypatch.setattr(update_worker, "schema_preflight", lambda *_args: None)

    def fail_cleanup(_work):
        raise OSError("synthetic cleanup failure")

    monkeypatch.setattr(updater.Update, "prune", fail_cleanup)
    path = git_install["root"] / "worker-job.json"
    job_id = "c" * 32
    path.write_text(json.dumps({"id": job_id, "status": "queued", "target": "1.21.0"}))
    assert update_worker.run(git_install["app"], path, job_id, "1.21.0", updater) == 1
    assert json.loads(path.read_text())["status"] == "failed"
