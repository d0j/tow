"""Current file presence cannot be inferred from old completions or client piece caches."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import ClassVar

import pytest

from tow import platform
from tow.progress import reconcile_topic

FIRST = "2026-09-12T20:00:00+00:00"
SECOND = "2026-09-12T20:01:00+00:00"
THIRD = "2026-09-12T20:02:00+00:00"


class Client:
    client_id = "fake-main"
    client_kind = "fake"
    capabilities: ClassVar = {"inspect": True, "list_files": True}

    def __init__(self, folder: Path, names: list[str]):
        self.present = True
        self.info = {
            "save_path": str(folder),
            "state": "stoppedUP",
            "progress": 1.0,
            "files": [{"name": name, "size": 10, "progress": 1.0, "priority": 1} for name in names],
        }

    def inspect_torrent(self, infohash):
        if not self.present:
            return None
        return {**deepcopy(self.info), "hash": infohash}


def world(tmp_path, monkeypatch, *, names=None):
    names = names or ["Show/S01E01.mkv"]
    current = {"volume": "uuid:test-disk"}
    monkeypatch.setattr(
        platform,
        "volume_of",
        lambda path: platform.Volume(current["volume"], "local") if current["volume"] else None,
    )
    for name in names:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"0123456789")
    topic = {"id": "show", "title": "Show S01 [5 of 24]", "hash": "a" * 40, "save_path": str(tmp_path)}
    history = {"topics": {"show": {"baseline_at": "2026-09-12T19:00:00+00:00", "items": {}}}}
    client = Client(tmp_path, names)
    first = reconcile_topic(topic, client, history, FIRST)
    assert first["summary"]["completed"] > 0
    return client, topic, history, current


def test_native_recheck_cannot_turn_missing_media_into_a_recovery_notification(tmp_path, monkeypatch):
    from tow import file_presence

    client, topic, history, _current = world(tmp_path, monkeypatch)
    target = tmp_path / "Show/S01E01.mkv"
    target.unlink()
    missing = reconcile_topic(topic, client, history, SECOND)
    assert file_presence.observe(topic, missing, client_id=client.client_id, at=SECOND) == "missing"

    client.info["progress"] = 0.0
    client.info["state"] = "stoppedDL"
    client.info["files"][0]["progress"] = 0.0
    incomplete = reconcile_topic(topic, client, history, THIRD)
    assert incomplete["missing_files"] == 0  # The client no longer claims the absent file complete.
    assert incomplete["absent_files"] == incomplete["incomplete_files"] == 1
    assert incomplete["selected_file_count"] == 1
    assert file_presence.observe(topic, incomplete, client_id=client.client_id, at=THIRD) == ""
    assert topic["files_missing"]["count"] == 1

    target.write_bytes(b"0123456789")  # Full-size preallocation still is not confirmed content.
    allocated = reconcile_topic(topic, client, history, THIRD)
    assert allocated["absent_files"] == 0
    assert allocated["incomplete_files"] == 1
    assert file_presence.observe(topic, allocated, client_id=client.client_id, at=THIRD) == ""

    client.info["files"][0]["progress"] = client.info["progress"] = 1.0
    restored = reconcile_topic(topic, client, history, THIRD)
    assert file_presence.observe(topic, restored, client_id=client.client_id, at=THIRD) == "restored"
    assert restored["events"] == []


def live_items(history):
    return [item for item in history["topics"]["show"]["items"].values() if not item.get("superseded")]


def test_removing_shared_old_revision_files_updates_all_current_series_on_the_next_pass(tmp_path, monkeypatch):
    names = [f"Show/S01E{number:02}.mkv" for number in range(1, 6)]
    client, topic, history, _current = world(tmp_path, monkeypatch, names=names)
    recorded = {item["identity"]: item["completed_observed_at"] for item in live_items(history)}
    for name in names[:3]:
        (tmp_path / name).unlink()

    result = reconcile_topic(topic, client, history, SECOND)

    assert result["summary"]["completed"] == 2
    assert result["summary"]["expected"] == 24
    assert result["summary"]["is_complete"] is False
    assert result["missing_files"] == 3
    assert result["unverified_files"] == 0
    assert [item["status"] for item in live_items(history)] == ["missing"] * 3 + ["completed"] * 2
    assert result["events"] == []
    assert {item["identity"]: item["completed_observed_at"] for item in live_items(history)} == recorded
    for name in names[:3]:
        (tmp_path / name).write_bytes(b"0123456789")

    restored = reconcile_topic(topic, client, history, THIRD)

    assert restored["summary"]["completed"] == 5
    assert restored["missing_files"] == restored["unverified_files"] == 0
    assert restored["events"] == []  # restoring presence does not announce each completion again


@pytest.mark.parametrize("change", ["remove", "rename", "parent_rename", "truncate", "directory"])
def test_a_recent_completion_is_invalidated_by_current_file_changes(tmp_path, monkeypatch, change):
    client, topic, history, _current = world(tmp_path, monkeypatch)
    target = tmp_path / "Show/S01E01.mkv"
    if change == "remove":
        target.unlink()
    elif change == "rename":
        target.rename(target.with_name("moved.mkv"))
    elif change == "parent_rename":
        target.parent.rename(tmp_path / "Other")
    elif change == "truncate":
        target.write_bytes(b"short")
    else:
        target.unlink()
        target.mkdir()

    result = reconcile_topic(topic, client, history, SECOND)

    assert result["summary"]["completed"] == 0
    assert live_items(history)[0]["status"] == "missing"
    assert live_items(history)[0]["completed_observed_at"] == FIRST
    assert result["events"] == []


@pytest.mark.parametrize("timestamp", [None, True, -1, 2**62, float("nan"), float("inf"), "recent"])
def test_legacy_confirmation_timestamps_never_authorize_current_presence(tmp_path, monkeypatch, timestamp):
    client, topic, history, _current = world(tmp_path, monkeypatch)
    live_items(history)[0]["confirmed_ts"] = timestamp
    (tmp_path / "Show/S01E01.mkv").unlink()

    result = reconcile_topic(topic, client, history, SECOND)

    assert result["summary"]["completed"] == 0
    assert live_items(history)[0]["status"] == "missing"


@pytest.mark.parametrize("volume", [None, "uuid:other-disk"])
def test_a_disconnected_or_replaced_volume_never_keeps_recent_files_completed(tmp_path, monkeypatch, volume):
    names = [f"Show/S01E{number:02}.mkv" for number in range(1, 6)]
    client, topic, history, current = world(tmp_path, monkeypatch, names=names)
    current["volume"] = volume
    seen_calls = []
    real_volume_of = platform.volume_of
    monkeypatch.setattr(platform, "volume_of", lambda path: seen_calls.append(path) or real_volume_of(path))
    file_looks = []
    monkeypatch.setattr(
        "tow.progress.reconcile.filesystem_confirmation", lambda *args, **kwargs: file_looks.append(args) or True
    )

    unavailable = reconcile_topic(topic, client, history, SECOND)

    assert unavailable["summary"]["completed"] == 0
    assert unavailable["missing_files"] == 0
    assert unavailable["unverified_files"] == 5
    assert {item["status"] for item in live_items(history)} == {"unverified"}
    assert all(item["verification_unavailable"] for item in live_items(history))
    assert file_looks == []  # one failed volume probe, never five SMB file timeouts
    assert len(seen_calls) == 1
    assert history["topics"]["show"]["volume"]["id"] == "uuid:test-disk"
    assert "volume_unseen" in topic
    current["volume"] = "uuid:test-disk"

    restored = reconcile_topic(topic, client, history, THIRD)

    assert restored["summary"]["completed"] == 5
    assert restored["events"] == []
    assert "volume_unseen" not in topic


def test_current_file_permission_error_is_unknown_not_completed_or_missing(tmp_path, monkeypatch):
    client, topic, history, _current = world(tmp_path, monkeypatch)
    real_stat = Path.stat

    def denied(path, *args, **kwargs):
        if path.name == "S01E01.mkv":
            raise PermissionError("synthetic permission refusal")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", denied)

    result = reconcile_topic(topic, client, history, SECOND)

    assert result["summary"]["completed"] == 0
    assert live_items(history)[0]["status"] == "unverified"
    assert result["missing_files"] == 0
    assert result["unverified_files"] == 1
    assert live_items(history)[0]["verification_unavailable"] is True
    assert live_items(history)[0]["completed_observed_at"] == FIRST


@pytest.mark.parametrize("unobserved", ["absent", "checking", "pending", "empty", "unselected", "unsupported"])
def test_no_presence_counts_are_published_without_observing_current_selected_files(tmp_path, monkeypatch, unobserved):
    client, topic, history, _current = world(tmp_path, monkeypatch)
    if unobserved == "absent":
        client.present = False
    elif unobserved == "checking":
        client.info["state"] = "checkingUP"
    elif unobserved == "pending":
        topic["verification_pending"] = {"phase": "checking"}
    elif unobserved == "empty":
        client.info["files"] = []
    elif unobserved == "unselected":
        client.info["files"][0]["priority"] = 0
    else:
        client.capabilities = {"inspect": False}

    result = reconcile_topic(topic, client, history, SECOND)

    assert "missing_files" not in result
    assert "unverified_files" not in result


def test_an_unresolvable_folder_is_only_looked_at_once_per_pass(tmp_path, monkeypatch):
    client, topic, history, _current = world(
        tmp_path, monkeypatch, names=[f"Show/S01E{number:02}.mkv" for number in range(1, 6)]
    )
    real_resolve = Path.resolve
    looked = []

    def denied(path, *args, **kwargs):
        if platform.ordinary_path(path) == tmp_path:
            looked.append(path)
            raise PermissionError("synthetic folder refusal")
        return real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", denied)

    result = reconcile_topic(topic, client, history, SECOND)

    assert result["summary"]["completed"] == 0
    assert {item["status"] for item in live_items(history)} == {"unverified"}
    assert len(looked) == 1


def test_changing_save_folder_cannot_carry_the_old_folders_positive_confirmation(tmp_path, monkeypatch):
    client, topic, history, _current = world(tmp_path, monkeypatch)
    new = tmp_path / "new"
    new.mkdir()
    topic["save_path"] = client.info["save_path"] = str(new)

    result = reconcile_topic(topic, client, history, SECOND)

    assert result["summary"]["completed"] == 0
    assert live_items(history)[0]["status"] == "missing"
    assert live_items(history)[0]["completed_observed_at"] == FIRST


def test_revision_completion_history_cannot_confirm_a_removed_shared_file(tmp_path, monkeypatch):
    client, topic, history, _current = world(tmp_path, monkeypatch)
    (tmp_path / "Show/S01E01.mkv").unlink()
    topic["hash"] = "b" * 40

    result = reconcile_topic(topic, client, history, SECOND)

    assert result["summary"]["completed"] == 0
    assert len(live_items(history)) == 1
    assert live_items(history)[0]["status"] == "missing"
    assert live_items(history)[0]["completion_carried_from"]
    assert live_items(history)[0]["completed_observed_at"] == FIRST
    assert "episode_completed" not in result["events"]


@pytest.mark.parametrize(("name", "episodes"), [("Show/S01E01E02.mkv", 2), ("Show/S01E01-E03.mkv", 3)])
def test_a_removed_multi_episode_file_invalidates_every_covered_episode(tmp_path, monkeypatch, name, episodes):
    client, topic, history, _current = world(tmp_path, monkeypatch, names=[name])
    assert history["topics"]["show"]["summary"]["completed"] == episodes
    (tmp_path / name).unlink()

    result = reconcile_topic(topic, client, history, SECOND)

    assert result["summary"]["completed"] == 0
    assert live_items(history)[0]["status"] == "missing"


def test_presence_confirmation_reads_metadata_without_opening_the_media(tmp_path, monkeypatch):
    client, topic, history, _current = world(tmp_path, monkeypatch)
    real_open = Path.open

    def no_media(path, *args, **kwargs):
        assert path.suffix != ".mkv", "presence must not read or hash media bytes"
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", no_media)

    result = reconcile_topic(topic, client, history, SECOND)

    assert result["summary"]["completed"] == 1
    assert result["events"] == []


def test_a_cached_complete_file_cannot_bypass_a_path_retargeted_outside_the_save_folder(tmp_path, monkeypatch):
    folder = tmp_path / "media"
    client, topic, history, _current = world(folder, monkeypatch)
    outside = tmp_path / "outside.mkv"
    outside.write_bytes(b"0123456789")
    real_resolve, real_stat = Path.resolve, Path.stat

    def retargeted(path, *args, **kwargs):
        if path.name == "S01E01.mkv":
            return outside
        return real_resolve(path, *args, **kwargs)

    def no_foreign_stat(path, *args, **kwargs):
        assert platform.ordinary_path(path) != outside, "do not look outside the save folder"
        return real_stat(path, *args, **kwargs)

    # Emulate a changed directory symlink without depending on Windows symlink privileges.
    monkeypatch.setattr(Path, "resolve", retargeted)
    monkeypatch.setattr(Path, "stat", no_foreign_stat)

    result = reconcile_topic(topic, client, history, SECOND)

    assert result["summary"]["completed"] == 0
    assert live_items(history)[0]["status"] == "unverified"
    assert live_items(history)[0]["completed_observed_at"] == FIRST


def test_partial_verification_keeps_known_missing_and_unknown_file_counts_distinct(tmp_path, monkeypatch):
    names = [f"Show/S01E{number:02}.mkv" for number in range(1, 4)]
    client, topic, history, _current = world(tmp_path, monkeypatch, names=names)
    (tmp_path / names[0]).unlink()
    real_stat = Path.stat

    def denied(path, *args, **kwargs):
        if path.name == "S01E02.mkv":
            raise PermissionError("synthetic permission refusal")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", denied)

    result = reconcile_topic(topic, client, history, SECOND)

    assert result["summary"]["completed"] == 1
    assert result["missing_files"] == result["unverified_files"] == 1
    assert [item["status"] for item in live_items(history)] == ["missing", "unverified", "completed"]


def test_unchanged_completions_are_checked_again_without_rewriting_their_history(tmp_path, monkeypatch):
    clock = {"now": 1_000}
    monkeypatch.setattr("tow.progress.reconcile.time.time", lambda: clock["now"])
    client, topic, history, _current = world(tmp_path, monkeypatch)
    original = deepcopy(history["topics"]["show"])
    real_stat = Path.stat
    looks = []

    def looked(path, *args, **kwargs):
        if path.name == "S01E01.mkv":
            looks.append(path)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", looked)
    clock["now"] += 60

    result = reconcile_topic(topic, client, history, SECOND)

    current = deepcopy(history["topics"]["show"])
    assert current.pop("last_scan_at") == SECOND
    assert original.pop("last_scan_at") == FIRST
    assert current == original
    assert looks  # no history write does not mean presence came from a cache
    assert result["missing_files"] == result["unverified_files"] == 0
    assert live_items(history)[0]["confirmed_ts"] == 1_000


@pytest.mark.parametrize("previous_state", ["missing", "unverified"])
def test_a_positive_transition_refreshes_confirmation_time_without_repeating_completion(
    tmp_path, monkeypatch, previous_state
):
    clock = {"now": 1_000}
    monkeypatch.setattr("tow.progress.reconcile.time.time", lambda: clock["now"])
    client, topic, history, current = world(tmp_path, monkeypatch)
    target = tmp_path / "Show/S01E01.mkv"
    if previous_state == "missing":
        target.unlink()
    else:
        current["volume"] = None
    clock["now"] += 60
    reconcile_topic(topic, client, history, SECOND)
    assert live_items(history)[0]["status"] == previous_state
    assert live_items(history)[0]["confirmed_ts"] == 1_000
    if previous_state == "missing":
        target.write_bytes(b"0123456789")
    else:
        current["volume"] = "uuid:test-disk"
    clock["now"] += 60

    result = reconcile_topic(topic, client, history, THIRD)

    assert live_items(history)[0]["confirmed_ts"] == 1_120
    assert live_items(history)[0]["completed_observed_at"] == FIRST
    assert result["events"] == []


def test_an_explicit_volume_rebind_records_a_current_positive_confirmation(tmp_path, monkeypatch):
    clock = {"now": 1_000}
    monkeypatch.setattr("tow.progress.reconcile.time.time", lambda: clock["now"])
    client, topic, history, current = world(tmp_path, monkeypatch)
    current["volume"] = "uuid:new-disk"
    clock["now"] += 60

    result = reconcile_topic(topic, client, history, SECOND, rebind_volume=True)

    assert live_items(history)[0]["confirmed_ts"] == 1_060
    assert history["topics"]["show"]["volume"]["id"] == "uuid:new-disk"
    assert result["events"] == []


@pytest.mark.parametrize("invalid", [None, True, 0, -1, 2**62, float("nan"), float("inf"), "old"])
def test_an_invalid_confirmation_timestamp_is_repaired_once_by_actual_presence(tmp_path, monkeypatch, invalid):
    clock = {"now": 1_000}
    monkeypatch.setattr("tow.progress.reconcile.time.time", lambda: clock["now"])
    client, topic, history, _current = world(tmp_path, monkeypatch)
    live_items(history)[0]["confirmed_ts"] = invalid
    clock["now"] += 60
    reconcile_topic(topic, client, history, SECOND)
    assert live_items(history)[0]["confirmed_ts"] == 1_060
    clock["now"] += 60

    result = reconcile_topic(topic, client, history, THIRD)

    assert live_items(history)[0]["confirmed_ts"] == 1_060
    assert result["events"] == []


@pytest.mark.parametrize("rebind", [False, True])
@pytest.mark.parametrize("local_state", ["present", "missing"])
def test_a_remote_clients_identical_path_is_never_used_as_local_presence_evidence(
    tmp_path, monkeypatch, rebind, local_state
):
    client, topic, history, _current = world(tmp_path, monkeypatch)
    recorded_volume = deepcopy(history["topics"]["show"]["volume"])
    if local_state == "missing":
        (tmp_path / "Show/S01E01.mkv").unlink()

    def no_local_io(*args, **kwargs):
        raise AssertionError("a remote client's path must not be probed on this computer")

    monkeypatch.setattr("tow.folders.seen_from_here", no_local_io)
    monkeypatch.setattr(platform, "volume_of", no_local_io)
    monkeypatch.setattr(Path, "resolve", no_local_io)
    monkeypatch.setattr(Path, "stat", no_local_io)

    result = reconcile_topic(topic, client, history, SECOND, local_files=False, rebind_volume=rebind)

    assert result["summary"]["completed"] == result["missing_files"] == 0
    assert result["unverified_files"] == 1
    assert live_items(history)[0]["status"] == "unverified"
    assert live_items(history)[0]["completed_observed_at"] == FIRST
    assert history["topics"]["show"]["volume"] == recorded_volume
    assert "volume_unseen" not in topic  # remote unknown is not a disconnected local disk
    assert result["events"] == []


def test_first_remote_observation_cannot_bind_a_volume_from_an_identical_local_folder(tmp_path, monkeypatch):
    client, topic, _old_history, _current = world(tmp_path, monkeypatch)
    history = {"topics": {}}

    def no_local_io(*args, **kwargs):
        raise AssertionError("a remote client's path must not be probed on this computer")

    monkeypatch.setattr("tow.folders.seen_from_here", no_local_io)
    monkeypatch.setattr(platform, "volume_of", no_local_io)
    monkeypatch.setattr(Path, "resolve", no_local_io)
    monkeypatch.setattr(Path, "stat", no_local_io)

    result = reconcile_topic(topic, client, history, FIRST, local_files=False)

    assert result["summary"]["completed"] == result["missing_files"] == 0
    assert result["unverified_files"] == 1
    assert live_items(history)[0]["status"] == "unverified"
    assert "confirmed_ts" not in live_items(history)[0]
    assert "volume" not in history["topics"]["show"]
    assert "volume_unseen" not in topic
    assert result["events"] == []


@pytest.mark.parametrize(("reported_folder", "state"), [("old", "moving"), ("old", "stoppedUP"), ("new", "moving")])
def test_an_accepted_move_never_treats_departing_files_as_missing(tmp_path, monkeypatch, reported_folder, state):
    old, new = tmp_path / "old", tmp_path / "new"
    client, topic, history, _current = world(old, monkeypatch)
    new.mkdir()
    (old / "Show").rename(new / "Show")
    topic.update(save_path=str(new), move_pending={"from": str(old), "to": str(new), "since": FIRST})
    client.info.update(save_path=str(old if reported_folder == "old" else new), state=state)
    real_stat = Path.stat

    def no_moving_file_stat(path, *args, **kwargs):
        assert path.name != "S01E01.mkv", "the client's pending move has no settled file location"
        return real_stat(path, *args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(Path, "stat", no_moving_file_stat)
        moving = reconcile_topic(topic, client, history, SECOND)

    assert moving["summary"]["completed"] == moving["missing_files"] == 0
    assert moving["unverified_files"] == 1
    assert live_items(history)[0]["status"] == "unverified"
    assert live_items(history)[0]["completed_observed_at"] == FIRST
    assert "move_pending" in topic
    assert "volume_unseen" not in topic
    assert moving["events"] == []
    client.info.update(save_path=str(new), state="uploading")

    settled = reconcile_topic(topic, client, history, THIRD)

    assert settled["summary"]["completed"] == 1
    assert settled["missing_files"] == settled["unverified_files"] == 0
    assert "move_pending" not in topic
    assert history["topics"]["show"]["volume"]["path"] == str(new)
    assert live_items(history)[0]["completed_observed_at"] == FIRST
    assert settled["events"] == []
