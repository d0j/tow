from __future__ import annotations

from typing import ClassVar

import pytest

from tow.episodes import expected_for_topic, summarize_completion
from tow.progress import _expected_from_observed, reconcile_topic
from tow.selection import SelectionError, normalize_policy

HASH = "a" * 40


class Client:
    client_id = "fake-main"
    client_kind = "fake"
    capabilities: ClassVar = {"inspect": True, "list_files": True, "completion_time": False}

    def __init__(self, files):
        self.files = files

    def inspect_torrent(self, infohash):
        return {"hash": infohash, "progress": 1.0, "files": self.files}


def topic_for(tmp_path, policy, **flags):
    return {
        "id": "test",
        "title": "Show A",
        "hash": HASH,
        "save_path": str(tmp_path),
        "selection": policy,
        "selected_episode_keys": ["episode:s01e01"],
        "selection_hash": HASH,
        "selection_verified": True,
        **flags,
    }


@pytest.mark.parametrize("mode", ["files", "exact"])
def test_verified_file_rule_refreshes_newly_recognized_membership_without_changing_policy(tmp_path, mode):
    paths = ["ShowA.S01E01.mkv", "ShowA_S02E01.mkv"]
    for path in paths:
        (tmp_path / path).write_bytes(b"x")
    policy = normalize_policy(mode, "*.mkv", files=[{"path": path, "size": 1} for path in paths], source_hash=HASH)
    topic = topic_for(tmp_path, policy)
    client = Client([{"name": path, "size": 1, "progress": 1.0, "priority": 1} for path in paths])
    history = {"topics": {}}
    result = reconcile_topic(topic, client, history, "2026-09-10T20:00:00+00:00")
    assert result["summary"]["completed"] == 2
    assert result["summary"]["expected"] == 2
    assert result["summary"]["is_complete"] is True
    assert result["events"] == []
    assert topic["selection"] == policy
    assert topic["selected_episode_keys"] == ["episode:s01e01"]


@pytest.mark.parametrize("mode", ["episodes", "files", "exact"])
@pytest.mark.parametrize(
    "flags",
    [
        {"selection_dirty": True},
        {"selection_verified": False},
        {"selection_hash": "b" * 40},
    ],
)
def test_pending_unconfirmed_or_other_revision_rule_never_claims_its_target_completed(tmp_path, mode, flags):
    path = "ShowA.S01E01.mkv"
    (tmp_path / path).write_bytes(b"x")
    policy = normalize_policy(
        mode, "S01E99" if mode == "episodes" else "*.mkv", files=[{"path": path, "size": 1}], source_hash=HASH
    )
    topic = topic_for(tmp_path, policy, **flags)
    history = {"topics": {}}
    client = Client([{"name": path, "size": 1, "progress": 1.0, "priority": 1}])
    result = reconcile_topic(topic, client, history, "2026-09-10T20:00:00+00:00")
    assert result["summary"]["completed"] == 1
    assert result["summary"]["is_complete"] is False
    assert result["summary"]["completion_known"] is False
    assert history["topics"]["test"]["expected"]["source"] == "files"
    assert result["events"] == []


@pytest.mark.parametrize("mode", ["files", "exact"])
@pytest.mark.parametrize("empty_cache", [False, True])
def test_unwanted_client_file_cannot_complete_a_verified_literal_or_mask_target(tmp_path, mode, empty_cache):
    wanted, unrelated = "ShowA.S01E01.wanted.mkv", "ShowA.S01E01.other.mkv"
    (tmp_path / unrelated).write_bytes(b"x")
    policy = normalize_policy(mode, "*.wanted.mkv", files=[{"path": wanted, "size": 1}], source_hash=HASH)
    topic = topic_for(tmp_path, policy)
    if empty_cache:
        topic["selected_episode_keys"] = []
    client = Client(
        [
            {"name": wanted, "size": 1, "progress": 0.0, "priority": 0},
            {"name": unrelated, "size": 1, "progress": 1.0, "priority": 1},
        ]
    )
    result = reconcile_topic(topic, client, {"topics": {}}, "2026-09-10T20:00:00+00:00")
    assert result["summary"]["completed"] == 0
    assert result["summary"]["expected"] == 1
    assert result["summary"]["is_complete"] is False


@pytest.mark.parametrize("mode", ["files", "exact"])
def test_same_completed_combined_file_refreshes_membership_and_event_silently(tmp_path, mode):
    path = "ShowA.S01E08&S02E01.mkv"
    (tmp_path / path).write_bytes(b"x")
    policy = normalize_policy(mode, "*.mkv", files=[{"path": path, "size": 1}], source_hash=HASH)
    topic = topic_for(tmp_path, policy, selected_episode_keys=["episode:s01e08"])
    at = "2026-09-10T20:00:00+00:00"
    identity = "file:" + path.lower() + ":1"
    item = {
        "identity": identity,
        "kind": "episode",
        "label": "S01E08",
        "relative_path": path,
        "source_hash": HASH,
        "size": 1,
        "status": "completed",
        "episode_keys": ["episode:s01e08"],
        "first_seen_at": "2026-09-10T19:00:00+00:00",
        "completed_observed_at": at,
    }
    record = {
        "baseline_at": "2026-09-10T19:00:00+00:00",
        "items": {identity: item},
        "last_event": {"kind": "episode_completed", "label": "S01E08", "at": at},
    }
    history = {"topics": {"test": record}}
    client = Client([{"name": path, "size": 1, "progress": 1.0, "priority": 1}])
    result = reconcile_topic(topic, client, history, "2026-09-11T20:00:00+00:00")
    assert result["summary"]["completed"] == result["summary"]["expected"] == 2
    assert result["events"] == []
    assert record["last_event"]["label"] == "S01E08, S02E01"
    assert record["last_event"]["at"] == at
    assert list(record["items"]) == [identity]
    assert item["completed_observed_at"] == at
    assert (tmp_path / path).read_bytes() == b"x"


def test_changed_exact_identity_is_refused_instead_of_completing_with_other_bytes(tmp_path):
    path = "ShowA.S01E01.mkv"
    (tmp_path / path).write_bytes(b"xx")
    policy = normalize_policy("exact", files=[{"path": path, "size": 1}], source_hash=HASH)
    with pytest.raises(SelectionError) as caught:
        reconcile_topic(
            topic_for(tmp_path, policy),
            Client([{"name": path, "size": 2, "progress": 1.0}]),
            {"topics": {}},
            "2026-09-10T20:00:00+00:00",
        )
    assert caught.value.code == "selection.exact_changed"


def test_unconfirmed_episode_rule_without_saved_keys_cannot_be_completed_by_old_files():
    topic = {"hash": HASH, "selection": normalize_policy("episodes", "S01E99"), "selection_verified": True}
    expected = _expected_from_observed(topic, expected_for_topic(topic), ["episode:s01e01"], [])
    assert expected["source"] == "files"
    assert (
        summarize_completion(
            {"file": {"kind": "episode", "episode_keys": ["episode:s01e01"], "status": "completed"}}, expected
        )["is_complete"]
        is False
    )


@pytest.mark.parametrize("mode", ["files", "exact"])
@pytest.mark.parametrize("prior_scan", [False, True])
def test_temporarily_empty_client_metadata_keeps_target_without_inventing_completion(tmp_path, mode, prior_scan):
    path = "ShowA.S01E01.mkv"
    policy = normalize_policy(mode, "*.mkv", files=[{"path": path, "size": 1}], source_hash=HASH)
    topic = topic_for(tmp_path, policy)
    history = {"topics": {}}
    if prior_scan:
        reconcile_topic(
            topic,
            Client([{"name": path, "size": 1, "progress": 0.0, "priority": 1}]),
            history,
            "2026-09-10T20:00:00+00:00",
        )
    result = reconcile_topic(topic, Client([]), history, "2026-09-11T20:00:00+00:00")
    record = history["topics"]["test"]
    assert result["summary"]["expected"] == 1
    assert result["summary"]["completed"] == 0
    assert result["summary"]["is_complete"] is False
    assert result["events"] == []
    assert len(record["items"]) == int(prior_scan)
    assert topic["selection"] == policy
    assert record.get("baseline_at") == ("2026-09-10T20:00:00+00:00" if prior_scan else None)


@pytest.mark.parametrize("mode", ["files", "exact"])
def test_empty_metadata_does_not_shrink_the_recomputed_rule_target_to_old_episode_cache(tmp_path, mode):
    paths = ["ShowA.S01E01.mkv", "ShowA_S02E01.mkv"]
    (tmp_path / paths[0]).write_bytes(b"x")
    policy = normalize_policy(mode, "*.mkv", files=[{"path": path, "size": 1} for path in paths], source_hash=HASH)
    topic = topic_for(tmp_path, policy)
    history = {"topics": {}}
    client = Client(
        [{"name": path, "size": 1, "progress": float(index == 0), "priority": 1} for index, path in enumerate(paths)]
    )
    first = reconcile_topic(topic, client, history, "2026-09-10T20:00:00+00:00")
    assert first["summary"]["expected"] == 2
    assert first["summary"]["completed"] == 1
    assert first["summary"]["is_complete"] is False
    second = reconcile_topic(topic, Client([]), history, "2026-09-11T20:00:00+00:00")
    assert second["summary"]["expected"] == 2
    assert second["summary"]["completed"] == 1
    assert second["summary"]["is_complete"] is False
    assert second["events"] == []


@pytest.mark.parametrize("mode", ["files", "exact"])
@pytest.mark.parametrize("change", ["policy", "hash", "client", "season", "dirty", "unverified", "legacy"])
def test_cached_rule_target_is_not_reused_for_another_context(tmp_path, mode, change):
    paths = ["ShowA.S01E01.mkv", "ShowA_S02E01.mkv"]
    policy = normalize_policy(mode, "*.mkv", files=[{"path": path, "size": 1} for path in paths], source_hash=HASH)
    topic = topic_for(tmp_path, policy)
    history = {"topics": {}}
    reconcile_topic(
        topic,
        Client([{"name": path, "size": 1, "progress": 0.0, "priority": 1} for path in paths]),
        history,
        "2026-09-10T20:00:00+00:00",
    )
    assert history["topics"]["test"]["expected"]["total"] == 2
    if change == "policy":
        topic["selection"] = normalize_policy(mode, paths[0], files=[{"path": paths[0], "size": 1}], source_hash=HASH)
    elif change == "hash":
        topic["hash"] = topic["selection_hash"] = "b" * 40
    elif change == "client":
        topic["client_id"] = "another-client"
    elif change == "season":
        topic["selected_episode_keys"] = ["episode:s03e01"]
    elif change == "dirty":
        topic["selection_dirty"] = True
    elif change == "unverified":
        topic["selection_verified"] = False
    else:
        history["topics"]["test"]["expected"].pop("selection_fingerprint")
    result = reconcile_topic(topic, Client([]), history, "2026-09-11T20:00:00+00:00")
    assert result["summary"]["expected"] == (None if change in {"dirty", "unverified"} else 1)
    assert result["summary"]["is_complete"] is False
    assert result["events"] == []


@pytest.mark.parametrize("mode", ["files", "exact"])
def test_rule_context_is_stable_across_hash_case_and_policy_dictionary_order(tmp_path, mode):
    paths = ["ShowA.S01E01.mkv", "ShowA_S02E01.mkv"]
    policy = normalize_policy(mode, "*.mkv", files=[{"path": path, "size": 1} for path in paths], source_hash=HASH)
    topic = topic_for(tmp_path, policy)
    history = {"topics": {}}
    reconcile_topic(
        topic,
        Client([{"name": path, "size": 1, "progress": 0.0, "priority": 1} for path in paths]),
        history,
        "2026-09-10T20:00:00+00:00",
    )
    topic["hash"] = HASH.upper()
    topic["selection"] = dict(reversed(list(policy.items())))
    result = reconcile_topic(topic, Client([]), history, "2026-09-11T20:00:00+00:00")
    assert result["summary"]["expected"] == 2
    assert result["summary"]["is_complete"] is False
