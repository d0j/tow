"""Operator rollback restores data; compensation restores the exact still-live operation."""

from __future__ import annotations

from copy import deepcopy

import pytest

from tow.bundle import ExportImportError, export_bundle, import_bundle, importer, rollback_import
from tow.bundle import checkpoint as checkpoints
from tow.paths import download_history_path, state_path
from tow.store import load_state, save_download_history, save_secrets, save_state


def topic(name, revision):
    return {
        "id": name,
        "title": "Synthetic Show",
        "paused": True,
        "selection": {"mode": "episodes", "value": "1-2"},
        "repair_pending": {"phase": "checking", "blocked": True},
        "repair_request_revision": revision,
    }


def completed_checkpoint():
    old = topic("archived-show", "old-request")
    save_state({"topics": [old], "undo": {"kind": "topic", "ts": "2026-09-12T20:00:00+00:00", "item": deepcopy(old)}})
    save_download_history({"schema_version": 1, "topics": {}})
    save_secrets({})
    checkpoint = checkpoints.create_import_checkpoint()
    checkpoints.write_import_transaction(checkpoint, status="committed")
    return checkpoint, old


def test_public_checkpoint_rollback_discards_historical_repairs_and_live_undo_authorization():
    checkpoint, old = completed_checkpoint()
    save_state({"topics": [{"id": "current-show"}]})

    assert rollback_import(checkpoint)["preview"] is True
    assert load_state()["topics"] == [{"id": "current-show"}]
    assert rollback_import(checkpoint, apply=True)["ok"] is True

    expected = {key: value for key, value in old.items() if key not in {"repair_pending", "repair_request_revision"}}
    assert load_state()["topics"] == [expected]
    assert load_state()["undo"]["item"] == expected


def test_internal_checkpoint_compensation_preserves_the_previous_live_authorization_exactly():
    checkpoint, _old = completed_checkpoint()
    before = state_path().read_bytes()
    save_state({"topics": [{"id": "partially-imported-show"}]})

    assert rollback_import(checkpoint, apply=True, preserve_repair_authorization=True)["ok"] is True

    assert state_path().read_bytes() == before


@pytest.mark.parametrize("preserve", [False, True])
def test_interrupted_rollback_keeps_its_requested_authorization_policy(monkeypatch, preserve):
    checkpoint, old = completed_checkpoint()
    checkpoint_state = state_path().read_bytes()
    save_state({"topics": [{"id": "current-show"}]})
    real_write = checkpoints.atomic_write

    def lose_process_after_state(path, content):
        real_write(path, content)
        if path == state_path():
            raise KeyboardInterrupt("synthetic process exit after state write")

    with monkeypatch.context() as crash:
        crash.setattr(checkpoints, "atomic_write", lose_process_after_state)
        with pytest.raises(KeyboardInterrupt):
            rollback_import(checkpoint, apply=True, preserve_repair_authorization=preserve)

    assert checkpoints._read_import_transaction(checkpoint)["forget_repairs"] is (not preserve)
    checkpoints.recover_import_transactions()
    expected = (
        old
        if preserve
        else {key: value for key, value in old.items() if key not in {"repair_pending", "repair_request_revision"}}
    )
    assert load_state()["topics"] == [expected]
    assert load_state()["undo"]["item"] == expected
    if preserve:
        assert state_path().read_bytes() == checkpoint_state


def test_failed_public_rollback_compensates_with_current_authorization_unchanged(monkeypatch):
    checkpoint, _old = completed_checkpoint()
    current = topic("current-show", "current-request")
    save_state({"topics": [current]})
    save_download_history({"schema_version": 1, "topics": {"live": {"items": {}}}})
    before = state_path().read_bytes()
    real_write = checkpoints.atomic_write
    fail_once = [True]

    def fail_history(path, content):
        if path == download_history_path() and fail_once[0]:
            fail_once[0] = False
            raise OSError("synthetic rollback write refusal")
        return real_write(path, content)

    monkeypatch.setattr(checkpoints, "atomic_write", fail_history)

    with pytest.raises(ExportImportError, match="destination preserved"):
        rollback_import(checkpoint, apply=True)

    assert state_path().read_bytes() == before
    assert load_state()["topics"] == [current]


def test_failed_import_uses_compensation_mode_instead_of_operator_rollback(tmp_path, monkeypatch):
    save_state({"topics": [{"id": "incoming-show"}]})
    save_download_history({"schema_version": 1, "topics": {}})
    save_secrets({})
    bundle = tmp_path / "incoming.towx"
    export_bundle(bundle, "synthetic-passphrase")
    current = topic("current-show", "current-request")
    save_state({"topics": [current]})
    before = state_path().read_bytes()
    monkeypatch.setattr(
        importer,
        "_destination_readback",
        lambda *_args: (_ for _ in ()).throw(ExportImportError("synthetic read-back refusal")),
    )

    with pytest.raises(ExportImportError, match="synthetic read-back refusal"):
        import_bundle(bundle, "synthetic-passphrase", apply=True)

    assert state_path().read_bytes() == before
    assert load_state()["topics"] == [current]
