"""Automatic cleanup retries only a bound, local, already-authorized retention operation."""

from __future__ import annotations

from pathlib import Path

import pytest

from tow import backup_maintenance as maintenance
from tow import paths, platform, restore_points, snapshots
from tow.config import load_config, save_config
from tow.platform._common import Volume
from tow.snapshots import create
from tow.store import persistence_lock, save_secrets, save_state

NOW = 1_793_664_000.0


@pytest.fixture
def copies(tmp_path, monkeypatch):
    cfg = load_config()
    cfg.update(
        restore_points_dir=str(tmp_path.parent / (tmp_path.name + "-manual")),
        backup_dir=str(tmp_path.parent / (tmp_path.name + "-night")),
        backup_keep=1,
        backup_days=None,
        backup_enabled=True,
    )
    save_config(cfg)
    save_state({"topics": []})
    save_secrets({})
    monkeypatch.setattr(platform, "volume_of", lambda _path: Volume("uuid:synthetic-volume", "local"))
    monkeypatch.setattr(restore_points, "RESTORE_POINT_LIMIT", 2)
    monkeypatch.setattr(restore_points, "_prune", lambda **_kwargs: None)
    return cfg


def manual():
    for _index in range(3):
        restore_points.create_restore_point()
    restore_points._record_cleanup(True, restore_points.restore_points_dir())
    return maintenance._plan(maintenance._context("manual", load_config())).candidate


def nightly(monkeypatch):
    monkeypatch.setattr(create, "_prune_night_copies", lambda *_args: ([], True, False))
    for _index in range(3):
        snapshots.create_snapshot()


def test_released_manual_archive_is_cleaned_without_creating_another_copy(copies):
    manual()
    before = restore_points.list_restore_points()
    assert maintenance.run(now=NOW)["manual"] == "complete"
    after = restore_points.list_restore_points()
    assert len(after) == 2
    assert {row["id"] for row in after} < {row["id"] for row in before}
    assert restore_points.cleanup_status()["pending"] is False


def test_local_night_cleanup_continues_one_copy_per_pass_without_spending_failure_budget(copies, monkeypatch):
    nightly(monkeypatch)
    protected = snapshots.status()["last_snapshot"]
    assert maintenance.run(now=NOW)["night"] == "continued"
    assert len(snapshots.list_snapshots(limit=None)) == 2
    assert snapshots.cleanup_status()["pending"] is True
    assert maintenance.run(now=NOW + 600)["night"] == "complete"
    assert [row["name"] for row in snapshots.list_snapshots()] == [protected]
    assert snapshots.cleanup_status()["pending"] is False
    assert maintenance._read()["night"]["failures"] == 0


def test_failed_removal_has_a_finite_durable_retry_budget(copies, monkeypatch):
    target = manual()
    assert target is not None
    original = Path.unlink
    attempts = []

    def held(path, *args, **kwargs):
        if path == target:
            attempts.append(path)
            raise PermissionError("held")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", held)
    assert maintenance.run(now=NOW)["manual"] == "remove_failed"
    assert maintenance.run(now=NOW + 1)["manual"] == "waiting_or_exhausted"
    assert maintenance.run(now=NOW + 600)["manual"] == "remove_failed"
    assert maintenance.run(now=NOW + 2400)["manual"] == "remove_failed"
    assert maintenance.run(now=NOW + 20000)["manual"] == "waiting_or_exhausted"
    assert len(attempts) == 3
    assert restore_points.cleanup_status()["pending"] is True


def test_lost_unlink_ack_is_confirmed_from_actual_absence(copies, monkeypatch):
    target = manual()
    original = Path.unlink

    def lost(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path == target:
            raise OSError("lost acknowledgement")
        return result

    monkeypatch.setattr(Path, "unlink", lost)
    assert maintenance.run(now=NOW)["manual"] == "complete"
    assert restore_points.cleanup_status()["pending"] is False


@pytest.mark.parametrize("change", ["folder", "policy", "volume", "restore", "replace", "owner"])
def test_change_during_expensive_verification_cancels_the_old_prune(copies, monkeypatch, tmp_path, change):
    target = manual()
    assert target is not None
    original = maintenance._verify

    def verify(plan):
        verified = original(plan)
        if plan.context.kind != "manual":
            return verified
        if change == "folder":
            save_config(dict(load_config(), restore_points_dir=str(tmp_path.parent / (tmp_path.name + "-other"))))
        elif change == "policy":
            monkeypatch.setattr(restore_points, "RESTORE_POINT_LIMIT", 3)
        elif change == "volume":
            monkeypatch.setattr(platform, "volume_of", lambda _path: Volume("uuid:another-volume", "local"))
        elif change == "restore":
            with persistence_lock():
                restore_points.create_restore_point(protected={target.stem})
        elif change == "owner":
            monkeypatch.setattr(maintenance, "derive_local_secret", lambda _scope: "changed-owner")
        else:
            target.write_bytes(b"replacement must never be deleted")
        return verified

    monkeypatch.setattr(maintenance, "_verify", verify)
    assert maintenance.run(now=NOW)["manual"] in {
        "context_changed",
        "volume_changed",
        "observation_unknown",
        "not_pending",
    }
    assert target.is_file()


def test_foreign_archive_is_not_deleted_or_called_clean(copies):
    manual()
    target = restore_points.restore_points_dir() / "20000101T000000Z-12345678.towx"
    target.write_bytes(b"foreign archive")
    restore_points._record_cleanup(True, target.parent)
    assert maintenance.run(now=NOW)["manual"] == "validation_failed"
    assert target.read_bytes() == b"foreign archive"
    assert restore_points.cleanup_status()["pending"] is True


def test_foreign_member_in_night_copy_is_not_swept_up(copies, monkeypatch):
    nightly(monkeypatch)
    folder = snapshots.snapshot_path(snapshots.status()["last_snapshot"])
    foreign = folder / "foreign.txt"
    foreign.write_text("keep", encoding="utf-8")
    assert maintenance.run(now=NOW)["night"] == "copy_unknown"
    assert foreign.is_file()
    assert len(snapshots.list_snapshots(limit=None)) == 3
    assert snapshots.cleanup_status()["pending"] is True


def test_night_auto_off_and_remote_unknown_volumes_never_delete(copies, monkeypatch):
    nightly(monkeypatch)
    save_config(dict(load_config(), backup_enabled=False))
    assert maintenance.run(now=NOW)["night"] == "disabled"
    assert len(snapshots.list_snapshots(limit=None)) == 3
    save_config(copies)
    monkeypatch.setattr(platform, "volume_of", lambda _path: Volume("net:synthetic", "network"))
    assert maintenance.run(now=NOW)["night"] == "remote_folder"
    assert maintenance._read()["night"]["reason"] == "remote_folder"
    assert len(snapshots.list_snapshots(limit=None)) == 3
    monkeypatch.setattr(platform, "volume_of", lambda _path: None)
    assert maintenance.run(now=NOW)["night"] == "volume_unknown"


def test_unknown_cleanup_is_not_permission_to_prune(copies):
    manual()
    path = paths.data_dir() / "restore-point-status.json"
    path.write_text("{unreadable", encoding="utf-8")
    assert maintenance.run(now=NOW)["manual"] == "observation_unknown"
    assert len(restore_points.list_restore_points()) == 3


def test_failed_observation_write_does_not_claim_cleanup_success(copies, monkeypatch):
    manual()
    monkeypatch.setattr(restore_points, "_record_cleanup", lambda *_args: None)
    assert maintenance.run(now=NOW)["manual"] == "observation_unconfirmed"
    assert restore_points.cleanup_status()["pending"] is True


def test_read_only_diagnostic_does_not_even_create_a_retry_record(copies):
    manual()
    before = {path.name: path.read_bytes() for path in paths.data_dir().iterdir() if path.is_file()}
    with paths.read_only():
        assert maintenance.run(now=NOW) == {"manual": "read_only", "night": "read_only"}
    after = {path.name: path.read_bytes() for path in paths.data_dir().iterdir() if path.is_file()}
    assert before == after


def test_overlapping_pass_cannot_take_an_already_reserved_cleanup(copies, monkeypatch):
    manual()
    original = maintenance._verify
    overlap = []

    def verify(plan):
        overlap.append(maintenance.run(now=NOW))
        return original(plan)

    monkeypatch.setattr(maintenance, "_verify", verify)
    assert maintenance.run(now=NOW)["manual"] == "complete"
    assert overlap[0]["manual"] == "waiting_or_exhausted"
    assert len(restore_points.list_restore_points()) == 2


def test_expensive_decryption_does_not_hold_the_shared_data_lock(copies, monkeypatch):
    import threading

    manual()
    original = maintenance._verify
    available = threading.Event()

    def write():
        with persistence_lock():
            available.set()

    def verify(plan):
        thread = threading.Thread(target=write)
        thread.start()
        assert available.wait(2), "expensive verification held the data lock"
        thread.join(2)
        return original(plan)

    monkeypatch.setattr(maintenance, "_verify", verify)
    assert maintenance.run(now=NOW)["manual"] == "complete"


@pytest.mark.parametrize("problem", ["foreign", "foreign_member", "owned"])
def test_only_the_install_signed_partial_without_foreign_members_is_removed(copies, monkeypatch, problem):
    import json

    from tow.snapshots.manifest import signing_key
    from tow.snapshots.record import cleanup_inventory, record_status

    snapshots.create_snapshot()
    folder = snapshots.backup_root()
    partial = folder / ".tow-20261103-000000.partial"
    partial.mkdir()
    proof = create._partial_proof(partial.name, signing_key())
    if problem == "foreign":
        proof["signature"] = "invalid"
    (partial / "PARTIAL.json").write_text(json.dumps(proof), encoding="utf-8")
    (partial / "state.json").write_text("{}", encoding="utf-8")
    if problem == "foreign_member":
        (partial / "foreign.txt").write_text("keep", encoding="utf-8")
    record_status(last_cleanup_pending=True, **cleanup_inventory(folder))
    assert maintenance.run(now=NOW)["night"] == ("complete" if problem == "owned" else "copy_unknown")
    assert partial.exists() is (problem != "owned")
    assert snapshots.cleanup_status()["pending"] is (problem != "owned")


def test_interrupted_owned_tree_removal_keeps_failure_budget_and_can_finish(copies, monkeypatch):
    nightly(monkeypatch)
    original = create.remove_copy

    def partly_removed(folder, _parent, marker, _expected):
        member = next(path for path in folder.iterdir() if path.is_file() and path.name != marker)
        member.unlink()
        return False

    monkeypatch.setattr(create, "remove_copy", partly_removed)
    assert maintenance.run(now=NOW)["night"] == "remove_failed"
    assert maintenance._read()["night"]["failures"] == 1
    assert maintenance.run(now=NOW + 1)["night"] == "waiting_or_exhausted"
    monkeypatch.setattr(create, "remove_copy", original)
    assert maintenance.run(now=NOW + 600)["night"] == "continued"
    assert maintenance.run(now=NOW + 1200)["night"] == "complete"
    assert snapshots.cleanup_status()["pending"] is False


def test_a_lost_cleanup_record_is_unknown_not_an_empty_retry_budget(copies):
    manual()
    retry = paths.data_dir() / maintenance.STATE_NAME
    retry.write_text("{broken", encoding="utf-8")
    assert maintenance.run(now=NOW)["manual"] == "retry_record_unknown"
    assert retry.read_text(encoding="utf-8") == "{broken"
    assert len(restore_points.list_restore_points()) == 3


def test_no_automatic_retry_after_the_authorization_window(copies, monkeypatch):
    manual()
    attempts = []
    monkeypatch.setattr(
        maintenance, "_verify", lambda _plan: attempts.append(1) or (_ for _ in ()).throw(OSError("held"))
    )
    assert maintenance.run(now=NOW)["manual"] == "validation_failed"
    assert maintenance.run(now=NOW + maintenance.TTL_SEC + 1)["manual"] == "waiting_or_exhausted"
    assert attempts == [1]
    assert restore_points.cleanup_status()["pending"] is True


def test_crash_before_removal_does_not_reset_the_same_contexts_budget(copies):
    manual()
    context = maintenance._context("manual", load_config())
    assert maintenance._reserve(context, NOW)
    assert maintenance._reserve(context, NOW + 600)
    assert maintenance._reserve(context, NOW + 1200)
    assert maintenance._reserve(context, NOW + 1800) is None
    assert maintenance._read()["manual"]["failures"] == 3
    assert len(restore_points.list_restore_points()) == 3


def test_backward_clock_reanchors_the_local_retry_once(copies):
    manual()
    context = maintenance._context("manual", load_config())
    assert maintenance._reserve(context, NOW + 3600)
    assert maintenance._reserve(context, NOW) is None
    anchored = maintenance._read()["manual"]["next_at"]
    assert anchored == NOW + 600
    assert maintenance._reserve(context, NOW + 1) is None
    assert maintenance._read()["manual"]["next_at"] == anchored


@pytest.mark.parametrize("keep", [2, 10])
def test_all_retained_manual_archives_corrupt_cannot_delete_the_only_old_valid_copy(copies, monkeypatch, keep):
    from tow.bundle import verify_bundle

    monkeypatch.setattr(restore_points, "RESTORE_POINT_LIMIT", keep)
    for _index in range(keep + 1):
        restore_points.create_restore_point()
    points = restore_points.list_restore_points()
    target = restore_points.point_path(points[-1]["id"])
    original = target.read_bytes()
    for point in points[:keep]:
        restore_points.point_path(point["id"]).write_bytes(b"corrupt retained archive")
    restore_points._record_cleanup(True, target.parent)
    assert maintenance.run(now=NOW)["manual"] == "protected_copy_unknown"
    assert target.read_bytes() == original
    verify_bundle(target, restore_points._passphrase())
    assert restore_points.cleanup_status()["pending"] is True


def test_one_valid_retained_manual_archive_is_enough_when_the_newest_is_corrupt(copies):
    target = manual()
    points = restore_points.list_restore_points()
    restore_points.point_path(points[0]["id"]).write_bytes(b"corrupt newest archive")
    valid_retained = restore_points.point_path(points[1]["id"])
    original = valid_retained.read_bytes()
    assert maintenance.run(now=NOW)["manual"] == "complete"
    assert target is not None
    assert not target.exists()
    assert valid_retained.read_bytes() == original


def test_public_archive_verification_never_accepts_a_foreign_location(copies, tmp_path):
    manual()
    point = restore_points.point_path(restore_points.list_restore_points()[0]["id"])
    foreign = tmp_path / point.name
    foreign.write_bytes(point.read_bytes())
    restore_points.verify_restore_point_file(point)
    with pytest.raises(restore_points.RestorePointError, match=r"."):
        restore_points.verify_restore_point_file(foreign)


def test_public_cleanup_observation_cannot_rebind_the_selected_folder(copies, tmp_path):
    manual()
    status = paths.data_dir() / "restore-point-status.json"
    before = status.read_bytes()
    with pytest.raises(restore_points.RestorePointError, match=r"."):
        restore_points.record_cleanup_observation(False, tmp_path)
    assert status.read_bytes() == before


def test_retry_record_broken_during_verification_cannot_abort_the_watchdog_maintenance_boundary(copies, monkeypatch):
    manual()
    original = maintenance._verify
    retry = paths.data_dir() / maintenance.STATE_NAME

    def verify(plan):
        verified = original(plan)
        retry.write_text("{broken", encoding="utf-8")
        return verified

    monkeypatch.setattr(maintenance, "_verify", verify)
    result = maintenance.run(now=NOW)
    assert result["manual"] == "retry_record_unknown"
    assert "night" in result  # the next independent duty is still observed
    assert retry.read_text(encoding="utf-8") == "{broken"
    assert len(restore_points.list_restore_points()) == 3
    assert restore_points.cleanup_status()["pending"] is True


@pytest.mark.parametrize(
    "damage",
    [
        {"revision": 5},
        {"revision": "not-a-revision"},
        {"failures": True},
        {"failures": 99},
        {"since": "unknown"},
        {"next_at": -1},
        {"operation": "unknown"},
    ],
)
def test_malformed_retry_shape_cannot_be_reinterpreted_as_a_new_budget(copies, damage):
    import json

    manual()
    context = maintenance._context("manual", load_config())
    row = {
        "revision": context.revision,
        "failures": 1,
        "since": NOW,
        "next_at": NOW + 600,
        "reason": "remove_failed",
        **damage,
    }
    retry = paths.data_dir() / maintenance.STATE_NAME
    retry.write_text(json.dumps({"manual": row}), encoding="utf-8")
    original = retry.read_bytes()
    assert maintenance.run(now=NOW + 1000)["manual"] == "retry_record_unknown"
    assert retry.read_bytes() == original
    assert len(restore_points.list_restore_points()) == 3
