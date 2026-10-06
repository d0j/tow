from __future__ import annotations

from typing import ClassVar

import pytest

from tow.progress import reconcile_topic

HASH = "a" * 40


class Client:
    client_id = "fake-main"
    client_kind = "fake"
    capabilities: ClassVar = {"inspect": True, "list_files": True}

    def __init__(self):
        self.files = [
            {"name": "ShowA.S01E01.mkv", "size": 1, "progress": 1.0, "priority": 1},
            {"name": "ShowA.S01E02.mkv", "size": 1, "progress": 0.0, "priority": 0},
        ]

    def inspect_torrent(self, infohash):
        return {"hash": infohash, "files": self.files}


def topic_for(tmp_path, title):
    (tmp_path / "ShowA.S01E01.mkv").write_bytes(b"x")
    return {
        "id": "test",
        "title": title,
        "hash": HASH,
        "save_path": str(tmp_path),
        "selection": {"mode": "all"},
        "selection_hash": HASH,
        "selection_verified": True,
        "selected_episode_keys": ["episode:s01e01"],
    }


@pytest.mark.parametrize("title", ["Show A", "Show A [1 из 1]", "Show A [2 из 3]"])
@pytest.mark.parametrize("priority", [0, 1])
def test_all_episode_target_uses_metadata_not_only_enabled_client_files(tmp_path, title, priority):
    topic = topic_for(tmp_path, title)
    client = Client()
    client.files[1]["priority"] = priority
    history = {"topics": {}}
    result = reconcile_topic(topic, client, history, "2026-09-10T20:00:00+00:00")
    assert result["summary"]["expected"] == (3 if "из 3" in title else 2)
    assert result["summary"]["completed"] == 1
    assert result["summary"]["is_complete"] is False
    assert result["events"] == []
    assert client.files[1]["priority"] == priority
    assert topic["selection"] == {"mode": "all"}
    assert topic["selected_episode_keys"] == ["episode:s01e01"]


@pytest.mark.parametrize("title", ["Show A", "Show A [1 из 1]"])
def test_missing_metadata_retains_the_current_all_episode_target(tmp_path, title):
    topic = topic_for(tmp_path, title)
    client = Client()
    history = {"topics": {}}
    reconcile_topic(topic, client, history, "2026-09-10T20:00:00+00:00")
    client.files = []
    result = reconcile_topic(topic, client, history, "2026-09-11T20:00:00+00:00")
    assert result["summary"]["expected"] == 2
    assert result["summary"]["completed"] == 1
    assert result["summary"]["is_complete"] is False
    assert result["events"] == []


def test_new_tracker_total_invalidates_the_cached_all_target(tmp_path):
    topic = topic_for(tmp_path, "Show A [1 из 1]")
    history = {"topics": {}}
    reconcile_topic(topic, Client(), history, "2026-09-10T20:00:00+00:00")
    topic["tracker_title"] = "Show A [2 из 3]"
    client = Client()
    client.files = []
    result = reconcile_topic(topic, client, history, "2026-09-11T20:00:00+00:00")
    assert result["summary"]["expected"] == 3
    assert result["summary"]["is_complete"] is False


def test_disabled_subtitle_without_a_video_cannot_inflate_all_episode_target(tmp_path):
    topic = topic_for(tmp_path, "Show A [1 из 1]")
    client = Client()
    client.files[1]["name"] = "ShowA.S01E99.srt"
    result = reconcile_topic(topic, client, {"topics": {}}, "2026-09-10T20:00:00+00:00")
    assert result["summary"]["expected"] == result["summary"]["completed"] == 1
    assert result["summary"]["is_complete"] is True


def test_enabling_and_completing_the_second_episode_emits_completion_once(tmp_path):
    topic = topic_for(tmp_path, "Show A [2 из 2]")
    client = Client()
    history = {"topics": {}}
    first = reconcile_topic(topic, client, history, "2026-09-10T20:00:00+00:00")
    assert first["summary"]["expected"] == 2
    assert first["summary"]["is_complete"] is False
    client.files[1].update(priority=1, progress=1.0)
    (tmp_path / "ShowA.S01E02.mkv").write_bytes(b"x")
    second = reconcile_topic(topic, client, history, "2026-09-11T20:00:00+00:00")
    assert second["summary"]["expected"] == second["summary"]["completed"] == 2
    assert second["summary"]["is_complete"] is True
    assert second["events"].count("episode_completed") == 1
    third = reconcile_topic(topic, client, history, "2026-09-12T20:00:00+00:00")
    assert third["events"] == []


@pytest.mark.parametrize("metadata_available", [False, True])
@pytest.mark.parametrize(
    "flags", [{"selection_dirty": True}, {"selection_verified": False}, {"selection_hash": "b" * 40}]
)
def test_unconfirmed_all_rule_cannot_be_completed_by_previous_client_files(tmp_path, metadata_available, flags):
    topic = topic_for(tmp_path, "Show A [1 из 1]")
    client = Client()
    client.files = client.files[:1]
    history = {"topics": {}}
    reconcile_topic(topic, client, history, "2026-09-10T20:00:00+00:00")
    topic.update(flags)
    if not metadata_available:
        client.files = []
    result = reconcile_topic(topic, client, history, "2026-09-11T20:00:00+00:00")
    assert result["summary"]["completed"] == 1
    assert result["summary"]["completion_known"] is False
    assert result["summary"]["is_complete"] is False
    assert result["events"] == []
