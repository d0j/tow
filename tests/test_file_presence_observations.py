"""Missing-file evidence follows the current owner intent and stays shared across views."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient

from tow import check_steps, file_presence, platform
from tow.check import reconcile as check_reconcile
from tow.issues import Snapshot, fingerprint
from tow.issues.kinds import files_missing
from tow.store import save_download_history, save_state
from tow.web import app

FIRST = "2026-09-12T20:00:00+00:00"
SECOND = "2026-09-12T20:01:00+00:00"
CONFIG = {"clients": {"main": {"kind": "qbittorrent", "default": True}}}


def topic(**updates):
    return {
        "id": "presence-topic",
        "title": "Synthetic Show [5 of 24]",
        "url": "https://tracker.example/topic/1",
        "hash": "a" * 40,
        "save_path": "/downloads/show",
        "client_id": "main",
        "selection": {"mode": "all"},
        "selection_hash": "a" * 40,
        "selection_verified": True,
        **updates,
    }


def observe_missing(item, count=3, *, at=FIRST, client_id="main"):
    file_presence.observe(item, {"missing_files": count, "unverified_files": 0}, client_id=client_id, at=at)


def test_unchanged_problem_keeps_its_first_observation_and_issue_identity():
    item = topic()
    observe_missing(item)
    before = deepcopy(item["files_missing"])
    first_issues = files_missing.collect(Snapshot({"topics": [item]}, CONFIG, 1_800_000_000))

    observe_missing(item, at=SECOND)

    assert item["files_missing"] == before
    assert file_presence.count(item, CONFIG) == 3
    assert fingerprint(first_issues) == fingerprint(
        files_missing.collect(Snapshot({"topics": [item]}, CONFIG, 1_800_000_100))
    )
    assert item["files_missing"]["at"] == FIRST


def test_a_changed_confirmed_absence_count_refreshes_the_diagnostic_without_changing_topic_status():
    item = topic(last_ok=True, last_error="")
    observe_missing(item)

    observe_missing(item, 2, at=SECOND)

    assert file_presence.count(item, CONFIG) == 2
    assert item["files_missing"]["at"] == SECOND
    assert item["last_ok"] is True
    assert item["last_error"] == ""


def test_dictionary_order_and_unrelated_timer_edits_do_not_hide_the_same_missing_files():
    item = topic(selection={"mode": "episodes", "value": "1-5"}, check_interval_min=30)
    observe_missing(item)
    before = deepcopy(item["files_missing"])
    item["selection"] = {"value": "1-5", "mode": "episodes"}
    item["check_interval_min"] = 60

    observe_missing(item, at=SECOND)

    assert file_presence.count(item, CONFIG) == 3
    assert item["files_missing"] == before


@pytest.mark.parametrize(
    "change",
    [
        {"hash": "b" * 40},
        {"hash": None},
        {"client_id": "other"},
        {"save_path": "/downloads/elsewhere"},
        {"selection": {"mode": "episodes", "value": "4-5"}},
        {"selection_dirty": True},
        {"selection_hash": "b" * 40},
        {"selection_verified": False},
        {"file_aliases": {"Show/S01E01.mkv": "renamed.mkv"}},
        {"move_pending": {"from": "/downloads/show", "to": "/downloads/new"}},
    ],
)
def test_absence_from_a_previous_revision_or_owner_intent_is_not_current(change):
    item = topic()
    observe_missing(item)
    item.update(change)

    assert file_presence.count(item, CONFIG) == 0
    assert files_missing.collect(Snapshot({"topics": [item]}, CONFIG, 1_800_000_000)) == []


def test_a_default_client_change_invalidates_only_implicit_client_evidence():
    implicit = topic(client_id=None)
    explicit = topic()
    observe_missing(implicit)
    observe_missing(explicit)
    other_default = {"clients": {"main": {"kind": "qbittorrent"}, "other": {"kind": "deluge", "default": True}}}

    assert file_presence.count(implicit, other_default) == 0
    assert file_presence.count(explicit, other_default) == 3


@pytest.mark.parametrize(
    "result",
    [
        {},
        {"missing_files": 0},
        {"missing_files": 0, "unverified_files": 5},
        {"missing_files": True, "unverified_files": 0},
        {"missing_files": 0, "unverified_files": False},
        {"missing_files": -1, "unverified_files": 0},
        {"missing_files": 0, "unverified_files": -1},
        {"missing_files": "0", "unverified_files": 0},
    ],
)
def test_unknown_or_malformed_observation_does_not_claim_recovery(result):
    item = topic()
    observe_missing(item)
    before = deepcopy(item["files_missing"])

    file_presence.observe(item, result, client_id="main", at=SECOND)

    assert item["files_missing"] == before
    assert file_presence.count(item, CONFIG) == 3


def test_fully_known_healthy_observation_clears_diagnostic_and_check_merge_clears_disk_copy():
    item = topic()
    observe_missing(item)
    disk = {"topics": [deepcopy(item)]}

    file_presence.observe(item, {"missing_files": 0, "unverified_files": 0}, client_id="main", at=SECOND)
    check_steps.merge_check_results(disk, {"topics": [item]})

    assert "files_missing" not in item
    assert "files_missing" not in disk["topics"][0]


@pytest.mark.parametrize("evidence", [{"absent_files": 3}, {"absent_files": 0, "incomplete_files": 3}])
def test_invalidated_piece_cache_or_preallocation_is_not_a_file_recovery(evidence):
    item = topic()
    observe_missing(item)
    before = deepcopy(item["files_missing"])

    transition = file_presence.observe(
        item, {"missing_files": 0, "unverified_files": 0, **evidence}, client_id="main", at=SECOND
    )

    assert transition == ""
    assert item["files_missing"] == before
    assert file_presence.count(item, CONFIG) == 3


def test_partial_recovery_updates_remaining_count_without_repeating_the_missing_warning():
    item = topic()
    observe_missing(item)
    transition = file_presence.observe(
        item,
        {"missing_files": 0, "unverified_files": 0, "absent_files": 2, "incomplete_files": 2},
        client_id="main",
        at=SECOND,
    )
    assert transition == ""
    assert file_presence.count(item, CONFIG) == 2
    assert item["files_missing"]["at"] == FIRST


def test_an_inflight_progress_commit_cannot_erase_a_new_repair_request_or_resurrect_a_cancelled_one():
    checked = topic()
    disk_topic = deepcopy(checked)
    disk_topic.update(repair_request_revision="new-request", repair_pending={"id": "new-request"})
    disk = {"topics": [disk_topic]}
    check_steps.merge_check_results(disk, {"topics": [checked]}, edited={"presence-topic": {"repair_request_revision"}})
    assert disk_topic["repair_pending"] == {"id": "new-request"}

    checked.update(repair_request_revision="new-request", repair_pending={"id": "new-request", "phase": "checking"})
    disk_topic.pop("repair_pending")
    disk_topic["repair_request_revision"] = "cancelled"
    check_steps.merge_check_results(disk, {"topics": [checked]}, edited={"presence-topic": {"repair_request_revision"}})
    assert "repair_pending" not in disk_topic


@pytest.mark.parametrize("legacy", ["unknown legacy value", ["unknown"], 42])
def test_new_valid_absence_can_replace_malformed_imported_diagnostic(legacy):
    item = topic(files_missing=legacy)
    assert file_presence.count(item, CONFIG) == 0

    observe_missing(item)

    assert file_presence.count(item, CONFIG) == 3


@pytest.mark.parametrize(
    "change",
    [
        {"client_id": "other"},
        {"save_path": "/downloads/new"},
        {"selection": {"mode": "episodes", "value": "4-5"}},
        {"move_pending": {"from": "/downloads/show", "to": "/downloads/new"}},
    ],
)
def test_concurrent_owner_edit_does_not_attach_a_check_observation_to_new_intent(change):
    checked = topic()
    disk_topic = deepcopy(checked)
    disk_topic.update(change)
    observe_missing(checked)
    disk = {"topics": [disk_topic]}

    check_steps.merge_check_results(disk, {"topics": [checked]}, edited={"presence-topic": set(change)})

    assert file_presence.count(disk_topic, CONFIG) == 0
    assert all(disk_topic[key] == value for key, value in change.items())


@pytest.mark.parametrize("suppression", [{"paused": True}, {"volume_unseen": {"folder": "/downloads/show"}}])
def test_pause_or_unknown_volume_suppresses_attention_without_fabricating_recovery(suppression):
    item = topic()
    observe_missing(item)
    item.update(suppression)

    assert files_missing.collect(Snapshot({"topics": [item]}, CONFIG, 1_800_000_000)) == []
    assert file_presence.count(item, CONFIG) == 3
    assert "files_missing" in item


class ObservedClient:
    client_id = "main"
    client_kind = "fake"
    capabilities: ClassVar = {"inspect": True, "list_files": True}

    def __init__(self, folder, names):
        self.folder, self.names = folder, names
        self.inspect_calls = 0

    def inspect_torrent(self, infohash):
        self.inspect_calls += 1
        return {
            "hash": infohash,
            "save_path": str(self.folder),
            "state": "stoppedUP",
            "progress": 1.0,
            "files": [{"name": name, "size": 10, "progress": 1.0, "priority": 1} for name in self.names],
        }


@pytest.mark.parametrize("owner_state", [{}, {"paused": True}, {"tracking_mode": "once", "once_done": True}])
def test_reconcile_records_real_file_loss_and_preserves_owner_stop_intent(tmp_path, monkeypatch, owner_state):
    monkeypatch.setattr(platform, "volume_of", lambda path: platform.Volume("uuid:test-presence", "local"))
    names = [f"S01E{number:02d}.mkv" for number in range(1, 6)]
    for name in names:
        (tmp_path / name).write_bytes(b"0123456789")
    item = topic(save_path=str(tmp_path), **owner_state)
    client = ObservedClient(tmp_path, names)
    run = SimpleNamespace(get_client_for=lambda topic: ("main", client), notify=False, how="progress")
    history = {"topics": {}}
    row = {"id": item["id"], "ok": True, "status": "skipped"}
    records = []
    check_reconcile._reconcile_one(item, row, run, history, records)
    for name in names[:3]:
        (tmp_path / name).unlink()

    check_reconcile._reconcile_one(item, row, run, history, records)

    assert file_presence.count(item, CONFIG) == 3
    assert history["topics"][item["id"]]["summary"]["completed"] == 2
    assert client.inspect_calls == 2  # the adapter supports no mutating operation at all
    assert all(item[key] == value for key, value in owner_state.items())
    assert [kind for kind, _fields in records] == ["files_missing"]
    assert records[0][1]["n"] == 3


def test_independent_web_sessions_share_absence_warning_counter_and_detail_status(tmp_path):
    item = topic(save_path=str(tmp_path), last_ok=True, last_error="")
    observe_missing(item)
    save_state({"topics": [item], "health": {"qbit_ok": True, "check_ok": True}, "mirrors": {}})
    entries = {
        f"episode:s01e{number:02d}": {
            "label": f"S01E{number:02d}",
            "episode_key": f"episode:s01e{number:02d}",
            "status": "missing" if number <= 3 else "completed",
            "progress": 1.0,
        }
        for number in range(1, 6)
    }
    save_download_history(
        {"schema_version": 1, "topics": {item["id"]: {"expected": {"kind": "episodes", "total": 24}, "items": entries}}}
    )
    first, second = TestClient(app), TestClient(app)

    home_a, home_b = first.get("/"), second.get("/")
    details_a = first.get("/topics/presence-topic/downloads.json")
    details_b = second.get("/topics/presence-topic/downloads.json")
    panel_a = first.get("/topics/presence-topic/edit-panel")
    panel_b = second.get("/topics/presence-topic/edit-panel")

    for response in (home_a, home_b):
        assert response.status_code == 200
        assert 'data-issue-id="files_missing:presence-topic"' in response.text
        assert (
            "/?open=presence-topic&amp;fix=repair_files%3Apresence-topic&amp;spot=row-presence-topic" in response.text
        )
        assert "2/24" in response.text
    assert details_a.status_code == details_b.status_code == 200
    assert details_a.json() == details_b.json()
    assert details_a.json()["summary"]["completed"] == 2
    assert [row["status"] for row in details_a.json()["items"]] == ["missing"] * 3 + ["completed"] * 2
    assert "progress" in details_a.json()["items"][0]  # the explicit status must override the client's stale 100%
    for response in (panel_a, panel_b):
        assert response.status_code == 200
        assert 'class="download-details" href="/topics/presence-topic/downloads.json"' in response.text
        assert "home.topic.files_missing" not in response.text
