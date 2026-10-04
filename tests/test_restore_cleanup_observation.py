"""Unknown cleanup observations must not resolve a confirmed cleanup warning."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tow import diagnostic_json, restore_points, snapshots, watchdog
from tow.config import load_config, save_config
from tow.i18n import t
from tow.paths import data_dir
from tow.pulse import Probes
from tow.store import save_secrets, save_state
from tow.web import app

NOW = 1791072000


def _seed(lang="en"):
    cfg = load_config()
    cfg["language"] = lang
    cfg.pop("heartbeat_url", None)
    save_config(cfg)
    save_state({"topics": [], "mirrors": {}, "health": {"auto_at_ts": NOW}})
    save_secrets({})


def _check(sent):
    return watchdog.run_watchdog(
        is_healthy=lambda _port: True,
        deploy_running=lambda: False,
        now=lambda: NOW,
        sleep=lambda _seconds: None,
        send=lambda text: sent.append(text) or True,
        flush=lambda: None,
        probes=Probes.inert(),
    )


def _damage(mode):
    path = data_dir() / "restore-point-status.json"
    if mode == "missing":
        path.unlink()
    else:
        path.write_bytes({"malformed": b"{", "type": b'{"cleanup_pending":"false"}'}[mode])
    return path


@pytest.mark.parametrize("lang", ["ru", "en"])
@pytest.mark.parametrize("mode", ["missing", "malformed", "type"])
def test_unknown_observation_does_not_resolve_warning_after_restart(lang, mode):
    _seed(lang)
    restore_points._record_cleanup(True, restore_points.restore_points_dir())
    sent = []
    _check(sent)
    path = _damage(mode)
    raw = path.read_bytes() if path.exists() else None
    for _ in range(3):
        _check(sent)
        assert watchdog._load_state()["restore_point_cleanup"] is False
        assert not any(t("watchdog.alert.point_cleanup_ok", lang) in text for text in sent)
        assert (path.read_bytes() if path.exists() else None) == raw


@pytest.mark.parametrize("lang", ["ru", "en"])
@pytest.mark.parametrize("mode", ["missing", "malformed", "type"])
def test_settings_do_not_claim_known_cleanup_for_missing_or_damaged_record(lang, mode):
    _seed(lang)
    point = restore_points.create_restore_point()
    _damage(mode)
    with TestClient(app, headers={"Accept-Language": lang}) as client:
        response = client.get("/settings")
    assert response.status_code == 200
    manual = response.text.split('id="backup-manual"', 1)[1].split("</article>", 1)[0]
    assert 'class="pill warn"' in manual
    assert t("settings.backups.pill_unknown", lang) in manual
    assert point["id"] in manual


def test_changed_folder_cannot_resolve_old_folder_warning(tmp_path):
    _seed()
    restore_points._record_cleanup(True, restore_points.restore_points_dir())
    sent = []
    _check(sent)
    cfg = load_config()
    cfg["restore_points_dir"] = str(tmp_path / "other-points")
    save_config(cfg)
    _check(sent)
    restore_points._record_cleanup(False, restore_points.restore_points_dir())
    _check(sent)
    assert sent == [t("watchdog.alert.point_cleanup_pending", "en")]


@pytest.mark.parametrize("previous", [None, True])
def test_unknown_or_healthy_previous_state_cannot_create_recovery(previous):
    _seed()
    watchdog._state_path().write_text(json.dumps({"restore_point_cleanup": previous}), encoding="utf-8")
    restore_points._record_cleanup(False, restore_points.restore_points_dir())
    sent = []
    _check(sent)
    assert not sent


@pytest.mark.parametrize("lang", ["ru", "en"])
@pytest.mark.parametrize("pending", [True, False])
@pytest.mark.parametrize("mode", ["missing", "malformed", "type"])
def test_observation_recovery_is_distinct_from_cleanup_completion(lang, pending, mode):
    _seed(lang)
    folder = restore_points.restore_points_dir()
    restore_points._record_cleanup(pending, folder)
    sent = []
    _check(sent)
    sent.clear()
    path = _damage(mode)
    for _ in range(3):
        report = _check(sent)
        assert report["restore_point_cleanup_pending"] is None
        assert report["restore_point_cleanup_read_error"] is True
        assert watchdog._load_state()["restore_point_cleanup"] is not pending
    assert sent == [t("watchdog.alert.point_cleanup_unreadable", lang)]
    assert not path.exists() if mode == "missing" else path.is_file()
    restore_points._record_cleanup(pending, folder)
    _check(sent)
    _check(sent)
    assert sent[-1] == t("watchdog.alert.point_cleanup_readable", lang)
    assert len(sent) == 2
    restore_points._record_cleanup(False, folder)
    _check(sent)
    _check(sent)
    if pending:
        assert sent[-1] == t("watchdog.alert.point_cleanup_ok", lang)
        assert len(sent) == 3
    else:
        assert len(sent) == 2


BAD_RECORDS = (
    b"{",
    b"[]",
    b"null",
    b"\xff",
    b'{"x":NaN}',
    b'{"x":1e9999}',
    b'{"x":' + b"[" * 129 + b"0" + b"]" * 129 + b"}",
    b'{"cleanup_pending":null,"location":"somewhere"}',
    b'{"cleanup_pending":0,"location":"somewhere"}',
    b'{"cleanup_pending":"false","location":"somewhere"}',
    b'{"cleanup_pending":true,"location":[]}',
    b'{"cleanup_pending":true,"location":null}',
    b'{"cleanup_pending":true,"location":""}',
    b"{}",
)


@pytest.mark.parametrize("raw", BAD_RECORDS)
def test_cleanup_record_is_bounded_typed_and_read_only(raw):
    _seed()
    path = data_dir() / "restore-point-status.json"
    path.write_bytes(raw)
    before = set(path.parent.iterdir())
    result = restore_points.cleanup_status()
    assert result["pending"] is None
    assert result["read_error"] is True
    assert result["location"] == str(restore_points.restore_points_dir().resolve())
    assert path.read_bytes() == raw
    assert set(path.parent.iterdir()) == before


def test_size_limit_applies_before_decode(monkeypatch):
    _seed()
    path = data_dir() / "restore-point-status.json"
    raw = b" " * (diagnostic_json.MAX_BYTES + 1)
    path.write_bytes(raw)
    assert restore_points.cleanup_status()["read_error"] is True
    assert path.read_bytes() == raw


@pytest.mark.parametrize("mode", ["directory", "permission", "reparse"])
def test_nonregular_or_inaccessible_records_are_not_opened(monkeypatch, mode):
    _seed()
    path = data_dir() / "restore-point-status.json"
    if mode == "directory":
        path.mkdir()
    else:
        restore_points._record_cleanup(False, restore_points.restore_points_dir())
        original = Path.lstat

        def lstat(candidate, *args, **kwargs):
            if candidate == path:
                if mode == "permission":
                    raise PermissionError("synthetic denied observation")
                from types import SimpleNamespace

                return SimpleNamespace(st_mode=original(candidate).st_mode, st_file_attributes=1024)
            return original(candidate, *args, **kwargs)

        monkeypatch.setattr(Path, "lstat", lstat)

    def never_open(_path):
        pytest.fail("must reject before opening an unsafe record")

    monkeypatch.setattr(restore_points, "read_object", never_open)
    result = restore_points.cleanup_status()
    assert result["pending"] is None
    assert result["read_error"] is True


def test_folder_resolution_failure_retains_previous_fact(monkeypatch):
    _seed()
    restore_points._record_cleanup(True, restore_points.restore_points_dir())
    sent = []
    _check(sent)

    def unavailable(**_kwargs):
        raise restore_points.RestorePointError("synthetic unavailable folder")

    monkeypatch.setattr(restore_points, "restore_points_dir", unavailable)
    _check(sent)
    _check(sent)
    assert watchdog._load_state()["restore_point_cleanup"] is False
    assert sent[-1] == t("watchdog.alert.point_cleanup_unreadable", "en")
    assert len(sent) == 2


def test_new_folder_pending_has_its_own_warning_not_old_recovery(tmp_path):
    _seed()
    restore_points._record_cleanup(True, restore_points.restore_points_dir())
    sent = []
    _check(sent)
    cfg = load_config()
    cfg["restore_points_dir"] = str(tmp_path / "new-points")
    save_config(cfg)
    restore_points._record_cleanup(True, restore_points.restore_points_dir())
    _check(sent)
    assert sent == [t("watchdog.alert.point_cleanup_pending", "en")] * 2


def test_first_start_without_record_is_quiet_and_unknown():
    _seed()
    sent = []
    for _ in range(3):
        result = _check(sent)
        assert result["restore_point_cleanup_pending"] is None
        assert result["restore_point_cleanup_read_error"] is False
        assert "restore_point_cleanup" not in watchdog._load_state()
    assert not sent
    with TestClient(app) as client:
        response = client.get("/settings")
    manual = response.text.split('id="backup-manual"', 1)[1].split("</article>", 1)[0]
    assert 'class="pill mut"' in manual
    assert t("backup.restore_point.cleanup_unknown") not in manual


@pytest.mark.parametrize("pending", [True, False])
def test_unrecorded_legacy_watchdog_retains_known_result(pending):
    _seed()
    watchdog._state_path().write_text(json.dumps({"restore_point_cleanup": not pending}), encoding="utf-8")
    sent = []
    _check(sent)
    assert watchdog._load_state()["restore_point_cleanup"] is not pending
    assert sent == [t("watchdog.alert.point_cleanup_unreadable", "en")]


@pytest.mark.parametrize("bad", [None, 0, "false", [], {}])
def test_invalid_writer_input_cannot_replace_existing_observation(bad):
    _seed()
    folder = restore_points.restore_points_dir()
    restore_points._record_cleanup(True, folder)
    path = data_dir() / "restore-point-status.json"
    before = path.read_bytes()
    with pytest.raises(TypeError):
        restore_points._record_cleanup(bad, folder)
    assert path.read_bytes() == before


def test_writer_validation_failure_keeps_previous_bytes(monkeypatch):
    _seed()
    folder = restore_points.restore_points_dir()
    restore_points._record_cleanup(True, folder)
    path = data_dir() / "restore-point-status.json"
    before = path.read_bytes()
    monkeypatch.setattr(diagnostic_json, "MAX_BYTES", 10)
    restore_points._record_cleanup(False, folder)
    assert path.read_bytes() == before


def test_damaged_night_record_does_not_resolve_night_cleanup_warning():
    _seed()
    snapshots.status_path().write_text(json.dumps({"last_cleanup_pending": True}), encoding="utf-8")
    sent = []
    _check(sent)
    snapshots.status_path().write_bytes(b"{")
    for _ in range(3):
        _check(sent)
        assert watchdog._load_state()["backup_cleanup"] is False
    assert not any(t("watchdog.alert.backup_cleanup_ok", "en") in text for text in sent)


@pytest.mark.parametrize("initial_pending", [True, False])
@pytest.mark.parametrize("prune_fails", [True, False])
def test_failed_metadata_write_cannot_confirm_stale_cleanup(monkeypatch, initial_pending, prune_fails):
    _seed()
    first = restore_points.create_restore_point()
    restore_points._record_cleanup(initial_pending, restore_points.restore_points_dir())
    path = data_dir() / "restore-point-status.json"
    before = path.read_bytes()

    def denied(*_args, **_kwargs):
        raise PermissionError("synthetic denied operation")

    monkeypatch.setattr(restore_points, "atomic_write_text", denied)
    if prune_fails:
        monkeypatch.setattr(restore_points, "_prune", denied)
    made = restore_points.create_restore_point()
    assert restore_points.point_path(first["id"]).is_file()
    assert restore_points.point_path(made["id"]).is_file()
    assert bool(made.get("cleanup_warning")) is prune_fails
    assert path.read_bytes() == before
    assert restore_points.cleanup_status()["pending"] is None
    assert restore_points.cleanup_status()["read_error"] is True


def test_new_archive_with_backward_clock_invalidates_old_result():
    _seed()
    restore_points.create_restore_point()
    assert restore_points.cleanup_status()["pending"] is False
    path = restore_points.restore_points_dir() / "20000101T000000Z-deadbeef.towx"
    path.write_bytes(b"synthetic foreign archive with an older clock")
    assert restore_points.cleanup_status()["pending"] is None
    assert restore_points.cleanup_status()["read_error"] is True


@pytest.mark.parametrize("pending", [True, False])
def test_legacy_unbound_record_cannot_confirm_completed_cleanup(pending):
    _seed()
    folder = restore_points.restore_points_dir()
    path = data_dir() / "restore-point-status.json"
    path.write_text(json.dumps({"cleanup_pending": pending, "location": str(folder.resolve())}), encoding="utf-8")
    result = restore_points.cleanup_status()
    assert result["pending"] is (True if pending else None)
    assert result["read_error"] is False


@pytest.mark.parametrize("inventory", [None, 0, [], "wrong", "g" * 64])
def test_invalid_inventory_binding_is_unknown(inventory):
    _seed()
    folder = restore_points.restore_points_dir()
    path = data_dir() / "restore-point-status.json"
    path.write_text(
        json.dumps({"cleanup_pending": False, "location": str(folder.resolve()), "inventory": inventory}),
        encoding="utf-8",
    )
    assert restore_points.cleanup_status()["pending"] is None
    assert restore_points.cleanup_status()["read_error"] is True
