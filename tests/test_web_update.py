from __future__ import annotations

import ast
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from tow import update_worker, web_update
from tow.restore_points import RestorePointError
from tow.store import save_secrets, save_state
from tow.web import app, services


@pytest.fixture
def install(monkeypatch, tmp_path):
    runtime = tmp_path / "portable"
    code = runtime / "app"
    (code / "scripts").mkdir(parents=True)
    (code / "scripts" / "update.py").write_text("# isolated updater fixture\n", encoding="utf-8")
    monkeypatch.setattr(web_update, "repo_root", lambda: code)
    monkeypatch.setattr(web_update, "root", lambda: runtime)
    monkeypatch.setattr(web_update, "runtime_dir", lambda: runtime / "runtime")
    monkeypatch.setattr(web_update, "__version__", "1.22.20")
    monkeypatch.setattr(web_update.releases, "published_version", lambda version: version)
    monkeypatch.setattr(web_update, "create_restore_point", lambda: {"id": "synthetic-copy"})
    calls = []
    backend = SimpleNamespace(
        name="windows",
        process_alive=lambda pid: pid == 123,
        spawn_detached=lambda args, **kwargs: calls.append((args, kwargs)) or 123,
    )
    monkeypatch.setattr(web_update.platform, "current", lambda: backend)
    return runtime, calls, backend


def test_start_reserves_job_and_copies_worker_outside_replaceable_app(install):
    runtime, calls, _backend = install
    result = web_update.start("v1.22.21")
    assert result["ok"] is True
    job = json.loads((runtime / "runtime" / "web-update" / "job.json").read_text())
    assert job["status"] == "queued"
    assert job["safety_point"] == "synthetic-copy"
    assert len(calls) == 1
    argv, options = calls[0]
    assert Path(argv[0]).is_file()
    assert not Path(argv[0]).is_relative_to(runtime / "app" / ".venv")
    assert argv[1] == "-u"
    assert Path(argv[2]).is_relative_to(runtime / "runtime")
    assert Path(argv[2]).is_file()
    assert options["require_breakaway"] is True
    assert options["hidden"] is True
    assert argv[-1] == "1.22.21"


def test_parallel_click_is_refused_without_a_second_archive(install, monkeypatch):
    count = []
    monkeypatch.setattr(web_update, "create_restore_point", lambda: count.append(1) or {"id": "copy"})
    web_update.start("1.22.21")
    with pytest.raises(web_update.WebUpdateError, match=r"releases\.busy"):
        web_update.start("1.22.21")
    assert count == [1]
    assert len(install[1]) == 1


@pytest.mark.parametrize(
    "version",
    [
        "main",
        "latest",
        "abcdef",
        "1.22.19",
        "1.22.21-rc.1",
        "1.22.21;exit",
        "../x",
        "",
        "https://example.invalid/",
        "01.22.21",
    ],
)
def test_only_supported_stable_versions_are_accepted(install, version):
    with pytest.raises(web_update.WebUpdateError, match=r"releases\.unsupported_version"):
        web_update.start(version)
    assert not install[1]


def test_current_version_is_not_reinstalled(install):
    with pytest.raises(web_update.WebUpdateError, match=r"releases\.already_installed"):
        web_update.start("1.22.20")


def test_backup_failure_never_launches_an_updater(install, monkeypatch):
    def failed():
        raise RestorePointError("synthetic backup failure")

    monkeypatch.setattr(web_update, "create_restore_point", failed)
    with pytest.raises(web_update.WebUpdateError, match=r"releases\.backup_failed"):
        web_update.start("1.22.21")
    assert not install[1]
    assert not (install[0] / "runtime" / "web-update" / "job.json").exists()


def test_unpublished_or_offline_release_never_creates_a_backup(install, monkeypatch):
    monkeypatch.setattr(web_update, "create_restore_point", lambda: pytest.fail("should not archive"))

    def failed(_version):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(web_update.releases, "published_version", failed)
    with pytest.raises(web_update.WebUpdateError, match=r"releases\.not_published"):
        web_update.start("1.22.21")
    assert not install[1]


def test_launch_failure_keeps_the_archive_and_records_a_terminal_result(install):
    def failed(*_args, **_kwargs):
        raise PermissionError("cannot break away")

    install[2].spawn_detached = failed
    with pytest.raises(web_update.WebUpdateError, match=r"releases\.launch_failed"):
        web_update.start("1.22.21")
    result = web_update.status()
    assert result["status"] == "failed"
    assert result["safety_point"] == "synthetic-copy"
    assert result["active"] is False


def test_real_archive_is_created_and_verified_before_spawn(install, monkeypatch):
    from tow.restore_points import create_restore_point, point_path

    save_secrets({})
    save_state({"topics": [], "mirrors": {}})
    monkeypatch.setattr(web_update, "create_restore_point", create_restore_point)
    archive = []

    def spawn(*_args, **_kwargs):
        job = json.loads((install[0] / "runtime" / "web-update" / "job.json").read_text())
        archive.append(point_path(job["safety_point"]).read_bytes())
        return 123

    install[2].spawn_detached = spawn
    web_update.start("1.22.21")
    assert archive[0]


def test_interrupted_job_is_visible_and_not_silently_replaced(install):
    web_update.start("1.22.21")
    path = install[0] / "runtime" / "web-update" / "job.json"
    job = json.loads(path.read_text())
    job.update(status="installing", pid=456, started_at=time.time() - 60)
    path.write_text(json.dumps(job))
    assert web_update.status()["status"] == "interrupted"
    with pytest.raises(web_update.WebUpdateError, match=r"releases\.interrupted"):
        web_update.start("1.22.22")
    assert len(install[1]) == 1


@pytest.mark.parametrize(
    "content",
    ["not json", "[]", '{"status":[]}', " " * 65537],
    ids=["invalid-json", "wrong-container", "wrong-status", "oversized"],
)
def test_unreadable_job_fails_closed(install, content):
    path = install[0] / "runtime" / "web-update" / "job.json"
    path.parent.mkdir(parents=True)
    path.write_text(content)
    with pytest.raises(web_update.WebUpdateError, match=r"releases\.job_unreadable"):
        web_update.start("1.22.21")
    assert not install[1]


def test_previous_successful_compatible_version_is_available(install):
    (install[0] / "update-state.json").write_text(json.dumps({"status": "ok", "previous_version": "1.22.21"}))
    assert web_update.status()["rollback_version"] == "1.22.21"


def test_service_manager_cannot_kill_a_posix_web_updater(install, monkeypatch):
    install[2].name = "linux"
    monkeypatch.setenv("TOW_AUTOSTART", "systemd")
    assert web_update.status()["supported"] is False
    with pytest.raises(web_update.WebUpdateError, match=r"releases\.detached_unavailable"):
        web_update.start("1.22.21")
    assert not install[1]


def test_development_checkout_never_installs(monkeypatch):
    assert web_update.status()["supported"] is False
    with pytest.raises(web_update.WebUpdateError, match=r"releases\.not_runtime"):
        web_update.start("1.22.21")


def test_web_install_route_confirms_queue_and_enforces_origin(monkeypatch):
    calls = []
    monkeypatch.setattr(
        services,
        "start_web_update",
        lambda version: calls.append(version) or {"ok": True, "id": "synthetic", "target": version},
    )
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    response = client.post("/updates/install", data={"version": "1.22.21"})
    assert response.status_code == 202
    assert response.json()["ok"] is True
    assert calls == ["1.22.21"]
    assert TestClient(app).post("/updates/install", data={"version": "1.22.21"}).status_code == 403
    assert calls == ["1.22.21"]


def test_worker_is_still_python_311_compatible():
    ast.parse(Path(update_worker.__file__).read_text(), feature_version=(3, 11))


@pytest.mark.parametrize("schema", [True, -1, 2, "1", None])
def test_target_schema_preflight_refuses_incompatible_data(tmp_path, schema):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "state.json").write_text(json.dumps({"schema_version": schema}))
    work = SimpleNamespace(
        root=tmp_path,
        code=SimpleNamespace(kind="git"),
        state={"target": "synthetic"},
        sys=SimpleNamespace(git=lambda *_args: "STATE_SCHEMA_VERSION = 1\n"),
    )
    with pytest.raises(RuntimeError, match="cannot read"):
        update_worker.schema_preflight(work, RuntimeError)


def test_archive_target_schema_is_checked_without_touching_live_data(tmp_path):
    source = tmp_path / "app.new" / "src" / "tow"
    source.mkdir(parents=True)
    (source / "store.py").write_text("STATE_SCHEMA_VERSION = 1\n")
    work = SimpleNamespace(root=tmp_path, code=SimpleNamespace(kind="archive", new=tmp_path / "app.new"))
    update_worker.schema_preflight(work, RuntimeError)
    assert not (tmp_path / "data").exists()


def test_windows_updater_never_falls_back_into_the_parent_job(monkeypatch):
    from tow.platform.windows import WindowsBackend

    calls = []

    def failed(*_args, **kwargs):
        calls.append(kwargs)
        raise PermissionError("job escape refused")

    monkeypatch.setattr("tow.platform.windows.subprocess.Popen", failed)
    with pytest.raises(PermissionError, match="refused"):
        WindowsBackend().spawn_detached(["synthetic"], require_breakaway=True)
    assert len(calls) == 1


def test_update_log_is_bounded_and_redacted(install):
    result = web_update.start("1.22.21")
    folder = install[0] / "runtime" / "web-update" / result["id"]
    (folder / "update.log").write_text("x" * 20000 + "\nhttps://example.invalid/a?token=synthetic-secret\n")
    text = web_update.log_tail()
    assert len(text) <= 16000
    assert "synthetic-secret" not in text


def test_reading_log_cannot_select_an_arbitrary_path(install):
    path = install[0] / "runtime" / "web-update" / "job.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"id": "../foreign", "status": "ok"}))
    with pytest.raises(web_update.WebUpdateError, match=r"releases\.job_unreadable"):
        web_update.log_tail()


@pytest.mark.parametrize("status", ["invented", "", "interrupted"])
def test_unknown_stored_phase_refuses_a_new_install(install, status):
    web_update.start("1.22.21")
    path = install[0] / "runtime" / "web-update" / "job.json"
    job = json.loads(path.read_text())
    job["status"] = status
    path.write_text(json.dumps(job))
    with pytest.raises(web_update.WebUpdateError, match=r"releases\.job_unreadable"):
        web_update.start("1.22.22")
    assert len(install[1]) == 1


def test_nonfinite_journal_is_never_treated_as_a_running_job(install):
    path = install[0] / "runtime" / "web-update" / "job.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"id":"' + "a" * 32 + '","status":"queued","started_at":Infinity}')
    with pytest.raises(web_update.WebUpdateError, match=r"releases\.job_unreadable"):
        web_update.status()


def test_old_success_does_not_claim_another_version_is_installed(install):
    web_update.start("1.22.21")
    path = install[0] / "runtime" / "web-update" / "job.json"
    job = json.loads(path.read_text())
    job["status"] = "ok"
    path.write_text(json.dumps(job))
    assert web_update.status()["status"] == "superseded"


def test_web_mutations_are_refused_during_an_update_but_reads_and_signout_work(monkeypatch):
    monkeypatch.setattr(services, "web_update_status", lambda: {"active": True})
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    assert client.post("/settings/service/restart").status_code == 409
    assert client.get("/healthz").status_code == 200
    assert client.get("/settings").status_code == 200
    assert client.post("/logout", follow_redirects=False).status_code != 409


def test_a_completed_terminal_recovery_unblocks_a_dead_job_without_get_writes(install):
    web_update.start("1.22.21")
    path = install[0] / "runtime" / "web-update" / "job.json"
    job = json.loads(path.read_text())
    job.update(status="installing", pid=456, started_at=time.time() - 60)
    path.write_text(json.dumps(job))
    before = path.read_bytes()
    (install[0] / "update-state.json").write_text(
        json.dumps(
            {
                "status": "ok",
                "target_version": "1.22.20",
                "finished_at": datetime.now(UTC).isoformat(),
            }
        )
    )
    assert web_update.status()["status"] == "recovered"
    assert path.read_bytes() == before
    assert web_update.start("1.22.22")["ok"] is True
    assert len(install[1]) == 2
