from __future__ import annotations

import json
from pathlib import Path

import pytest
import test_import_monitorrent as monitorrent_fixtures
import test_web_update as update_fixtures
from fastapi.testclient import TestClient
from helpers import flash_kind, shown

from tow import restore_points, web_update
from tow.config import load_config, save_config
from tow.i18n import t
from tow.store import load_state, save_secrets, save_state
from tow.web import app, services

database = monitorrent_fixtures.database
install = update_fixtures.install


def _seed(lang="ru"):
    cfg = load_config()
    cfg["language"] = lang
    save_config(cfg)
    save_state({"topics": [], "mirrors": {}, "sentinel": "original"})
    save_secrets({})


def _block(monkeypatch, victim, mode="held"):
    original_unlink, original_lstat = Path.unlink, Path.lstat
    fault = {"enabled": True, "attempted": False}

    def unlink(path, *args, **kwargs):
        if path == victim and fault["enabled"]:
            fault["attempted"] = True
            if mode == "held":
                raise PermissionError("synthetic held archive")
            return None
        return original_unlink(path, *args, **kwargs)

    def lstat(path, *args, **kwargs):
        if path == victim and fault["enabled"] and fault["attempted"] and mode == "unreadable":
            raise PermissionError("synthetic unreadable deletion result")
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    monkeypatch.setattr(Path, "lstat", lstat)
    return fault


@pytest.mark.parametrize("mode", ["held", "noop", "unreadable"])
def test_cleanup_needs_confirmed_absence_and_retries_without_losing_new_copy(monkeypatch, mode):
    _seed()
    old = restore_points.create_restore_point()
    victim = restore_points.point_path(old["id"])
    before = victim.read_bytes()
    monkeypatch.setattr(restore_points, "RESTORE_POINT_LIMIT", 1)
    fault = _block(monkeypatch, victim, mode)
    made = restore_points.create_restore_point()
    assert made["cleanup_warning"]
    assert victim.read_bytes() == before
    assert restore_points.point_path(made["id"]).is_file()
    assert restore_points.cleanup_pending() is True
    fault["enabled"] = False
    next_point = restore_points.create_restore_point()
    assert "cleanup_warning" not in next_point
    assert restore_points.cleanup_pending() is False
    assert not victim.exists()
    assert [point["id"] for point in restore_points.list_restore_points()] == [next_point["id"]]


@pytest.mark.parametrize("lang", ["ru", "en"])
@pytest.mark.parametrize("operation", ["point", "portable"])
def test_real_web_restore_preserves_success_and_reports_safety_copy_cleanup(monkeypatch, tmp_path, lang, operation):
    _seed(lang)
    selected = restore_points.create_restore_point()
    portable = tmp_path / "portable.towx"
    restore_points.export_portable_bundle(portable)
    save_state({"topics": [], "mirrors": {}, "sentinel": "changed"})
    old = restore_points.create_restore_point()
    monkeypatch.setattr(restore_points, "RESTORE_POINT_LIMIT", 1)
    _block(monkeypatch, restore_points.point_path(old["id"]))
    with TestClient(app, headers={"Origin": "http://127.0.0.1", "Accept-Language": lang}) as client:
        if operation == "point":
            response = client.post(f"/settings/restore-points/{selected['id']}/restore", follow_redirects=False)
        else:
            response = client.post(
                "/settings/portable/import",
                data={"operation": "restore"},
                files={"backup_file": ("portable.towx", portable.read_bytes())},
                follow_redirects=False,
            )
    assert response.status_code == 303
    flash = shown(response.headers["location"])
    assert t("backup.restore_point.restore_cleanup_warning", lang) in flash
    assert flash_kind(response.headers["location"]) == "warn"
    assert load_state()["sentinel"] == "original"
    assert restore_points.cleanup_pending() is True
    if operation == "point":
        assert restore_points.point_path(selected["id"]).is_file()


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_restore_result_keeps_both_audit_and_cleanup_warnings(monkeypatch, lang):
    from tow.i18n import use
    from tow.web.routes_backup import _restore_outcome

    use(lang)
    message, kind = _restore_outcome("web.settings.restored", {"log_recorded": False, "cleanup_warning": "pending"})
    assert kind == "warn"
    assert t("web.settings.audit_missing", lang) in message
    assert t("backup.restore_point.restore_cleanup_warning", lang) in message


def test_monitorrent_does_not_hide_cleanup_of_its_verified_restore_point(database, monkeypatch):
    from tow.import_monitorrent import import_monitorrent

    def fail(**_kwargs):
        raise PermissionError("synthetic held archive")

    monkeypatch.setattr(restore_points, "_prune", fail)
    result = import_monitorrent(database, apply=True)
    assert result["topics_added"] == 1
    assert result["cleanup_warning"]
    assert restore_points.point_path(result["restore_point"]).is_file()


def test_web_update_keeps_cleanup_warning_across_restart_and_terminal_status(install, monkeypatch):
    runtime, _calls, _backend = install
    monkeypatch.setattr(web_update, "create_restore_point", lambda: {"id": "copy", "cleanup_warning": "pending"})
    result = web_update.start("1.22.21")
    assert result["backup_cleanup_pending"] is True
    path = runtime / "runtime" / "web-update" / "job.json"
    job = json.loads(path.read_text())
    assert job["backup_cleanup_pending"] is True
    job.update(status="ok", finished_at=job["started_at"] + 10)
    path.write_text(json.dumps(job), encoding="utf-8")
    monkeypatch.setattr(web_update, "__version__", "1.22.21")
    before = path.read_bytes()
    assert web_update.status()["backup_cleanup_pending"] is True
    monkeypatch.setattr(services, "web_update_status", web_update.status)
    with TestClient(app) as client:
        assert client.get("/updates/status").json()["backup_cleanup_pending"] is True
    assert path.read_bytes() == before


@pytest.mark.parametrize("bad", ["false", 0, [], None])
def test_update_cleanup_flag_is_typed_not_untrusted_text(install, bad):
    runtime, _calls, _backend = install
    web_update.start("1.22.21")
    path = runtime / "runtime" / "web-update" / "job.json"
    job = json.loads(path.read_text())
    job.update(status="failed", backup_cleanup_pending=bad)
    path.write_text(json.dumps(job), encoding="utf-8")
    with pytest.raises(web_update.WebUpdateError, match=r"releases\.job_unreadable"):
        web_update.status()


def test_legacy_update_without_cleanup_flag_remains_readable(install):
    runtime, _calls, _backend = install
    web_update.start("1.22.21")
    path = runtime / "runtime" / "web-update" / "job.json"
    job = json.loads(path.read_text())
    job.pop("backup_cleanup_pending", None)
    job["status"] = "failed"
    path.write_text(json.dumps(job), encoding="utf-8")
    assert web_update.status()["backup_cleanup_pending"] is False


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_settings_and_history_show_pending_point_cleanup_after_reload(monkeypatch, lang):
    from tow.log import format_event, history_events

    _seed(lang)
    old = restore_points.create_restore_point()
    monkeypatch.setattr(restore_points, "RESTORE_POINT_LIMIT", 1)
    _block(monkeypatch, restore_points.point_path(old["id"]))
    restore_points.create_restore_point()
    with TestClient(app, headers={"Accept-Language": lang}) as client:
        page = client.get("/settings")
    assert page.status_code == 200
    assert t("backup.restore_point.cleanup_pending", lang) in page.text
    manual = page.text.split('id="backup-manual"', 1)[1].split("</article>", 1)[0]
    assert 'class="pill warn"' in manual
    summary = page.text.split('id="settings-transfer-title"', 1)[1].split("</summary>", 1)[0]
    assert 'class="pill warn"' in summary
    rows = [row for row in history_events(group="errors") if row["kind"] == "backup_cleanup_pending"]
    assert rows
    assert rows[-1]["copy_kind"] == "restore_point"
    assert t("log.cleanup.restore_point", lang) in format_event(rows[-1])["detail"]


def test_failed_copy_does_not_clear_prior_cleanup_warning(monkeypatch):
    _seed()
    restore_points._record_cleanup(True, restore_points.restore_points_dir())

    def fail(*_args, **_kwargs):
        raise PermissionError("synthetic denied export")

    monkeypatch.setattr(restore_points, "export_bundle", fail)
    with pytest.raises(restore_points.RestorePointError):
        restore_points.create_restore_point()
    assert restore_points.cleanup_pending() is True


def test_cleanup_status_is_bound_to_folder_and_reads_do_not_write(tmp_path):
    from tow.paths import data_dir

    _seed()
    original = restore_points.restore_points_dir()
    restore_points._record_cleanup(True, original)
    path = data_dir() / "restore-point-status.json"
    before = path.read_bytes()
    cfg = load_config()
    cfg["restore_points_dir"] = str(tmp_path / "other")
    save_config(cfg)
    assert restore_points.cleanup_pending() is False
    cfg.pop("restore_points_dir")
    save_config(cfg)
    assert restore_points.cleanup_pending() is True
    assert path.read_bytes() == before


@pytest.mark.parametrize("content", ["not-json", "[]", '{"cleanup_pending": "true"}', "\ufffd"])
def test_invalid_monitoring_state_never_grants_delete_permission_or_breaks_settings(content):
    from tow.paths import data_dir

    _seed()
    (data_dir() / "restore-point-status.json").write_text(content, encoding="utf-8")
    assert restore_points.cleanup_pending() is False
    with TestClient(app) as client:
        assert client.get("/settings").status_code == 200


def test_status_write_failure_keeps_verified_copy_and_operation_warning(monkeypatch):
    _seed()

    def fail(*_args, **_kwargs):
        raise PermissionError("synthetic denied metadata")

    monkeypatch.setattr(restore_points, "atomic_write_text", fail)
    monkeypatch.setattr(restore_points, "_prune", fail)
    made = restore_points.create_restore_point()
    assert made["cleanup_warning"]
    assert restore_points.point_path(made["id"]).is_file()


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_watchdog_reports_point_cleanup_and_retry_once_without_failing_heartbeat(monkeypatch, lang):
    from tow.watchdog import run_watchdog

    _seed(lang)
    cfg = load_config()
    cfg["heartbeat_url"] = "https://hc-ping.com/isolated-point-cleanup"
    save_config(cfg)
    now = 1791072000
    save_state({"topics": [], "health": {"auto_at_ts": now}})
    old = restore_points.create_restore_point()
    monkeypatch.setattr(restore_points, "RESTORE_POINT_LIMIT", 1)
    fault = _block(monkeypatch, restore_points.point_path(old["id"]))
    restore_points.create_restore_point()
    sent, pings = [], []

    def check():
        result = run_watchdog(
            is_healthy=lambda _port: True,
            deploy_running=lambda: False,
            send=lambda text: sent.append(text) or True,
            now=lambda: now,
            sleep=lambda _seconds: None,
            pulse=lambda _url, ok: pings.append(ok) or True,
            flush=lambda: None,
        )
        assert result["backup_ok"] is True
        assert result["service_ok"] is True
        assert result["checks_ok"] is True
        return result

    assert check()["restore_point_cleanup_pending"] is True
    check()
    assert sent == [t("watchdog.alert.point_cleanup_pending", lang)]
    fault["enabled"] = False
    restore_points.create_restore_point()
    assert check()["restore_point_cleanup_pending"] is False
    check()
    assert sent == [t("watchdog.alert.point_cleanup_pending", lang), t("watchdog.alert.point_cleanup_ok", lang)]
    assert pings == [True] * 4
