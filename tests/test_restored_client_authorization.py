"""An archived automatic handover is data, never a fresh request to start a client task."""

from __future__ import annotations

from copy import deepcopy

import pytest
from test_client_verification import H
from test_client_verification import _run as verify_run
from test_client_verification import world as verification_world

from tow import check, repair_authorization, undo
from tow.bundle import ExportImportError, export_bundle, import_bundle, importer, rollback_import
from tow.bundle import checkpoint as checkpoints
from tow.check.rows import client_marks
from tow.config import load_config, save_config
from tow.paths import state_path
from tow.snapshots import create_snapshot, restore_snapshot
from tow.store import load_state, save_download_history, save_secrets, save_state


def archived_topic(kind, path):
    topic = {
        "id": "archived",
        "title": "Synthetic Show",
        "hash": H,
        "url": "https://tracker.example/1",
        "save_path": str(path),
        "client_id": "main",
        "selection": {"mode": "all"},
        "last_check": "2026-09-12T20:00:00+00:00",
    }
    if kind == "space":
        topic["waiting_space"] = {
            "hash": H,
            "client": "main",
            "selection": "verified-native-choice",
            "since": "2026-09-12T19:00:00+00:00",
            "needed": 100,
            "free": 0,
        }
    else:
        topic["verification_pending"] = {
            "hash": H,
            "client": "main",
            "path": str(path),
            "owner": list(client_marks(topic)),
            "since": "2026-09-12T19:00:00+00:00",
            "resume_attempted": False,
            "receipt": {
                "hash": H,
                "wanted": [0],
                "files": [{"index": 0, "name": "Show/S01E01.mkv", "size": 10, "priority": 1}],
            },
        }
    return topic


def assert_frozen(topic):
    assert not topic.get("waiting_space")
    waiting = topic["verification_pending"]
    assert waiting["blocked"] == "check.restored_confirmation"
    assert waiting["repair_only"] is True
    assert waiting.get("resume_attempted") is not True
    assert waiting["owner"] == list(client_marks(topic))  # the existing four-field reader contract


@pytest.mark.parametrize("kind", ["space", "verification"])
def test_forget_freezes_legacy_handover_and_undo_without_fabricating_a_start(tmp_path, kind):
    original = archived_topic(kind, tmp_path)
    state = {"topics": [deepcopy(original)], "undo": {"kind": "topic", "item": deepcopy(original)}}
    assert repair_authorization.forget(state)
    for topic in (state["topics"][0], state["undo"]["item"]):
        assert_frozen(topic)
        assert topic["last_check"] == original["last_check"]
        assert topic["selection"] == original["selection"]
        assert topic["hash"] == original["hash"]
    assert not repair_authorization.forget(state)  # repeated recovery is stable


def test_empty_legacy_verification_dto_does_not_hide_a_real_space_wait(tmp_path):
    topic = archived_topic("space", tmp_path)
    topic["verification_pending"] = {}
    repair_authorization.forget({"topics": [topic]})
    assert_frozen(topic)
    assert topic["verification_pending"]["hash"] == H
    assert topic["verification_pending"]["client"] == "main"
    assert topic["verification_pending"]["path"] == str(tmp_path)


@pytest.mark.parametrize("kind", ["space", "verification"])
def test_requested_checkpoint_restore_freezes_historical_auto_start_but_compensation_preserves_bytes(tmp_path, kind):
    original = archived_topic(kind, tmp_path)
    save_state({"topics": [original]})
    save_download_history({"schema_version": 1, "topics": {}})
    save_secrets({})
    point = checkpoints.create_import_checkpoint()
    checkpoints.write_import_transaction(point, status="committed")
    exact = state_path().read_bytes()
    save_state({"topics": [{"id": "current"}]})
    rollback_import(point, apply=True, preserve_repair_authorization=True)
    assert state_path().read_bytes() == exact
    save_state({"topics": [{"id": "current"}]})
    rollback_import(point, apply=True)
    assert_frozen(load_state()["topics"][0])


@pytest.mark.parametrize("kind", ["space", "verification"])
def test_requested_bundle_import_uses_the_same_historical_permission_policy(tmp_path, kind):
    topic = archived_topic(kind, tmp_path / "media")
    save_state({"topics": [topic]})
    save_download_history({"schema_version": 1, "topics": {}})
    save_secrets({})
    bundle = tmp_path / "synthetic.towx"
    export_bundle(bundle, "synthetic-passphrase")
    save_state({"topics": []})
    import_bundle(bundle, "synthetic-passphrase", apply=True)
    assert_frozen(load_state()["topics"][0])


@pytest.mark.parametrize("kind", ["space", "verification"])
def test_requested_night_restore_does_not_revive_an_auto_start(tmp_path, kind):
    config = load_config()
    config["backup_dir"] = str(tmp_path.parent / f"{tmp_path.name}-night")
    save_config(config)
    topic = archived_topic(kind, tmp_path / "media")
    save_state({"topics": [topic]})
    save_download_history({"schema_version": 1, "topics": {}})
    save_secrets({})
    snapshot = create_snapshot()["snapshot"]
    save_state({"topics": []})
    restore_snapshot(snapshot, apply=True)
    assert_frozen(load_state()["topics"][0])


@pytest.mark.parametrize("kind", ["space", "verification"])
def test_undo_of_a_deleted_topic_restores_data_without_archived_auto_start(tmp_path, kind):
    topic = archived_topic(kind, tmp_path)
    state = {"topics": []}
    undo.stamp(state, "topic", item=topic, index=0)
    save_state(state)
    undo.apply(local=True)
    assert_frozen(load_state()["topics"][0])


def test_restored_legacy_receipt_needs_a_targeted_row_check_not_check_all_or_background(tmp_path, monkeypatch):
    client, _tracker, _sent = verification_world.__wrapped__(monkeypatch, tmp_path)
    verify_run()
    state = load_state()
    waiting = state["topics"][0]["verification_pending"]
    waiting.pop("client_generation", None)  # a pre-reservation-format stored receipt
    waiting.pop("resume_attempted", None)
    repair_authorization.forget(state)
    save_state(state)
    client.end_check(found=0.3, dropped_start=True)
    client.calls.clear()
    for scope in ({}, {"progress_only": True}, {"space_only": True}):
        check.run_check(apply=True, notify=False, how="manual", **scope)
        assert client.calls == []
        assert load_state()["topics"][0]["verification_pending"]["blocked"] == "check.restored_confirmation"
    row = check.run_check(apply=True, notify=False, how="manual", ids=["slow"])["results"][0]
    assert row["ok"]
    assert "verification_pending" not in load_state()["topics"][0]
    assert any(call[0] == "start" for call in client.calls)


@pytest.mark.parametrize("attempted", [False, True])
def test_failed_import_compensation_keeps_live_handover_flags_and_exact_bytes(tmp_path, monkeypatch, attempted):
    save_state({"topics": [{"id": "incoming"}]})
    save_download_history({"schema_version": 1, "topics": {}})
    save_secrets({})
    incoming = tmp_path / "incoming.towx"
    export_bundle(incoming, "synthetic-passphrase")
    live = archived_topic("verification", tmp_path / "media")
    live["verification_pending"]["resume_attempted"] = attempted
    save_state({"topics": [live]})
    before = state_path().read_bytes()
    monkeypatch.setattr(
        importer,
        "_destination_readback",
        lambda *_args: (_ for _ in ()).throw(ExportImportError("synthetic read-back failure")),
    )
    with pytest.raises(ExportImportError, match="synthetic read-back failure"):
        import_bundle(incoming, "synthetic-passphrase", apply=True)
    assert state_path().read_bytes() == before
    assert load_state()["topics"][0]["verification_pending"]["resume_attempted"] is attempted


@pytest.mark.parametrize("kind", ["space", "verification"])
@pytest.mark.parametrize("preserve", [False, True])
def test_rollback_restart_keeps_requested_freeze_or_exact_compensation_policy(tmp_path, monkeypatch, kind, preserve):
    original = archived_topic(kind, tmp_path / "media")
    save_state({"topics": [original]})
    save_download_history({"schema_version": 1, "topics": {}})
    save_secrets({})
    point = checkpoints.create_import_checkpoint()
    checkpoints.write_import_transaction(point, status="committed")
    exact = state_path().read_bytes()
    save_state({"topics": []})
    write = checkpoints.atomic_write

    def interrupted(path, content):
        write(path, content)
        if path == state_path():
            raise KeyboardInterrupt("synthetic process interruption")

    with monkeypatch.context() as crash:
        crash.setattr(checkpoints, "atomic_write", interrupted)
        with pytest.raises(KeyboardInterrupt):
            rollback_import(point, apply=True, preserve_repair_authorization=preserve)
    checkpoints.recover_import_transactions()
    if preserve:
        assert state_path().read_bytes() == exact
    else:
        assert_frozen(load_state()["topics"][0])


def test_freezing_an_already_dispatched_archived_handover_preserves_its_true_attempt_history(tmp_path):
    topic = archived_topic("verification", tmp_path)
    topic["verification_pending"]["resume_attempted"] = True
    state = {"topics": [topic]}
    repair_authorization.forget(state)
    assert topic["verification_pending"]["resume_attempted"] is True
    assert topic["verification_pending"]["blocked"] == "check.restored_confirmation"
