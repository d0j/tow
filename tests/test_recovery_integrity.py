"""Recovery must reject damaged input before changing any live store."""

from __future__ import annotations

import json

import pytest

from tow import snapshots, store
from tow.paths import data_dir, download_history_path, state_path


@pytest.mark.parametrize("number", [float("nan"), float("inf"), -float("inf")])
@pytest.mark.parametrize("undo", [False, True])
def test_invalid_secret_write_preserves_the_previous_encrypted_file(number, undo):
    write = store.save_secret_undo if undo else store.save_secrets
    target = store.secret_undo_path() if undo else store.encrypted_secrets_path()
    write({"fixture": "before"})
    before = target.read_bytes()

    with pytest.raises(store.SecretStoreError):
        write({"fixture": number})

    assert target.read_bytes() == before


@pytest.mark.parametrize("payload", [{"fixture": (1, 2)}, {1: "fixture"}, {None: "fixture"}])
@pytest.mark.parametrize("undo", [False, True])
def test_secret_payload_that_changes_when_encoded_never_overwrites_previous_bytes(payload, undo):
    write = store.save_secret_undo if undo else store.save_secrets
    target = store.secret_undo_path() if undo else store.encrypted_secrets_path()
    write({"fixture": "before"})
    before = target.read_bytes()
    with pytest.raises(store.SecretStoreError):
        write(payload)
    assert target.read_bytes() == before


def test_night_rollback_checks_all_backups_before_changing_any_store():
    store.save_state({"topics": [{"id": "before"}]})
    store.save_download_history({"topics": {"fixture": {"items": {}}}})
    paths = [state_path(), download_history_path()]
    with store.persistence_lock():
        safety = snapshots._begin_restore([(p.name, p, b"new") for p in paths], snapshot="fixture")
        for target in paths:
            target.write_bytes(b'{"fixture":"current"}')
        (safety / paths[-1].name).write_bytes(b"damaged backup")
        before = [target.read_bytes() for target in paths]

        with pytest.raises(snapshots.SnapshotError):
            snapshots._roll_back(safety)

        assert [target.read_bytes() for target in paths] == before
        assert (data_dir() / snapshots._MARKER).is_file()


@pytest.mark.parametrize("status", [[], {}, None, 1])
def test_malformed_night_status_has_a_typed_error_and_keeps_marker(status):
    safety = data_dir() / "before-restore-20261001-000000"
    safety.mkdir()
    (safety / snapshots._JOURNAL).write_text(
        json.dumps({"format": snapshots._JOURNAL_FORMAT, "status": status, "entries": []}), encoding="utf-8"
    )
    marker = data_dir() / snapshots._MARKER
    marker.write_text(json.dumps({"safety": safety.name}), encoding="utf-8")
    with pytest.raises(snapshots.SnapshotError), store.persistence_lock():
        pass
    assert marker.is_file()


def test_unreadable_old_safety_copy_is_not_pruned():
    safety = data_dir() / "before-restore-20261001-000000"
    safety.mkdir()
    journal = safety / snapshots._JOURNAL
    journal.write_bytes(b"{broken")
    foreign = safety / "keep.txt"
    foreign.write_bytes(b"do not erase")
    assert snapshots._tidy_safety_copies(keep=0) == []
    assert foreign.read_bytes() == b"do not erase"


@pytest.mark.parametrize("raw", [b'{"x":NaN}', b'{"x":Infinity}', b'{"x":1e9999}', b'{"x":' + b"9" * 5000 + b"}"])
def test_encrypted_secret_payload_parser_limits_are_typed(raw):
    cipher = store.master_fernet()
    envelope = json.dumps(
        {"format": "tow-secrets/v1", "cipher": "fernet", "token": cipher.encrypt(raw).decode("ascii")}
    )
    with pytest.raises(store.SecretStoreError):
        store.decrypt_secrets_bytes(envelope.encode("utf-8"))


@pytest.mark.parametrize("outer", [False, True])
def test_encrypted_secret_nesting_limit_is_typed(outer):
    raw = b'{"x":' + b"[" * 129 + b"0" + b"]" * 129 + b"}"
    if not outer:
        cipher = store.master_fernet()
        raw = json.dumps(
            {"format": "tow-secrets/v1", "cipher": "fernet", "token": cipher.encrypt(raw).decode("ascii")}
        ).encode()
    with pytest.raises(store.SecretStoreError):
        store.decrypt_secrets_bytes(raw)


def test_too_deep_secret_write_preserves_previous_bytes():
    store.save_secrets({"fixture": "before"})
    target = store.encrypted_secrets_path()
    before = target.read_bytes()
    value = 0
    for _ in range(129):
        value = [value]
    with pytest.raises(store.SecretStoreError):
        store.save_secrets({"fixture": value})
    assert target.read_bytes() == before


def test_finished_safety_copy_with_foreign_file_is_not_pruned():
    safety = data_dir() / "before-restore-20261001-000000"
    safety.mkdir()
    snapshots._write_journal(safety, {"format": snapshots._JOURNAL_FORMAT, "entries": []}, "committed")
    (safety / "keep.txt").write_bytes(b"not owned")
    assert snapshots._tidy_safety_copies(keep=0) == []
    assert (safety / "keep.txt").read_bytes() == b"not owned"


def test_night_restore_reservation_failure_never_removes_another_writers_files(monkeypatch):
    from pathlib import Path

    original = Path.mkdir
    reserved = []

    def reserve(path, *args, **kwargs):
        original(path, *args, **kwargs)
        if path.name.startswith("before-restore-"):
            reserved.append(path)
            (path / "keep.txt").write_bytes(b"another writer")
            raise FileExistsError("reserved elsewhere")

    monkeypatch.setattr(Path, "mkdir", reserve)
    with pytest.raises(snapshots.SnapshotError):
        snapshots._begin_restore([], snapshot="fixture")
    assert (reserved[0] / "keep.txt").read_bytes() == b"another writer"


def test_night_restore_verifies_copies_before_publishing_marker(monkeypatch):
    store.save_state({"topics": [{"id": "before"}]})
    target = state_path()
    before = target.read_bytes()
    write = snapshots.atomic_write_bytes

    def corrupt(path, content):
        write(
            path,
            b"damaged" if path.name == "state.json" and path.parent.name.startswith("before-restore-") else content,
        )

    monkeypatch.setattr(snapshots, "atomic_write_bytes", corrupt)
    with store.persistence_lock(), pytest.raises(snapshots.SnapshotError):
        snapshots._begin_restore([("state.json", target, b"new")], snapshot="fixture")
    assert target.read_bytes() == before
    assert not (data_dir() / snapshots._MARKER).exists()


def test_night_rollback_uses_verified_bytes_if_a_later_backup_changes(monkeypatch):
    store.save_state({"topics": [{"id": "before"}]})
    store.save_download_history({"topics": {"fixture": {"items": {}}}})
    paths = [state_path(), download_history_path()]
    saved = [p.read_bytes() for p in paths]
    with store.persistence_lock():
        safety = snapshots._begin_restore([(p.name, p, b"new") for p in paths], snapshot="fixture")
        for path in paths:
            path.write_bytes(b'{"fixture":"current"}')
        write = snapshots.atomic_write_bytes

        def change_later(path, content):
            if path == paths[0]:
                (safety / paths[1].name).write_bytes(b"changed after verification")
            write(path, content)

        monkeypatch.setattr(snapshots, "atomic_write_bytes", change_later)
        snapshots._roll_back(safety)
        assert [p.read_bytes() for p in paths] == saved
        assert not (data_dir() / snapshots._MARKER).exists()


def test_export_cannot_replace_quarantined_history_with_an_empty_backup(tmp_path):
    from tow.bundle import ExportImportError, export_bundle

    store.save_state({"topics": []})
    history = download_history_path()
    history.write_bytes(b"{broken")
    with pytest.raises(store.StoreCorruptionError):
        store.load_download_history()
    copies = list(history.parent.glob("download_history.json.corrupt-*"))
    assert len(copies) == 1
    output = tmp_path / "fixture.towx"
    with pytest.raises(ExportImportError):
        export_bundle(output, "fixture-passphrase")
    assert not output.exists()
    assert copies[0].read_bytes() == b"{broken"


@pytest.mark.parametrize(("field", "value"), [("status", []), ("status", {}), ("nested", [0] * 2)])
def test_damaged_import_transaction_does_not_allow_a_later_write(field, value):
    from tow import bundle

    store.save_state({"topics": [{"id": "before"}]})
    with store.persistence_lock():
        checkpoint = bundle._create_import_checkpoint()
        marker = checkpoint / "TRANSACTION.json"
        raw = json.loads(marker.read_bytes())
        if field == "nested":
            marker.write_bytes(b'{"nested":' + b"[" * 129 + b"0" + b"]" * 129 + b"}")
        else:
            raw[field] = value
            marker.write_text(json.dumps(raw), encoding="utf-8")
    before = state_path().read_bytes()
    with pytest.raises(bundle.ExportImportError):
        store.save_state({"topics": []})
    assert state_path().read_bytes() == before
    assert marker.is_file()


@pytest.mark.parametrize("value", [[], {}, None])
def test_bad_import_checkpoint_member_is_rejected_without_hash_type_escape(value):
    from tow import bundle

    store.save_state({"topics": [{"id": "before"}]})
    with store.persistence_lock():
        checkpoint = bundle._create_import_checkpoint()
        manifest = checkpoint / "MANIFEST.json"
        raw = json.loads(manifest.read_bytes())
        raw["targets"][0]["member"] = value
        manifest.write_text(json.dumps(raw), encoding="utf-8")
        with pytest.raises(bundle.ExportImportError):
            bundle._read_checkpoint(checkpoint)


def test_import_checks_new_backup_before_publishing_transaction(monkeypatch):
    from tow import bundle

    store.save_state({"topics": [{"id": "before"}]})
    before = state_path().read_bytes()
    write = bundle._atomic_write

    def corrupt(path, content):
        write(path, b"damaged" if path.name == "state.json" and path.parent.name == "files" else content)

    monkeypatch.setattr(bundle, "_atomic_write", corrupt)
    with store.persistence_lock(), pytest.raises(bundle.ExportImportError):
        bundle._create_import_checkpoint()
    assert state_path().read_bytes() == before
    assert not list(data_dir().glob("import-checkpoints/*/TRANSACTION.json"))


def test_finished_import_checkpoint_with_foreign_file_is_not_pruned():
    from tow import bundle

    with store.persistence_lock():
        checkpoint = bundle._create_import_checkpoint()
        bundle._write_import_transaction(checkpoint, status="committed")
    (checkpoint / "keep.txt").write_bytes(b"not owned")
    assert bundle.prune_import_checkpoints(keep=0) == 0
    assert (checkpoint / "keep.txt").read_bytes() == b"not owned"


def test_night_rollback_checks_later_target_before_touching_first_file():
    store.save_state({"topics": [{"id": "before"}]})
    store.save_download_history({"topics": {}})
    paths = [state_path(), download_history_path()]
    with store.persistence_lock():
        safety = snapshots._begin_restore([(p.name, p, b"new") for p in paths], snapshot="fixture")
        paths[0].write_bytes(b'{"fixture":"current"}')
        paths[1].unlink()
        paths[1].mkdir()
        before = paths[0].read_bytes()
        with pytest.raises(snapshots.SnapshotError):
            snapshots._roll_back(safety)
        assert paths[0].read_bytes() == before
        assert paths[1].is_dir()


def test_night_rollback_resolves_points_from_saved_config_without_writing_it(tmp_path):
    from tow.config import load_config, save_config
    from tow.paths import config_path

    old_root = tmp_path / "old-points"
    old_root.mkdir()
    cfg = load_config()
    cfg["restore_points_dir"] = str(old_root)
    save_config(cfg)
    point = old_root / "20261001T000000Z-deadbeef.towx"
    point.write_bytes(b"old point")
    with store.persistence_lock():
        safety = snapshots._begin_restore(
            [("config.yaml", config_path(), b"new"), (f"restore-points/{point.name}", point, b"new")],
            snapshot="fixture",
        )
        import yaml

        cfg["restore_points_dir"] = str(tmp_path / "other-points")
        config_path().write_text(yaml.safe_dump(cfg), encoding="utf-8")
        point.write_bytes(b"current point")
        plan = snapshots._rollback_plan(safety)
        assert plan[1][1] == point
        assert b"other-points" in config_path().read_bytes()  # preflight is read-only
        snapshots._roll_back(safety)
        assert point.read_bytes() == b"old point"
