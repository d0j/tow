"""A copy in another destination never proves that the newly selected folder is protected."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tow import snapshots
from tow.config import load_config, save_config
from tow.paths import in_root
from tow.snapshots.record import location_revision, record_status
from tow.store import save_secrets, save_state
from tow.supervisor import _facts, layout
from tow.supervisor.core import Supervisor


def select(folder):
    cfg = load_config()
    cfg["backup_dir"] = str(folder)
    save_config(cfg)
    return cfg


def test_a_different_folder_does_not_inherit_success_or_failure_and_retains_old_evidence(tmp_path):
    old = tmp_path.parent / (tmp_path.name + "-old")
    new = tmp_path.parent / (tmp_path.name + "-new")
    select(old)
    save_state({"topics": []})
    save_secrets({})
    snapshots.create_snapshot()
    snapshots.record_failure("synthetic access failure")
    original = snapshots.status_path().read_bytes()
    assert snapshots.current_status()["last_ok_at"]
    assert snapshots.current_status()["last_error"]
    select(new)
    scoped = snapshots.current_status()
    assert not scoped.get("last_ok_at")
    assert not scoped.get("last_error")
    assert _facts()["last_backup_ok"] == 0
    assert snapshots.status_path().read_bytes() == original
    select(old)
    assert snapshots.current_status()["last_error"] == "synthetic access failure"


def test_a_failure_in_the_new_folder_does_not_rebind_the_old_good_copy(tmp_path):
    old = tmp_path.parent / (tmp_path.name + "-old")
    new = tmp_path.parent / (tmp_path.name + "-new")
    select(old)
    record_status(
        last_ok_at=100,
        last_snapshot="tow-20261103-000000",
        location=str(old.resolve()),
        location_in_root=in_root(old),
        last_error="",
    )
    select(new)
    snapshots.record_failure("new folder unavailable")
    current = snapshots.current_status()
    assert not current.get("last_ok_at")
    assert snapshots.status_failed(current)
    assert current["last_error"] == "new folder unavailable"
    assert snapshots.status()["location"] == str(old.resolve())
    select(old)
    current = snapshots.current_status()
    assert current["last_ok_at"] == 100
    assert not snapshots.status_failed(current)


def test_a_moved_portable_folder_keeps_its_success_binding(tmp_path, monkeypatch):
    selected = tmp_path.parent / (tmp_path.name + "-night")
    cfg = select(selected)
    monkeypatch.setattr("tow.backup_actions.in_root", lambda _path: "backup/night")
    record_status(last_ok_at=100, location="synthetic-previous-install", location_in_root="backup/night", last_error="")
    assert snapshots.current_status(cfg=cfg)["last_ok_at"] == 100


def test_unreadable_status_is_still_an_observation_error(tmp_path):
    select(tmp_path.parent / (tmp_path.name + "-night"))
    snapshots.status_path().write_text("{broken", encoding="utf-8")
    assert snapshots.current_status()["read_error"] is True


def supervisor(cfg, tmp_path):
    return Supervisor(
        SimpleNamespace(load_config=lambda: cfg, now=lambda: 1_793_664_000.0, monotonic=lambda: 0.0),
        python="synthetic",
        home=tmp_path / "logs",
    )


def test_old_attempt_and_in_process_cadence_cannot_delay_an_empty_new_destination(tmp_path):
    old = tmp_path.parent / (tmp_path.name + "-old")
    new = tmp_path.parent / (tmp_path.name + "-new")
    cfg = select(old)
    sup = supervisor(cfg, tmp_path)
    now = sup.deps.now()
    sup._persisted.update(backup_attempt_at=now, backup_location=location_revision(cfg))
    sup.schedule.last["backup"] = now
    assert sup._facts_now()["last_backup_attempt"] == now
    new_cfg = dict(cfg, backup_dir=str(new))
    sup._apply_config(new_cfg)
    assert sup._facts_now()["last_backup_attempt"] == 0
    assert "backup" not in sup.schedule.last
    assert sup.schedule.backup_due_at(now, 0, sup._facts_now()["last_backup_attempt"]) <= now
    layout.write_json(layout.schedule_path(), sup._persisted)
    restarted = supervisor(new_cfg, tmp_path)
    assert restarted._facts_now()["last_backup_attempt"] == 0


@pytest.mark.parametrize("value", ["data", "../outside"])
def test_an_invalid_backup_destination_does_not_disable_the_supervisor(tmp_path, value):
    cfg = dict(load_config(), backup_dir=value)
    sup = supervisor(cfg, tmp_path)
    assert sup.schedule.interval_sec > 0
    assert sup._facts_now()["last_backup_attempt"] == 0


@pytest.mark.parametrize("previous_failed", [False, True])
@pytest.mark.parametrize("legacy_binding", [False, True])
def test_new_empty_destination_gets_its_own_pending_grace_without_fabricated_recovery(
    tmp_path,
    monkeypatch,
    previous_failed,
    legacy_binding,
):
    from tow import watchdog

    old = tmp_path.parent / (tmp_path.name + "-old")
    new = tmp_path.parent / (tmp_path.name + "-new")
    select(old)
    save_state({"topics": []})
    save_secrets({})
    snapshots.create_snapshot()
    if previous_failed:
        snapshots.record_failure("old destination unavailable")
    now = {"at": 1_793_664_000.0}
    previous = {
        "at": now["at"] - 60,
        "backup": not previous_failed,
        "backup_watch_since": now["at"] - 2 * watchdog.BACKUP_STALE_SEC,
        "backup_cleanup_location": str(old.resolve()),
    }
    if not legacy_binding:
        previous["backup_watch_location"] = str(old.resolve())
    watchdog._save_state(previous)
    select(new)
    sent = []

    def check():
        save_state({"topics": [], "health": {"auto_at_ts": now["at"]}})
        return watchdog.run_watchdog(
            is_healthy=lambda _port: True,
            deploy_running=lambda: False,
            now=lambda: now["at"],
            sleep=lambda _seconds: None,
            flush=lambda: None,
            send=lambda text: sent.append(text) or True,
        )

    first = check()
    assert first["backup_ok"] is None
    assert first["backup_state"] == "pending"
    assert first["backup_watch_since"] == now["at"]
    assert first["alerts"] == []
    now["at"] += 120
    assert check()["backup_watch_since"] == first["backup_watch_since"]
    assert sent == []
    now["at"] += watchdog.BACKUP_STALE_SEC
    overdue = check()
    assert overdue["backup_ok"] is False
    assert overdue["backup_state"] == "failed"
    assert len(sent) == 1
    monkeypatch.setattr("tow.snapshots.create.time.time", lambda: now["at"])
    snapshots.create_snapshot()
    recovered = check()
    assert recovered["backup_ok"] is True
    assert recovered["backup_state"] == "verified"
    assert len(sent) == 2
