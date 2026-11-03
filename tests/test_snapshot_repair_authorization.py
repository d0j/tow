"""Night-copy restoration never revives permission to change a torrent client."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from tow.config import load_config, save_config
from tow.paths import data_dir, state_path
from tow.repair_authorization import without_authorizations
from tow.snapshots import SnapshotError, create_snapshot, restore_snapshot
from tow.snapshots import restore as night_restore
from tow.store import load_download_history, load_state, save_download_history, save_secrets, save_state


def source(tmp_path):
    config = load_config()
    config["backup_dir"] = str(tmp_path.parent / f"{tmp_path.name}-night")
    save_config(config)
    topic = {
        "id": "synthetic-show",
        "title": "Synthetic Show",
        "hash": "a" * 40,
        "save_path": str(tmp_path / "media"),
        "selection": {"mode": "episodes", "value": "1-2"},
        "paused": True,
        "repair_pending": {"phase": "checking", "blocked": True},
        "repair_request_revision": "old-authorization",
    }
    save_state(
        {"topics": [topic], "undo": {"kind": "topic", "ts": "2026-09-12T20:00:00+00:00", "item": deepcopy(topic)}}
    )
    history = {"schema_version": 1, "topics": {topic["id"]: {"items": {"one": {"status": "completed"}}}}}
    save_download_history(history)
    save_secrets({})
    snapshot = Path(create_snapshot()["snapshot"])
    return snapshot, topic, history


def test_night_restore_drops_archived_repairs_and_undo_permission_but_preserves_data_and_intent(tmp_path):
    snapshot, topic, history = source(tmp_path)
    save_state({"topics": [{"id": "live-topic"}]})

    assert restore_snapshot(snapshot)["applied"] is False
    assert load_state()["topics"] == [{"id": "live-topic"}]
    assert restore_snapshot(snapshot, apply=True)["applied"] is True

    restored = load_state()
    expected = {key: value for key, value in topic.items() if key not in {"repair_pending", "repair_request_revision"}}
    assert restored["topics"] == [expected]
    assert restored["undo"]["item"] == expected
    assert load_download_history() == history


def test_a_failed_night_restore_compensates_with_the_live_authorization_unchanged(tmp_path, monkeypatch):
    snapshot, _topic, _history = source(tmp_path)
    live = {"id": "current-show", "repair_pending": {"phase": "checking"}, "repair_request_revision": "current-request"}
    save_state({"topics": [live]})
    before = state_path().read_bytes()
    original = night_restore._finish
    fail_once = [True]

    def refuse_commit(safety, status):
        if status == "committed" and fail_once[0]:
            fail_once[0] = False
            raise OSError("synthetic final commit refusal")
        return original(safety, status)

    monkeypatch.setattr(night_restore, "_finish", refuse_commit)

    with pytest.raises(SnapshotError):
        restore_snapshot(snapshot, apply=True)

    assert state_path().read_bytes() == before
    assert load_state()["topics"] == [live]
    assert not (data_dir() / ".tow-night-restore.json").exists()


def test_snapshot_state_without_repair_authorization_keeps_its_original_bytes():
    original = b'{"topics":[{"id":"quiet"}],"undo":null}\n'

    assert without_authorizations(original) == original
