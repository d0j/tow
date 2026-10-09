"""Days, safe cleanup, free-space refusal and disabled scheduler/watchdog behavior."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from helpers import flash_of
from test_supervisor import make

from tow import snapshots
from tow.backup_retention import MIB, MIN_OLDER_COPIES, retained_copies, retention_settings
from tow.config import ConfigError, load_config, save_config
from tow.paths import config_path
from tow.store import save_state
from tow.supervisor.schedule import Schedule
from tow.watchdog import run_watchdog
from tow.web import app

NOW = datetime(2026, 10, 5, 12, tzinfo=UTC)


def test_default_is_seven_days_and_legacy_count_is_not_silently_reinterpreted():
    assert retention_settings({})["days"] == 7
    assert retention_settings({})["mode"] == "days"
    assert retention_settings({"backup_keep": 3})["mode"] == "count"
    assert retention_settings({"backup_keep": 3, "backup_days": 7})["mode"] == "days"


def test_empty_optional_yaml_values_use_defaults_and_preserve_real_legacy_count():
    assert retention_settings({"backup_keep": None, "backup_days": None, "backup_max_mib": None}) == {
        "mode": "days",
        "keep": 14,
        "days": 7,
        "max_mib": 0,
    }
    assert retention_settings({"backup_keep": 3, "backup_days": None})["mode"] == "count"


@pytest.mark.parametrize("days", [1, 2, 7, 14, 30, 90, 3650])
def test_age_boundary_keeps_latest_and_only_newer_than_cutoff(days):
    entries = [(str(n), NOW - timedelta(days=n), MIB) for n in range(days + 3)]
    kept, pending = retained_copies(entries, newest="0", now=NOW, policy=retention_settings({"backup_days": days}))
    assert kept == {str(n) for n in range(max(days, 1 + MIN_OLDER_COPIES))}
    assert not pending


def test_sparse_offline_history_and_clock_rollback_keep_new_copy_and_future_history():
    entries = [("old", NOW - timedelta(days=50), 1), ("future", NOW + timedelta(days=10), 1), ("new", NOW, 1)]
    kept, pending = retained_copies(entries, newest="new", now=NOW, policy=retention_settings({}))
    assert kept == {"new", "future", "old"}  # the only earlier copy: kept whatever its age
    assert not pending


def test_a_gap_longer_than_the_retention_keeps_the_newest_earlier_copies():
    # The machine was off for ten days: every earlier copy is older than the seven days kept.
    entries = [(f"old-{n}", NOW - timedelta(days=10 + n), MIB) for n in range(7)]
    entries.append(("new", NOW, MIB))
    kept, pending = retained_copies(entries, newest="new", now=NOW, policy=retention_settings({}))
    assert kept == {"new", "old-0", "old-1", "old-2"}
    assert not pending


def test_budget_shortens_history_but_never_erases_oversize_new_copy():
    entries = [("old", NOW - timedelta(days=1), MIB), ("new", NOW, 2 * MIB)]
    kept, pending = retained_copies(entries, newest="new", now=NOW, policy=retention_settings({"backup_max_mib": 1}))
    assert kept == {"new"}
    assert pending


def _client():
    return TestClient(app, headers={"Origin": "http://127.0.0.1"})


@pytest.mark.parametrize("days", ["1", "7", "30", "3650"])
def test_days_form_saves_exact_integer_without_deleting_anything(days):
    response = _client().post("/settings/backup/retention", data={"days": days}, follow_redirects=False)
    assert response.status_code == 303
    assert load_config()["backup_days"] == int(days)
    assert "сейчас существующие копии не удалялись" in flash_of(response.headers["location"])


@pytest.mark.parametrize("days", ["", "0", "-1", "7.5", "3651", "true", "NaN", "9" * 5000, "٧", "７", "1_0"])
def test_bad_days_form_keeps_config(days):
    before = config_path().read_bytes()
    response = _client().post("/settings/backup/retention", data={"days": days}, follow_redirects=False)
    assert response.status_code == 303
    assert config_path().read_bytes() == before
    assert "целое число" in flash_of(response.headers["location"])


@pytest.mark.parametrize("enabled", ["", "1"])
def test_automatic_switch_readback(enabled):
    response = _client().post("/settings/backup/automatic", data={"enabled": enabled}, follow_redirects=False)
    assert response.status_code == 303
    assert load_config()["backup_enabled"] is bool(enabled)


def test_disabled_schedule_has_no_backup_job_or_due_date_then_reenables():
    schedule = Schedule(started_at=NOW.timestamp() - 86400, backup_enabled=False)
    facts = {"last_scheduled_check": 0, "last_backup_ok": 0, "last_backup_attempt": 0}
    assert "backup" not in dict(schedule.due(NOW.timestamp(), **facts))
    assert "backup" not in schedule.next_due(NOW.timestamp(), **facts)
    schedule.backup_enabled = True
    assert "backup" in dict(schedule.due(NOW.timestamp(), **facts))


@pytest.mark.parametrize("unreadable", [False, True])
def test_backup_switch_is_rechecked_before_launch_without_attempt_marker(tmp_path, unreadable):
    world, supervisor = make(tmp_path)
    assert supervisor.schedule.backup_enabled is True
    before = dict(supervisor._persisted)

    def latest_config():
        if unreadable:
            raise OSError("unreadable settings")
        return {"backup_enabled": False}

    supervisor.deps.load_config = latest_config
    supervisor._start_job("backup", world.clock.wall, world.clock.mono)
    assert world.jobs() == []
    assert supervisor.job is None
    assert supervisor._persisted == before
    assert supervisor.schedule.backup_enabled is False


def test_disabled_watchdog_does_not_claim_recovery_or_alert_about_stale_copy():
    cfg = load_config()
    cfg["backup_enabled"] = False
    save_config(cfg)
    save_state({"topics": [], "health": {"auto_at_ts": int(NOW.timestamp())}})
    report = run_watchdog(
        is_healthy=lambda _p: True,
        deploy_running=lambda: False,
        send=lambda _text: True,
        now=NOW.timestamp,
        sleep=lambda _s: None,
    )
    assert report["backup_enabled"] is False
    assert report["backup_ok"] is None
    assert not any("ночн" in text.casefold() for text in report["alerts"])


def test_no_space_keeps_old_copy_and_does_not_create_partial(tmp_path, monkeypatch):
    monkeypatch.setenv("TOW_HOME", str(tmp_path / "data"))
    cfg = load_config()
    cfg.update(backup_dir=str(tmp_path / "night"), backup_days=7)
    save_config(cfg)
    old = Path(snapshots.create_snapshot()["snapshot"])
    before = {p: p.read_bytes() for p in old.rglob("*") if p.is_file()}
    usage = shutil.disk_usage(old)
    monkeypatch.setattr(snapshots.shutil, "disk_usage", lambda _p: type(usage)(usage.total, usage.total, 0))
    with pytest.raises(snapshots.SnapshotError, match="не хватает места"):
        snapshots.create_snapshot()
    assert all(p.read_bytes() == content for p, content in before.items())
    assert sorted(p.name for p in old.parent.iterdir()) == [old.name]


def test_real_age_cleanup_removes_only_owned_expired_copy_after_verified_replacement(tmp_path, monkeypatch):
    monkeypatch.setenv("TOW_HOME", str(tmp_path / "data"))
    cfg = load_config()
    cfg.update(backup_dir=str(tmp_path / "night"), backup_days=7)
    save_config(cfg)
    old = Path(snapshots.create_snapshot()["snapshot"])
    proof = old / "MANIFEST.json"
    manifest = json.loads(proof.read_bytes())
    manifest["created_at"] = "2020-01-01T00:00:00+00:00"
    manifest["signature"] = snapshots._signature(manifest, snapshots._signing_key())
    proof.write_text(json.dumps(manifest), encoding="utf-8")
    assert snapshots.verify_snapshot(old)["signed"] is True
    for _ in range(MIN_OLDER_COPIES):  # newer earlier copies, so the expired one is not among the kept ones
        snapshots.create_snapshot()
    assert old.exists()
    new = snapshots.create_snapshot()
    assert not old.exists()
    assert new["pruned"] == [old.name]
    assert snapshots.verify_snapshot(Path(new["snapshot"]))["signed"] is True


def test_disabled_automatic_copies_still_allow_manual_creation(tmp_path, monkeypatch):
    monkeypatch.setenv("TOW_HOME", str(tmp_path / "data"))
    cfg = load_config()
    cfg.update(backup_dir=str(tmp_path / "night"), backup_enabled=False)
    save_config(cfg)
    response = _client().post("/settings/backup/now", follow_redirects=False)
    assert response.status_code == 303
    assert len(snapshots.list_snapshots(limit=None)) == 1


@pytest.mark.parametrize(("key", "value"), [("backup_days", 0), ("backup_days", 3651), ("backup_enabled", "maybe")])
def test_config_rejects_invalid_backup_controls(key, value):
    cfg = load_config()
    cfg[key] = value
    save_config(cfg)
    with pytest.raises(ConfigError, match=key):
        load_config()
