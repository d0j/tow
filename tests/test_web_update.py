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
    assert argv[1:4] == ["-I", "-S", "-u"]
    assert Path(argv[4]).is_relative_to(runtime / "runtime")
    assert Path(argv[4]).is_file()
    from tow.platform import locks

    assert (Path(argv[4]).parent / "locks.py").read_bytes() == Path(locks.__file__).read_bytes()
    assert (Path(argv[4]).parent / "worker.lock").read_bytes() == b"\0"
    assert job["lease_version"] == 1
    assert argv[5] == "--handoff"
    assert options["require_breakaway"] is False
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
        "1.22.20",
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


def test_current_version_is_not_reinstalled(install, monkeypatch):
    monkeypatch.setattr(web_update, "__version__", "1.22.21")
    with pytest.raises(web_update.WebUpdateError, match=r"releases\.already_installed"):
        web_update.start("1.22.21")


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


@pytest.mark.parametrize("phase", ["preparing", "stopping", "backup", "installing", "checking", "rolling_back"])
def test_unheld_worker_lease_does_not_mistake_a_reused_pid_for_an_active_update(install, phase):
    web_update.start("1.22.21")
    path = install[0] / "runtime" / "web-update" / "job.json"
    job = json.loads(path.read_text())
    lease = path.parent / job["id"] / "worker.lock"
    lease.write_bytes(b"\0")
    job.update(status=phase, pid=123, started_at=time.time() - 60, lease_version=1)
    path.write_text(json.dumps(job))
    before = path.read_bytes()

    result = web_update.status()

    assert result["active"] is False
    assert result["status"] == "interrupted"
    assert path.read_bytes() == before


def test_reused_pid_does_not_block_a_later_verified_terminal_recovery(install):
    web_update.start("1.22.21")
    path = install[0] / "runtime" / "web-update" / "job.json"
    job = json.loads(path.read_text())
    (path.parent / job["id"] / "worker.lock").write_bytes(b"\0")
    job.update(status="installing", pid=123, started_at=time.time() - 60, lease_version=1)
    path.write_text(json.dumps(job))
    before = path.read_bytes()
    (install[0] / "update-state.json").write_text(
        json.dumps({"status": "ok", "target_version": "1.22.20", "finished_at": datetime.now(UTC).isoformat()})
    )

    assert web_update.status()["status"] == "recovered"
    assert path.read_bytes() == before
    assert web_update.start("1.22.22")["ok"] is True
    assert len(install[1]) == 2


def test_worker_lease_keeps_a_long_update_active_even_without_a_pid(install):
    from tow.platform import locks

    web_update.start("1.22.21")
    path = install[0] / "runtime" / "web-update" / "job.json"
    job = json.loads(path.read_text())
    lease = path.parent / job["id"] / "worker.lock"
    lease.write_bytes(b"\0")
    job.update(status="installing", started_at=time.time() - 86400, lease_version=1)
    path.write_text(json.dumps(job))
    with lease.open("r+b") as handle:
        assert locks.lock(handle, wait=False)
        try:
            assert web_update.status()["active"] is True
            with pytest.raises(web_update.WebUpdateError, match=r"releases\.busy"):
                web_update.start("1.22.22")
            assert len(install[1]) == 1
        finally:
            locks.unlock(handle)
    assert web_update.status()["status"] == "interrupted"


@pytest.mark.parametrize("lease_version", [None, False, True, 0, 2, "1", []])
def test_unknown_or_malformed_worker_lease_fails_closed(install, lease_version):
    web_update.start("1.22.21")
    path = install[0] / "runtime" / "web-update" / "job.json"
    job = json.loads(path.read_text())
    job["lease_version"] = lease_version
    path.write_text(json.dumps(job))
    with pytest.raises(web_update.WebUpdateError, match=r"releases\.job_unreadable"):
        web_update.status()
    with pytest.raises(web_update.WebUpdateError, match=r"releases\.job_unreadable"):
        web_update.start("1.22.22")
    assert len(install[1]) == 1


@pytest.mark.parametrize("damage", ["missing", "empty", "oversized", "directory", "permission"])
def test_unreadable_worker_lease_never_unblocks_web_writes(install, monkeypatch, damage):
    web_update.start("1.22.21")
    path = install[0] / "runtime" / "web-update" / "job.json"
    job = json.loads(path.read_text())
    job.update(status="installing", pid=123, started_at=time.time() - 60)
    path.write_text(json.dumps(job))
    lease = path.parent / job["id"] / "worker.lock"
    if damage in {"missing", "directory"}:
        lease.unlink()
        if damage == "directory":
            lease.mkdir()
    elif damage in {"empty", "oversized"}:
        lease.write_bytes(b"" if damage == "empty" else b"xx")
    else:
        original = Path.open

        def denied(self, *args, **kwargs):
            if self == lease:
                raise PermissionError("synthetic lease access denied")
            return original(self, *args, **kwargs)

        monkeypatch.setattr(Path, "open", denied)
    monkeypatch.setattr(services, "web_update_status", web_update.status)
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    assert client.post("/settings/service/restart").status_code == 503
    assert client.get("/healthz").status_code == 200
    assert client.get("/updates/status").status_code == 503
    assert len(install[1]) == 1


def test_real_lease_controls_the_http_write_guard_and_recovery(install, monkeypatch):
    from tow.platform import locks

    web_update.start("1.22.21")
    path = install[0] / "runtime" / "web-update" / "job.json"
    job = json.loads(path.read_text())
    job.update(status="installing", pid=123, started_at=time.time() - 60)
    path.write_text(json.dumps(job))
    before = path.read_bytes()
    (install[0] / "update-state.json").write_text(
        json.dumps({"status": "ok", "target_version": "1.22.20", "finished_at": datetime.now(UTC).isoformat()})
    )
    monkeypatch.setattr(services, "web_update_status", web_update.status)
    restarted = []
    monkeypatch.setattr(services, "request_restart", lambda: restarted.append(True) or {"ok": True, "id": "test"})
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    with (path.parent / job["id"] / "worker.lock").open("r+b") as handle:
        assert locks.lock(handle, wait=False)
        try:
            assert client.get("/updates/status").json()["active"] is True
            assert client.post("/settings/service/restart").status_code == 409
            assert restarted == []
        finally:
            locks.unlock(handle)
    assert client.get("/updates/status").json()["status"] == "recovered"
    assert client.post("/settings/service/restart", follow_redirects=False).status_code == 303
    assert restarted == [True]
    assert path.read_bytes() == before


@pytest.mark.parametrize("alive", [False, True])
def test_pre_lease_worker_records_remain_conservative_and_readable(install, alive):
    web_update.start("1.22.21")
    path = install[0] / "runtime" / "web-update" / "job.json"
    job = json.loads(path.read_text())
    job.pop("lease_version")
    job.update(status="installing", pid=123 if alive else 456, started_at=time.time() - 60)
    path.write_text(json.dumps(job))
    before = path.read_bytes()
    assert web_update.status()["active"] is alive
    assert path.read_bytes() == before


@pytest.mark.parametrize("system", ["windows", "linux", "macos"])
@pytest.mark.parametrize("identity", ["own", "quoted", "foreign", "suffix", "unknown"])
def test_legacy_worker_identity_is_bound_to_the_whole_unique_script_path(install, system, identity):
    web_update.start("1.22.21")
    path = install[0] / "runtime" / "web-update" / "job.json"
    job = json.loads(path.read_text())
    job.pop("lease_version")
    job.update(status="installing", pid=123, started_at=time.time() - 60)
    path.write_text(json.dumps(job))
    script = str(path.parent / job["id"] / "worker.py")
    commands = {
        "own": f"python -I -S -u {script} --after-parent 123",
        "quoted": f'python -I -S -u "{script}" --after-parent 123',
        "foreign": "unrelated-program --watch",
        "suffix": f"python {script}.not-the-worker",
        "unknown": None,
    }
    install[2].name = system
    command = commands[identity]
    install[2].process_command = lambda _pid: command
    assert web_update.status()["active"] is (identity in {"own", "quoted", "unknown", "suffix"})
    if identity in {"unknown", "suffix"}:
        assert web_update.status()["error"] == "releases.worker_unverified"


def test_legacy_reused_pid_with_another_command_allows_verified_terminal_recovery(install):
    web_update.start("1.22.21")
    path = install[0] / "runtime" / "web-update" / "job.json"
    job = json.loads(path.read_text())
    job.pop("lease_version")
    job.update(status="installing", pid=123, started_at=time.time() - 60)
    path.write_text(json.dumps(job))
    before = path.read_bytes()
    install[2].process_command = lambda _pid: "unrelated-program"
    (install[0] / "update-state.json").write_text(
        json.dumps({"status": "ok", "target_version": "1.22.20", "finished_at": datetime.now(UTC).isoformat()})
    )
    assert web_update.status()["status"] == "recovered"
    assert path.read_bytes() == before
    assert web_update.start("1.22.22")["ok"] is True


def test_unknown_legacy_identity_is_an_explicit_safe_http_warning(install, monkeypatch):
    from tow.i18n import t

    web_update.start("1.22.21")
    path = install[0] / "runtime" / "web-update" / "job.json"
    job = json.loads(path.read_text())
    job.pop("lease_version")
    job.update(status="installing", pid=123, started_at=time.time() - 60)
    path.write_text(json.dumps(job))
    before = path.read_bytes()
    monkeypatch.setattr(services, "web_update_status", web_update.status)
    probes = []
    install[2].process_command = lambda pid: probes.append(pid) or None
    result = TestClient(app).get("/updates/status").json()
    assert result["active"] is True
    assert result["error_message"] == t("releases.worker_unverified", "ru")
    assert path.read_bytes() == before
    assert probes == [123]


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


def test_a_web_server_of_an_older_systemd_unit_does_not_start_an_update(install, monkeypatch):
    # The unit of TOW 1.24.1 sets only TOW_ROOT: systemd's own variable and the control group
    # tell that stopping tow.service would end the updater with it.
    install[2].name = "linux"
    monkeypatch.delenv("TOW_AUTOSTART", raising=False)
    monkeypatch.setenv("INVOCATION_ID", "0123456789abcdef")
    monkeypatch.setattr(
        "tow.autostart._cgroup", lambda: "0::/user.slice/user-1000.slice/user@1000.service/app.slice/tow.service\n"
    )
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
    from tow.platform import locks

    ast.parse(Path(update_worker.__file__).read_text(), feature_version=(3, 11))
    ast.parse(Path(locks.__file__).read_text(), feature_version=(3, 11))


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


def test_update_log_never_shows_the_install_folder(install):
    # The folder names the owner's account (C:\Users\<name>\TOW); the page is read over the LAN too.
    root = install[0]
    result = web_update.start("1.22.21")
    folder = root / "runtime" / "web-update" / result["id"]
    lines = [f"backing up {root}\\data", f"stopping {root.as_posix()}/app/scripts/tow", "done"]
    (folder / "update.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    text = web_update.log_tail()
    assert str(root) not in text
    assert root.as_posix() not in text
    assert "backing up <TOW>\\data" in text
    assert "stopping <TOW>/app/scripts/tow" in text


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


@pytest.mark.parametrize("phase", ["queued", "failed", "ok"])
@pytest.mark.parametrize("field", ["started_at", "finished_at"])
@pytest.mark.parametrize(
    "value",
    [10**1000, 1e300, -1, True, "1", [], {}],
    ids=["integer-overflow", "calendar-overflow", "negative", "boolean", "string", "list", "object"],
)
def test_invalid_job_dates_fail_closed_without_rewriting_evidence(install, monkeypatch, phase, field, value):
    web_update.start("1.22.21")
    path = install[0] / "runtime" / "web-update" / "job.json"
    job = json.loads(path.read_text())
    job.update(status=phase)
    job[field] = value
    path.write_text(json.dumps(job))
    before = path.read_bytes()
    monkeypatch.setattr(services, "web_update_status", web_update.status)
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})

    for action in (web_update.status, web_update.log_tail, lambda: web_update.start("1.22.22")):
        with pytest.raises(web_update.WebUpdateError, match=r"releases\.job_unreadable"):
            action()
    assert client.get("/updates/status").status_code == 503
    assert client.post("/settings/service/restart").status_code == 503
    assert client.get("/healthz").status_code == 200
    assert path.read_bytes() == before
    assert len(install[1]) == 1


def test_os_date_conversion_failure_never_claims_terminal_recovery(install, monkeypatch):
    web_update.start("1.22.21")
    path = install[0] / "runtime" / "web-update" / "job.json"
    job = json.loads(path.read_text())
    job.update(status="failed")
    path.write_text(json.dumps(job))
    before = path.read_bytes()
    (install[0] / "update-state.json").write_text(json.dumps({"finished_at": "synthetic-os-date"}))

    def unsupported_timestamp():
        raise OSError("synthetic calendar conversion failure")

    monkeypatch.setattr(
        web_update,
        "datetime",
        SimpleNamespace(fromisoformat=lambda _value: SimpleNamespace(tzinfo=UTC, timestamp=unsupported_timestamp)),
    )
    assert web_update.status()["status"] == "failed"
    assert path.read_bytes() == before


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


@pytest.mark.parametrize("old_status", ["installing", "failed", "refused", "rolled_back"])
def test_a_completed_terminal_recovery_unblocks_a_dead_job_without_get_writes(install, old_status):
    web_update.start("1.22.21")
    path = install[0] / "runtime" / "web-update" / "job.json"
    job = json.loads(path.read_text())
    job.update(status=old_status, pid=456, started_at=time.time() - 60, finished_at=time.time() - 30)
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


@pytest.mark.parametrize("newer", [False, True])
def test_terminal_recovery_does_not_hide_a_later_web_failure(install, newer):
    web_update.start("1.22.21")
    path = install[0] / "runtime" / "web-update" / "job.json"
    job = json.loads(path.read_text())
    job.update(status="failed", started_at=time.time() - 120, finished_at=time.time() - 30)
    path.write_text(json.dumps(job))
    finished = datetime.fromtimestamp(time.time() - (10 if newer else 60), UTC).isoformat()
    (install[0] / "update-state.json").write_text(
        json.dumps({"status": "ok", "target_version": "1.22.20", "finished_at": finished})
    )
    assert web_update.status()["status"] == ("recovered" if newer else "failed")


@pytest.mark.parametrize(
    "error",
    [
        "releases.broker_failed",
        "releases.inherited_job",
        "releases.parent_wait_failed",
        "releases.job_unreadable",
        "foreign-secret",
    ],
)
def test_update_status_localizes_only_known_reasons(monkeypatch, error):
    monkeypatch.setattr(services, "web_update_status", lambda: {"supported": True, "status": "failed", "error": error})
    response = TestClient(app).get("/updates/status")
    assert response.status_code == 200
    result = response.json()
    assert ("error_message" in result) is (error != "foreign-secret")
    if "error_message" in result:
        assert result["error_message"] != error
