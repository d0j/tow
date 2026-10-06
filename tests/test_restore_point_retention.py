from __future__ import annotations

from pathlib import Path

import pytest

from tow import bundle, restore_points
from tow.bundle import ExportImportError
from tow.store import save_secrets, save_state


def _seed() -> None:
    save_state({"topics": [], "mirrors": {}})
    save_secrets({})


def test_a_colliding_restore_point_is_never_deleted(monkeypatch):
    _seed()
    root = restore_points.restore_points_dir()
    root.mkdir(parents=True)
    existing = root / "20260101T000000Z-deadbeef.towx"
    existing.write_bytes(b"existing restore point")
    monkeypatch.setattr(restore_points, "point_path", lambda _id, **_kw: existing)

    with pytest.raises(restore_points.RestorePointError):
        restore_points.create_restore_point()

    assert existing.read_bytes() == b"existing restore point"


def test_a_failed_export_never_deletes_a_file_created_by_another_writer(monkeypatch):
    _seed()
    target: list[Path] = []

    def fail_export(path, *_args, **_kwargs):
        target.append(path)
        path.write_bytes(b"other writer")
        raise ExportImportError("export refused")

    monkeypatch.setattr(restore_points, "export_bundle", fail_export)
    with pytest.raises(restore_points.RestorePointError):
        restore_points.create_restore_point()

    assert target[0].read_bytes() == b"other writer"


def test_failed_pruning_keeps_the_verified_new_point_and_reports_warning(monkeypatch):
    _seed()

    def fail_prune(**_kwargs):
        raise restore_points.RestorePointError("cannot rotate", kind=restore_points.CREATE_FAILED)

    monkeypatch.setattr(restore_points, "_prune", fail_prune)
    made = restore_points.create_restore_point()
    assert made["cleanup_warning"]
    assert restore_points.point_path(made["id"]).is_file()
    assert restore_points.restore_from_point(made["id"])["ok"] is True


def test_rotation_keeps_foreign_or_damaged_restore_point_files(monkeypatch):
    _seed()
    root = restore_points.restore_points_dir()
    root.mkdir(parents=True)
    foreign = [root / f"2026010{n}T000000Z-deadbeef.towx" for n in (1, 2, 3)]
    for path in foreign:
        path.write_bytes(b"foreign or damaged copy")
    monkeypatch.setattr(restore_points, "RESTORE_POINT_LIMIT", 2)

    made = restore_points.create_restore_point()

    assert restore_points.point_path(made["id"]).is_file()
    assert all(path.read_bytes() == b"foreign or damaged copy" for path in foreign)


def test_rotation_only_counts_verified_copies(monkeypatch):
    _seed()
    monkeypatch.setattr(restore_points, "RESTORE_POINT_LIMIT", 2)
    first = restore_points.create_restore_point()
    second = restore_points.create_restore_point()
    third = restore_points.create_restore_point(protected={first["id"]})
    assert restore_points.point_path(first["id"]).is_file()
    assert restore_points.point_path(third["id"]).is_file()
    assert not restore_points.point_path(second["id"], must_exist=False).exists()


def test_rotation_decrypts_only_the_points_it_removes(monkeypatch):
    _seed()
    monkeypatch.setattr(restore_points, "RESTORE_POINT_LIMIT", 3)
    made = [restore_points.create_restore_point()["id"] for _ in range(3)]
    checked: list[str] = []
    real = restore_points.verify_bundle

    def counted(path, passphrase):
        checked.append(Path(path).stem)
        return real(path, passphrase)

    monkeypatch.setattr(restore_points, "verify_bundle", counted)
    newest = restore_points.create_restore_point()["id"]

    assert checked == [made[0]]  # one key derivation, not one per kept point
    assert not restore_points.point_path(made[0], must_exist=False).exists()
    assert {point["id"] for point in restore_points.list_restore_points()} == {*made[1:], newest}


def test_web_reports_saved_copy_with_cleanup_warning(monkeypatch):
    from fastapi.testclient import TestClient
    from helpers import shown

    from tow.i18n import t
    from tow.web import app, services

    monkeypatch.setattr(services, "create_restore_point", lambda: {"id": "synthetic", "cleanup_warning": "pending"})
    with TestClient(app, headers={"Origin": "http://127.0.0.1"}) as client:
        response = client.post("/settings/restore-points", follow_redirects=False)
    assert response.status_code == 303
    assert t("backup.restore_point.cleanup_warning") in shown(response.headers["location"])


def test_export_never_overwrites_a_file_created_during_preparation(monkeypatch, tmp_path):
    _seed()
    target = tmp_path / "collision.towx"
    original = bundle._build_export_members

    def concurrent_writer(**kwargs):
        result = original(**kwargs)
        target.write_bytes(b"another writer owns this file")
        return result

    monkeypatch.setattr(bundle, "_build_export_members", concurrent_writer)
    with pytest.raises(ExportImportError):
        bundle.export_bundle(target, "synthetic-passphrase")
    assert target.read_bytes() == b"another writer owns this file"


def test_read_back_failure_never_deletes_a_replacement(monkeypatch, tmp_path):
    _seed()
    target = tmp_path / "replacement.towx"

    def replaced(path, _passphrase):
        path.unlink()
        path.write_bytes(b"replacement belongs to someone else")
        raise ExportImportError("read back failed")

    monkeypatch.setattr(bundle, "_read_bundle", replaced)
    with pytest.raises(ExportImportError):
        bundle.export_bundle(target, "synthetic-passphrase")
    assert target.read_bytes() == b"replacement belongs to someone else"


@pytest.mark.parametrize("name", ["linux", "macos"])
def test_posix_exclusive_publication_preserves_an_existing_file(tmp_path, name):
    from tow.platform.posix import PosixBackend

    source, destination = tmp_path / "prepared", tmp_path / "published"
    source.write_bytes(b"prepared bytes")
    destination.write_bytes(b"existing bytes")
    with pytest.raises(FileExistsError):
        PosixBackend(name).publish_exclusive(source, destination)
    assert destination.read_bytes() == b"existing bytes"
    destination.unlink()
    PosixBackend(name).publish_exclusive(source, destination)
    assert destination.read_bytes() == b"prepared bytes"
