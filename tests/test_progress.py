from pathlib import Path
from typing import ClassVar

import pytest
from helpers import raises_code

from tow.episodes import item_identity, summarize_completion
from tow.progress import _file_progress, _latest_item_label, _preferred_season, reconcile_topic, safe_relative_path


def test_completed_auxiliary_file_does_not_replace_latest_episode_label():
    record = {
        "items": {
            "main": {"kind": "episode", "label": "S01E01", "episode_keys": ["episode:s01e01"], "status": "completed"},
            "sample": {"kind": "file", "label": "S01E99.sample.mkv", "status": "completed"},
        }
    }
    assert str(_latest_item_label(record)) == "S01E01"


def test_client_absolute_paths_cannot_be_interpreted_as_relative_files(tmp_path: Path):
    for name in ("/Show/S01E01.mkv", r"\Show\S01E01.mkv", r"\\server\share\S01E01.mkv", r"C:\Show\S01E01.mkv"):
        assert safe_relative_path(str(tmp_path), name) is None
    assert safe_relative_path(str(tmp_path), "Show/S01E01.mkv") == "Show/S01E01.mkv"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), "nan", "inf"])
def test_non_finite_client_progress_cannot_confirm_a_file(value):
    assert _file_progress({"progress": value}, {}) == 0.0


@pytest.mark.parametrize("value", [0, 0.5, 1.0, "0", "0.5", "1.0"])
def test_valid_file_fraction_is_preserved(value):
    assert _file_progress({"progress": value}, {}) == float(value)
    assert _file_progress({}, {"progress": value}) == float(value)


def test_non_finite_progress_does_not_emit_a_false_completion(tmp_path: Path):
    client = FakeClient()
    client.progress = float("nan")
    folder = tmp_path / "Show"
    folder.mkdir()
    (folder / "S01E01.mkv").write_bytes(b"0123456789")
    topic = {"id": "invalid-progress", "title": "Show [1 из 1]", "hash": "ABC", "save_path": str(tmp_path)}
    history = {"topics": {"invalid-progress": {"baseline_at": "2026-09-12T20:00:00+03:00", "items": {}}}}

    result = reconcile_topic(topic, client, history, "2026-09-12T21:00:00+03:00")

    assert result["summary"]["completed"] == 0
    assert "episode_completed" not in result["events"]


@pytest.mark.parametrize("value", [True, False, -1, 1.1, "1.1", "bad", None, [], {}, 10**1000])
def test_invalid_file_fraction_cannot_confirm_completion(tmp_path: Path, value):
    assert _file_progress({"progress": value}, {"progress": 1.0}) == 0.0
    client = FakeClient()
    client.progress = value
    folder = tmp_path / "Show"
    folder.mkdir()
    (folder / "S01E01.mkv").write_bytes(b"0123456789")
    topic = {"id": "invalid-fraction", "title": "Show [1 из 1]", "hash": "ABC", "save_path": str(tmp_path)}
    history = {"topics": {topic["id"]: {"baseline_at": "2026-09-12T20:00:00+03:00", "items": {}}}}
    result = reconcile_topic(topic, client, history, "2026-09-12T21:00:00+03:00")
    assert result["summary"]["completed"] == 0
    assert "episode_completed" not in result["events"]


@pytest.mark.parametrize("legacy", [False, True, "completed"])
def test_special_does_not_block_episode_completion_and_reconciliation_is_idempotent(tmp_path: Path, legacy):
    class ClientWithSpecial(FakeClient):
        def inspect_torrent(self, infohash):
            info = super().inspect_torrent(infohash)
            info["files"].append(
                {"name": "Show/Season 00/01.mkv", "size": 3, "progress": getattr(self, "special_progress", 0.0)}
            )
            return info

    client = ClientWithSpecial()
    client.progress = 1.0
    folder = tmp_path / "Show"
    folder.mkdir()
    (folder / "S01E01.mkv").write_bytes(b"0123456789")
    topic = {"id": "special", "title": "Show S01 [1 из 1]", "hash": "ABC", "save_path": str(tmp_path)}
    record = {"baseline_at": "2026-09-12T20:00:00+03:00", "items": {}}
    if legacy:
        identity = item_identity("Show/Season 00/01.mkv", None, 3)
        record["items"][identity] = {
            "identity": identity,
            "kind": "episode",
            "episode_key": "episode:s01e01",
            "episode_keys": ["episode:s01e01"],
            "relative_path": "Show/Season 00/01.mkv",
            "source_hash": "ABC",
            "size": 3,
            "status": "completed" if legacy == "completed" else "downloading",
            "first_seen_at": record["baseline_at"],
        }
        if legacy == "completed":
            record["items"][identity]["completed_observed_at"] = "2026-09-12T20:30:00+03:00"
    history = {"topics": {topic["id"]: record}}
    result = reconcile_topic(topic, client, history, "2026-09-12T21:00:00+03:00")
    assert result["summary"]["completed"] == 1
    assert result["summary"]["is_complete"] is True
    assert result["events"].count("episode_completed") == 1
    special = next(item for item in record["items"].values() if "Season 00" in item["relative_path"])
    assert special["kind"] == "file"
    assert special["episode_keys"] == []
    assert reconcile_topic(topic, client, history, "2026-09-12T21:01:00+03:00")["events"] == []
    special_folder = folder / "Season 00"
    special_folder.mkdir()
    (special_folder / "01.mkv").write_bytes(b"012")
    client.special_progress = 1.0
    completed = reconcile_topic(topic, client, history, "2026-09-12T21:02:00+03:00")
    assert completed["summary"]["completed"] == 1
    assert completed["events"] == ([] if legacy == "completed" else ["file_completed"])
    if legacy == "completed":
        assert special["completed_observed_at"] == "2026-09-12T20:30:00+03:00"
    assert reconcile_topic(topic, client, history, "2026-09-12T21:03:00+03:00")["events"] == []


def test_summary_deduplicates_episode_revisions():
    items = {
        "episode:s04e01": {"identity": "episode:s04e01", "kind": "episode", "label": "S04E01", "status": "completed"},
        "episode:s04e01#revision:old": {
            "identity": "episode:s04e01#revision:old",
            "kind": "episode",
            "label": "S04E01",
            "status": "completed",
        },
        "episode:s04e22": {"identity": "episode:s04e22", "kind": "episode", "label": "S04E22", "status": "completed"},
    }

    summary = summarize_completion(items, {"kind": "episodes", "total": 24})

    assert summary["completed"] == 2
    assert summary["expected"] == 24
    assert summary["is_complete"] is False


class FakeClient:
    client_id = "fake-main"
    client_kind = "fake"
    capabilities: ClassVar[dict[str, bool]] = {"inspect": True, "list_files": True, "completion_time": False}

    def __init__(self):
        self.progress = 0.0
        self.present = True

    def inspect_torrent(self, infohash):
        if not self.present:
            return None
        return {
            "hash": infohash,
            "progress": self.progress,
            "completion_on": None,
            "files": [
                {"name": "Show/S01E01.mkv", "size": 10, "progress": self.progress},
            ],
        }


def test_unfinished_sample_clip_does_not_block_completed_episode(tmp_path: Path):
    class ClientWithSample(FakeClient):
        def inspect_torrent(self, infohash):
            info = super().inspect_torrent(infohash)
            info["files"].append({"name": "Show/S01E01.sample.mkv", "size": 3, "progress": 0.0})
            info["files"].append({"name": "Show/S01E01 Sample.mkv", "size": 3, "progress": 0.0})
            info["files"].append({"name": "Show/S01E01 [Preview].mkv", "size": 3, "progress": 0.0})
            return info

    client = ClientWithSample()
    client.progress = 1.0
    folder = tmp_path / "Show"
    folder.mkdir()
    (folder / "S01E01.mkv").write_bytes(b"0123456789")
    topic = {"id": "sample", "title": "Show [1 из 1]", "hash": "ABC", "save_path": str(tmp_path)}
    history = {"topics": {"sample": {"baseline_at": "2026-09-12T20:00:00+03:00", "items": {}}}}

    result = reconcile_topic(topic, client, history, "2026-09-12T21:00:00+03:00")

    assert result["summary"]["completed"] == 1
    assert result["events"] == ["new_file", "episode_completed"]
    again = reconcile_topic(topic, client, history, "2026-09-12T21:01:00+03:00")
    assert again["events"] == []


def test_reconcile_records_first_completed_item_once(tmp_path: Path):
    client = FakeClient()
    topic = {
        "id": "topic-1",
        "title": "Show [01x01-01 из 1]",
        "hash": "ABC123",
        "save_path": str(tmp_path),
        "client_id": "fake-main",
    }
    history = {
        "schema_version": 1,
        "topics": {"topic-1": {"baseline_at": "2026-09-12T20:00:00+03:00", "items": {}}},
    }
    client.progress = 0.5
    first = reconcile_topic(topic, client, history, now="2026-09-12T21:00:00+03:00")
    assert first["summary"]["completed"] == 0
    assert first["summary"]["expected"] == 1

    (tmp_path / "Show").mkdir()
    (tmp_path / "Show" / "S01E01.mkv").write_bytes(b"0123456789")
    client.progress = 1.0
    second = reconcile_topic(topic, client, history, now="2026-09-12T21:01:00+03:00")
    assert second["summary"]["completed"] == 1
    assert second["summary"]["is_complete"] is True
    assert second["events"] == ["episode_completed"]

    third = reconcile_topic(topic, client, history, now="2026-09-12T21:02:00+03:00")
    assert third["events"] == []
    item = next(iter(history["topics"]["topic-1"]["items"].values()))
    assert item["episode_key"] == "episode:s01e01"
    assert item["completed_observed_at"] == "2026-09-12T21:01:00+03:00"


def test_first_missing_inspection_does_not_create_a_false_baseline(tmp_path: Path):
    client = FakeClient()
    client.present = False
    topic = {"id": "late", "title": "Show [1 из 1]", "hash": "ABC", "save_path": str(tmp_path)}
    history: dict = {"topics": {}}
    reconcile_topic(topic, client, history, "2026-09-12T21:00:00+03:00")
    assert "baseline_at" not in history["topics"]["late"]
    (tmp_path / "Show").mkdir()
    (tmp_path / "Show" / "S01E01.mkv").write_bytes(b"0123456789")
    client.present = True
    client.progress = 1.0
    result = reconcile_topic(topic, client, history, "2026-09-12T21:01:00+03:00")
    assert result["events"] == []
    assert history["topics"]["late"]["baseline_at"] == "2026-09-12T21:01:00+03:00"


def test_temporary_verification_failure_does_not_repeat_completion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    client = FakeClient()
    client.progress = 1.0
    topic = {"id": "offline", "title": "Show [1 из 1]", "hash": "ABC", "save_path": str(tmp_path)}
    history = {"topics": {"offline": {"baseline_at": "2026-09-12T20:00:00+03:00", "items": {}}}}
    evidence = iter((True, None, True))
    monkeypatch.setattr("tow.progress.filesystem_confirmation", lambda *_args, **_kw: next(evidence))
    monkeypatch.setattr("tow.progress.CONFIRMATION_REUSE_SEC", 0)  # every pass looks at the disk here
    first = reconcile_topic(topic, client, history, "2026-09-12T21:00:00+03:00")
    assert "episode_completed" in first["events"]
    second = reconcile_topic(topic, client, history, "2026-09-12T21:01:00+03:00")
    assert second["summary"]["completed"] == 0
    third = reconcile_topic(topic, client, history, "2026-09-12T21:02:00+03:00")
    assert third["events"] == []
    item = next(iter(history["topics"]["offline"]["items"].values()))
    assert item["completed_observed_at"] == "2026-09-12T21:00:00+03:00"


def test_baseline_completion_survives_offline_storage_without_new_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    client = FakeClient()
    client.progress = 1.0
    topic = {"id": "baseline-offline", "title": "Show [1 из 1]", "hash": "ABC", "save_path": str(tmp_path)}
    history: dict = {"topics": {}}
    evidence = iter((True, None, True))
    monkeypatch.setattr("tow.progress.filesystem_confirmation", lambda *_args, **_kw: next(evidence))
    monkeypatch.setattr("tow.progress.CONFIRMATION_REUSE_SEC", 0)  # every pass looks at the disk here
    reconcile_topic(topic, client, history, "2026-09-12T21:00:00+03:00")
    reconcile_topic(topic, client, history, "2026-09-12T21:01:00+03:00")
    result = reconcile_topic(topic, client, history, "2026-09-12T21:02:00+03:00")
    assert result["events"] == []


def test_reappearing_client_clears_stale_removed_event(tmp_path: Path):
    client = FakeClient()
    topic = {"id": "return", "title": "Show [1 из 1]", "hash": "ABC", "save_path": str(tmp_path)}
    history: dict = {"topics": {}}
    reconcile_topic(topic, client, history, "2026-09-12T21:00:00+03:00")
    client.present = False
    reconcile_topic(topic, client, history, "2026-09-12T21:01:00+03:00")
    assert history["topics"]["return"]["last_event"]["kind"] == "client_removed"
    client.present = True
    restored = reconcile_topic(topic, client, history, "2026-09-12T21:02:00+03:00")
    assert restored["events"] == ["client_restored"]
    assert history["topics"]["return"]["last_event"]["kind"] == "client_restored"


def test_unmatched_legacy_item_without_hash_cannot_block_episode_completion(tmp_path: Path):
    client = FakeClient()
    client.progress = 1.0
    (tmp_path / "Show").mkdir()
    (tmp_path / "Show" / "S01E01.mkv").write_bytes(b"0123456789")
    topic = {"id": "legacy", "title": "Show [1 из 1]", "hash": "ABC", "save_path": str(tmp_path)}
    history = {
        "topics": {
            "legacy": {
                "baseline_at": "2026-09-12T20:00:00+03:00",
                "items": {
                    "old": {
                        "identity": "old",
                        "relative_path": "Show/old.S01E01.mkv",
                        "episode_key": "episode:s01e01",
                        "status": "seen",
                    }
                },
            }
        }
    }
    result = reconcile_topic(topic, client, history, "2026-09-12T21:00:00+03:00")
    assert result["events"] == ["new_file", "episode_completed"]
    assert history["topics"]["legacy"]["items"]["old"]["superseded"] is True


def test_subtitle_only_file_mask_counts_a_file_not_a_completed_episode(tmp_path: Path):
    class SubtitleClient(FakeClient):
        def inspect_torrent(self, infohash):
            return {
                "hash": infohash,
                "files": [{"name": "Show/S01E01.ass", "size": 2, "progress": 1.0, "priority": 1}],
            }

    (tmp_path / "Show").mkdir()
    (tmp_path / "Show" / "S01E01.ass").write_bytes(b"ok")
    topic = {
        "id": "subs-only",
        "title": "Show [1 из 12]",
        "hash": "ABC",
        "save_path": str(tmp_path),
        "selection": {"mode": "files", "value": "*.ass"},
        "selected_episode_keys": [],
    }
    result = reconcile_topic(topic, SubtitleClient(), {"topics": {}}, "2026-09-12T21:00:00+03:00")
    assert result["summary"]["completed"] == 1
    assert result["summary"]["expected"] is None
    assert result["summary"]["is_complete"] is False


def test_client_claims_full_progress_but_missing_file_is_not_downloading(tmp_path: Path):
    client = FakeClient()
    client.progress = 1.0
    topic = {"id": "missing", "title": "Show [1 из 1]", "hash": "ABC", "save_path": str(tmp_path)}
    history: dict = {"topics": {}}
    result = reconcile_topic(topic, client, history, "2026-09-12T21:00:00+03:00")
    item = next(iter(history["topics"]["missing"]["items"].values()))
    assert item["status"] == "missing"
    assert result["summary"]["completed"] == 0


def test_auxiliary_numbered_file_cannot_complete_missing_episode(tmp_path: Path):
    class ExtrasClient(FakeClient):
        def inspect_torrent(self, infohash):
            return {
                "hash": infohash,
                "files": [
                    *(
                        {"name": f"Show/Show - {number:02d} [1080p].mkv", "size": 1, "progress": 1.0, "priority": 1}
                        for number in range(1, 12)
                    ),
                    {"name": "Extras/12.mkv", "size": 1, "progress": 1.0, "priority": 1},
                ],
            }

    (tmp_path / "Show").mkdir()
    (tmp_path / "Extras").mkdir()
    for number in range(1, 12):
        (tmp_path / "Show" / f"Show - {number:02d} [1080p].mkv").write_bytes(b"x")
    (tmp_path / "Extras" / "12.mkv").write_bytes(b"x")
    topic = {"id": "extras", "title": "Show [1-11 из 12]", "hash": "ABC", "save_path": str(tmp_path)}
    result = reconcile_topic(topic, ExtrasClient(), {"topics": {}}, "2026-09-12T21:00:00+03:00")
    assert result["summary"]["completed"] == 11
    assert result["summary"]["is_complete"] is False


def test_orphan_subtitle_cannot_complete_missing_video_episode(tmp_path: Path):
    class OrphanSubtitleClient(FakeClient):
        def inspect_torrent(self, infohash):
            return {
                "hash": infohash,
                "files": [
                    {"name": "Show/Show - 01 [1080p].mkv", "size": 1, "progress": 1.0},
                    {"name": "Subs/Show - 02 [1080p].ass", "size": 1, "progress": 1.0},
                ],
            }

    (tmp_path / "Show").mkdir()
    (tmp_path / "Subs").mkdir()
    (tmp_path / "Show" / "Show - 01 [1080p].mkv").write_bytes(b"x")
    (tmp_path / "Subs" / "Show - 02 [1080p].ass").write_bytes(b"x")
    topic = {"id": "orphan-sub", "title": "Show [1-2 из 2]", "hash": "ABC", "save_path": str(tmp_path)}
    result = reconcile_topic(topic, OrphanSubtitleClient(), {"topics": {}}, "2026-09-12T21:00:00+03:00")
    assert result["summary"]["completed"] == 1
    assert result["summary"]["is_complete"] is False


def test_legacy_same_path_without_hash_or_size_does_not_reannounce_completion(tmp_path: Path):
    client = FakeClient()
    client.progress = 1.0
    (tmp_path / "Show").mkdir()
    (tmp_path / "Show" / "S01E01.mkv").write_bytes(b"0123456789")
    topic = {"id": "legacy-complete", "title": "Show [1 из 1]", "hash": "ABC", "save_path": str(tmp_path)}
    history = {
        "topics": {
            "legacy-complete": {
                "baseline_at": "2026-09-12T20:00:00+03:00",
                "items": {
                    "file:show/s01e01.mkv": {
                        "identity": "file:show/s01e01.mkv",
                        "relative_path": "Show/S01E01.mkv",
                        "episode_key": "episode:s01e01",
                        "kind": "episode",
                        "status": "completed",
                        "completed_observed_at": "2026-09-12T19:00:00+03:00",
                    }
                },
            }
        }
    }
    result = reconcile_topic(topic, client, history, "2026-09-12T21:00:00+03:00")
    assert result["events"] == []
    assert result["summary"]["completed"] == 1


def test_season_relative_files_complete_absolute_title_window(tmp_path: Path):
    class SeasonTwoClient(FakeClient):
        def inspect_torrent(self, infohash):
            return {
                "hash": infohash,
                "files": [
                    {"name": f"Show/Show.S02E{episode:02d}.mkv", "size": 1, "progress": 1.0, "priority": 1}
                    for episode in range(1, 13)
                ],
            }

    (tmp_path / "Show").mkdir()
    for episode in range(1, 13):
        (tmp_path / "Show" / f"Show.S02E{episode:02d}.mkv").write_bytes(b"x")
    topic = {
        "id": "cour",
        "title": "Show Сезон 2 [Серии 13-24 из 24]",
        "hash": "ABC",
        "save_path": str(tmp_path),
    }
    history: dict = {"topics": {}}
    result = reconcile_topic(topic, SeasonTwoClient(), history, "2026-09-12T21:00:00+03:00")
    assert result["summary"]["completed"] == 12
    assert result["summary"]["expected"] == 12
    assert result["summary"]["is_complete"] is True
    assert history["topics"]["cour"]["expected"]["numbering"] == "season-relative"


def test_unknown_tracker_total_keeps_original_season_and_repairs_old_event(tmp_path: Path):
    class GrowingSeasonClient(FakeClient):
        def inspect_torrent(self, infohash):
            return {
                "hash": infohash,
                "files": [
                    {"name": f"Show 3/Show 3 - {episode:02d}.mkv", "size": 1, "progress": 1.0, "priority": 1}
                    for episode in range(1, 15)
                ],
            }

    folder = tmp_path / "Show 3"
    folder.mkdir()
    old_at = "2026-09-28T10:01:13+03:00"
    items = {}
    for episode in range(1, 15):
        rel = f"Show 3/Show 3 - {episode:02d}.mkv"
        (folder / f"Show 3 - {episode:02d}.mkv").write_bytes(b"x")
        identity = item_identity(rel, None, 1)
        items[identity] = {
            "identity": identity,
            "kind": "episode",
            "label": f"Серия {episode}",
            "episode_key": f"episode:e{episode:02d}",
            "episode_keys": [f"episode:e{episode:02d}"],
            "relative_path": rel,
            "source_hash": "ABC",
            "size": 1,
            "status": "completed",
            "completed_observed_at": old_at if episode == 14 else "2026-09-27T10:01:13+03:00",
        }
    topic = {
        "id": "growing",
        "title": "Show 3: Example [12 из 14]",
        "tracker_title": "Show III: Example [14 из ?]",
        "hash": "ABC",
        "save_path": str(tmp_path),
        "selected_episode_keys": [f"episode:e{episode:02d}" for episode in range(1, 15)],
    }
    history = {
        "topics": {
            "growing": {
                "baseline_at": "2026-09-24T09:15:29+03:00",
                "items": items,
                "last_event": {"kind": "episode_completed", "label": "Эпизод 14", "at": old_at},
            }
        }
    }

    result = reconcile_topic(topic, GrowingSeasonClient(), history, "2026-10-02T23:04:50+03:00")

    record = history["topics"]["growing"]
    assert result["events"] == []
    assert result["summary"]["completed"] == 14
    assert result["summary"]["expected"] == 14
    assert record["expected"]["keys"] == [f"episode:s03e{episode:02d}" for episode in range(1, 15)]
    assert record["last_event"]["label"] == "S03E14"
    assert record["last_event"]["at"] == old_at


@pytest.mark.parametrize(
    ("tracker_title", "season"),
    [
        ("Show III: Example [14 из ?]", 3),
        ("Show S01-S02 [14 из ?]", None),
        ("Show S04 [14 из ?]", 4),
    ],
)
def test_tracker_season_takes_priority_but_ambiguity_does_not_fall_back(tracker_title, season):
    topic = {"title": "Show 3: Example [12 из 14]", "tracker_title": tracker_title}
    assert _preferred_season(topic, ["episode:e14"]) == season


def test_season_number_in_files_resolves_title_window_without_title_season(tmp_path: Path):
    class SeasonTwoClient(FakeClient):
        def inspect_torrent(self, infohash):
            return {
                "hash": infohash,
                "files": [
                    {"name": f"Show S2 - {episode:02d}.mkv", "size": 1, "progress": 1.0, "priority": 1}
                    for episode in range(1, 14)
                ],
            }

    for episode in range(1, 14):
        (tmp_path / f"Show S2 - {episode:02d}.mkv").write_bytes(b"x")
    topic = {"id": "implicit-cour", "title": "Show [13-25 из 25]", "hash": "ABC", "save_path": str(tmp_path)}
    result = reconcile_topic(topic, SeasonTwoClient(), {"topics": {}}, "2026-09-12T21:00:00+03:00")
    assert result["summary"]["completed"] == 13
    assert result["summary"]["expected"] == 13
    assert result["summary"]["is_complete"] is True


def test_same_hash_client_rename_preserves_completion_without_new_events(tmp_path: Path):
    class RenameClient(FakeClient):
        file_name = "Show/Show.S01E01.mkv"

        def inspect_torrent(self, infohash):
            return {
                "hash": infohash,
                "files": [
                    {"name": self.file_name, "size": 1, "progress": 1.0, "priority": 1},
                ],
            }

    (tmp_path / "Show").mkdir()
    (tmp_path / "Show" / "Show.S01E01.mkv").write_bytes(b"x")
    client = RenameClient()
    topic = {"id": "rename", "title": "Show [1 из 1]", "hash": "ABC", "save_path": str(tmp_path)}
    history = {"topics": {"rename": {"baseline_at": "2026-09-12T20:00:00+03:00", "items": {}}}}
    first = reconcile_topic(topic, client, history, "2026-09-12T21:00:00+03:00")
    assert "episode_completed" in first["events"]
    (tmp_path / "Show" / "Show.S01E01.mkv").rename(tmp_path / "Show" / "Episode.S01E01.mkv")
    client.file_name = "Show/Episode.S01E01.mkv"
    second = reconcile_topic(topic, client, history, "2026-09-12T21:01:00+03:00")
    assert second["events"] == []
    assert second["summary"]["completed"] == 1
    assert len(history["topics"]["rename"]["items"]) == 1


def test_same_hash_rename_into_season_folder_keeps_completion_silent(tmp_path: Path):
    class RenameClient(FakeClient):
        file_name = "Show/Show - 01 [1080p].mkv"

        def inspect_torrent(self, infohash):
            return {
                "hash": infohash,
                "files": [
                    {"name": self.file_name, "size": 1, "progress": 1.0, "priority": 1},
                ],
            }

    (tmp_path / "Show").mkdir()
    (tmp_path / "Show" / "Show - 01 [1080p].mkv").write_bytes(b"x")
    client = RenameClient()
    topic = {"id": "folder-rename", "title": "Show [1 из 1]", "hash": "ABC", "save_path": str(tmp_path)}
    history = {"topics": {"folder-rename": {"baseline_at": "2026-09-12T20:00:00+03:00", "items": {}}}}
    reconcile_topic(topic, client, history, "2026-09-12T21:00:00+03:00")
    (tmp_path / "Show" / "Season 1").mkdir()
    (tmp_path / "Show" / "Show - 01 [1080p].mkv").rename(tmp_path / "Show" / "Season 1" / "Show - 01 [1080p].mkv")
    client.file_name = "Show/Season 1/Show - 01 [1080p].mkv"
    result = reconcile_topic(topic, client, history, "2026-09-12T21:01:00+03:00")
    assert result["events"] == []
    assert result["summary"]["completed"] == 1
    assert len(history["topics"]["folder-rename"]["items"]) == 1


def test_quality_suffix_cannot_falsely_complete_title_total(tmp_path: Path):
    class OneFileClient(FakeClient):
        def inspect_torrent(self, infohash):
            return {
                "hash": infohash,
                "files": [
                    {"name": "Show.S01E01-10bit.mkv", "size": 1, "progress": 1.0},
                ],
            }

    (tmp_path / "Show.S01E01-10bit.mkv").write_bytes(b"x")
    topic = {"id": "quality", "title": "Show Сезон 1, Серия 1 из 10", "hash": "ABC", "save_path": str(tmp_path)}
    result = reconcile_topic(topic, OneFileClient(), {"topics": {}}, "2026-09-12T21:00:00+03:00")
    assert result["summary"]["completed"] == 1
    assert result["summary"]["expected"] == 10
    assert result["summary"]["is_complete"] is False


def test_ep_prefix_quality_suffix_cannot_falsely_complete_title_total(tmp_path: Path):
    class OneFileClient(FakeClient):
        def inspect_torrent(self, infohash):
            return {
                "hash": infohash,
                "files": [
                    {"name": "Show.Ep01-10bit.mkv", "size": 1, "progress": 1.0},
                ],
            }

    (tmp_path / "Show.Ep01-10bit.mkv").write_bytes(b"x")
    topic = {"id": "ep-quality", "title": "Show [Серии 1-10 из 10]", "hash": "ABC", "save_path": str(tmp_path)}
    result = reconcile_topic(topic, OneFileClient(), {"topics": {}}, "2026-09-12T21:00:00+03:00")
    assert result["summary"]["completed"] == 1
    assert result["summary"]["expected"] == 10
    assert result["summary"]["is_complete"] is False


def test_mixed_seasonless_and_seasoned_videos_keep_distinct_progress(tmp_path: Path):
    class MixedClient(FakeClient):
        def inspect_torrent(self, infohash):
            return {
                "hash": infohash,
                "files": [
                    {"name": "Show - 01 [1080p].mkv", "size": 1, "progress": 1.0},
                    {"name": "Show S2 - 01.mkv", "size": 1, "progress": 1.0},
                ],
            }

    (tmp_path / "Show - 01 [1080p].mkv").write_bytes(b"x")
    (tmp_path / "Show S2 - 01.mkv").write_bytes(b"x")
    topic = {"id": "mixed", "title": "Show", "hash": "ABC", "save_path": str(tmp_path)}
    result = reconcile_topic(topic, MixedClient(), {"topics": {}}, "2026-09-12T21:00:00+03:00")
    assert result["summary"]["completed"] == 2
    assert result["summary"]["expected"] == 2


def test_all_file_episode_count_tracks_current_files_without_claiming_series_complete(
    tmp_path: Path,
):
    class CurrentFilesClient:
        client_id = "fake-main"
        client_kind = "fake"
        capabilities: ClassVar[dict[str, bool]] = {"inspect": True}

        def __init__(self):
            self.files = [
                {"name": f"Show/S01E{episode:02d}.mkv", "size": 10, "progress": 0, "priority": 1} for episode in (1, 2)
            ]

        def inspect_torrent(self, infohash):
            return {"hash": infohash, "state": "queuedDL", "files": self.files}

    client = CurrentFilesClient()
    topic = {
        "id": "current-files",
        "title": "Show",
        "hash": "ABC123",
        "save_path": str(tmp_path),
        "selection": {"mode": "all"},
        "selection_verified": True,
        "selected_episode_keys": ["episode:s01e01", "episode:s01e02"],
    }
    history = {"schema_version": 1, "topics": {}}

    first = reconcile_topic(topic, client, history, now="2026-09-24T17:00:00+03:00")
    assert first["summary"]["expected"] == 2
    assert first["summary"]["completed"] == 0
    assert first["summary"]["completion_known"] is False
    assert history["topics"]["current-files"]["expected"]["source"] == "files"

    (tmp_path / "Show").mkdir()
    for episode in (1, 2):
        (tmp_path / "Show" / f"S01E{episode:02d}.mkv").write_bytes(b"0123456789")
        client.files[episode - 1]["progress"] = 1
    second = reconcile_topic(topic, client, history, now="2026-09-24T17:01:00+03:00")
    assert second["summary"]["completed"] == 2
    assert second["summary"]["is_complete"] is False

    client.files.append({"name": "Show/S01E03.mkv", "size": 10, "progress": 0, "priority": 1})
    third = reconcile_topic(topic, client, history, now="2026-09-24T17:02:00+03:00")
    assert third["summary"]["expected"] == 3
    assert third["summary"]["completed"] == 2


def test_client_episode_files_supply_count_when_topic_has_no_saved_keys(tmp_path: Path):
    class Client:
        client_id = "fake-main"
        client_kind = "fake"
        capabilities: ClassVar[dict[str, bool]] = {"inspect": True}

        def inspect_torrent(self, infohash):
            return {
                "hash": infohash,
                "files": [
                    {"name": "Show/S01E01.mkv", "size": 10, "progress": 0, "priority": 1},
                    {"name": "Show/S01E02.mkv", "size": 10, "progress": 0, "priority": 1},
                ],
            }

    topic = {
        "id": "missing-keys",
        "title": "Show",
        "hash": "ABC123",
        "save_path": str(tmp_path),
        "selection": {"mode": "all"},
    }
    history = {"schema_version": 1, "topics": {}}

    result = reconcile_topic(topic, Client(), history, now="2026-09-24T17:00:00+03:00")

    assert result["summary"]["expected"] == 2
    assert history["topics"]["missing-keys"]["expected"]["source"] == "files"


def test_partial_selection_matches_legacy_seasonless_key_to_explicit_file_season(tmp_path: Path):
    class Client:
        client_id = "fake-main"
        client_kind = "fake"
        capabilities: ClassVar[dict[str, bool]] = {"inspect": True}

        def inspect_torrent(self, infohash):
            return {
                "hash": infohash,
                "files": [{"name": "Show/S03E14.mkv", "size": 10, "progress": 1.0, "priority": 1}],
            }

    folder = tmp_path / "Show"
    folder.mkdir()
    (folder / "S03E14.mkv").write_bytes(b"0123456789")
    topic = {
        "id": "legacy-partial-season",
        "title": "Show S03 [14 из 14]",
        "hash": "ABC123",
        "save_path": str(tmp_path),
        "selection": {"mode": "episodes", "value": "14"},
        "selected_episode_keys": ["episode:e14"],
    }
    history = {"topics": {}}

    result = reconcile_topic(topic, Client(), history, now="2026-09-24T17:00:00+03:00")

    assert result["summary"]["expected"] == 1
    assert result["summary"]["completed"] == 1
    assert history["topics"]["legacy-partial-season"]["expected"]["keys"] == ["episode:s03e14"]
    assert result["events"] == []
    repeat = reconcile_topic(topic, Client(), history, now="2026-09-24T17:01:00+03:00")
    assert repeat["summary"]["completed"] == 1
    assert repeat["events"] == []


@pytest.mark.parametrize(
    ("title", "episodes", "selected_keys", "completed", "expected_keys"),
    [
        (
            "Show S03 [14 из 14]",
            ("S03E14", "S04E14"),
            ("episode:e14",),
            0,
            ["episode:e14"],
        ),
        (
            "Show S04 [14 из 14]",
            ("S03E14",),
            ("episode:e14",),
            0,
            ["episode:e14"],
        ),
        (
            "Show S03 [14 из 15]",
            ("S03E14",),
            ("episode:e14", "episode:e15"),
            1,
            ["episode:s03e14", "episode:e15"],
        ),
    ],
)
def test_partial_legacy_key_alignment_requires_matching_unambiguous_selected_files(
    tmp_path: Path,
    title: str,
    episodes: tuple[str, ...],
    selected_keys: tuple[str, ...],
    completed: int,
    expected_keys: list[str],
):
    class Client:
        client_id = "fake-main"
        client_kind = "fake"
        capabilities: ClassVar[dict[str, bool]] = {"inspect": True}

        def inspect_torrent(self, infohash):
            return {
                "hash": infohash,
                "files": [
                    {"name": f"Show/{episode}.mkv", "size": 10, "progress": 1.0, "priority": 1} for episode in episodes
                ],
            }

    folder = tmp_path / "Show"
    folder.mkdir()
    for episode in episodes:
        (folder / f"{episode}.mkv").write_bytes(b"0123456789")
    topic = {
        "id": "partial-ambiguous",
        "title": title,
        "hash": "ABC123",
        "save_path": str(tmp_path),
        "selection": {"mode": "episodes", "value": "14"},
        "selected_episode_keys": list(selected_keys),
    }
    history = {"topics": {}}

    result = reconcile_topic(topic, Client(), history, now="2026-09-24T17:00:00+03:00")

    assert result["summary"]["expected"] == len(selected_keys)
    assert result["summary"]["completed"] == completed
    assert history["topics"]["partial-ambiguous"]["expected"]["keys"] == expected_keys


def test_initial_reconcile_is_baseline_not_a_new_completion(tmp_path: Path):
    client = FakeClient()
    client.progress = 1.0
    (tmp_path / "Show").mkdir()
    (tmp_path / "Show" / "S01E01.mkv").write_bytes(b"0123456789")
    topic = {
        "id": "topic-baseline",
        "title": "Show [01x01-01 из 1]",
        "hash": "ABC123",
        "save_path": str(tmp_path),
    }
    history = {"schema_version": 1, "topics": {}}
    result = reconcile_topic(topic, client, history, now="2026-09-12T21:00:00+03:00")
    record = history["topics"]["topic-baseline"]
    assert result["events"] == []
    assert record["baseline_at"] == "2026-09-12T21:00:00+03:00"
    item = next(iter(record["items"].values()))
    assert item["episode_key"] == "episode:s01e01"
    assert item["status"] == "completed"


def test_baseline_multifile_uses_torrent_event_not_client_file_order(tmp_path: Path):
    class MultiFileClient:
        client_id = "fake-main"
        client_kind = "fake"
        capabilities: ClassVar[dict[str, bool]] = {"inspect": True, "list_files": True, "completion_time": True}

        def inspect_torrent(self, infohash):
            return {
                "hash": infohash,
                "progress": 1.0,
                "added_on": 1789150747,
                "completion_on": 1789151447,
                "files": [
                    {"name": "Show/Show.S01E03.mkv", "size": 1, "progress": 1.0},
                    {"name": "Show/Show.S01E02.mkv", "size": 1, "progress": 1.0},
                    {"name": "Show/Show.S01E05.mkv", "size": 1, "progress": 1.0},
                    {"name": "Show/Show.S01E01.mkv", "size": 1, "progress": 1.0},
                    {"name": "Show/Show.S01E04.mkv", "size": 1, "progress": 1.0},
                ],
            }

    show = tmp_path / "Show"
    show.mkdir()
    for episode in ("E01", "E02", "E03", "E04", "E05"):
        (show / f"Show.S01{episode}.mkv").write_bytes(b"x")
    topic = {"id": "topic-order", "title": "Show [01x01-05 из 10]", "hash": "ABC123", "save_path": str(tmp_path)}

    history = {"schema_version": 1, "topics": {}}
    result = reconcile_topic(topic, MultiFileClient(), history, now="2026-09-12T23:29:18+03:00")
    record = history["topics"]["topic-order"]

    assert result["events"] == []
    assert "last_completed" not in record
    assert record["last_event"]["kind"] == "torrent_completed"
    assert record["last_event"]["at"] == "2026-09-11T21:30:47+03:00"
    assert record["last_event"]["label"] == "S01E05"
    assert [item["label"] for item in record["items"].values()] == ["S01E03", "S01E02", "S01E05", "S01E01", "S01E04"]


def test_reconcile_repairs_synthetic_baseline_completion(tmp_path: Path):
    class ClientWithCompletion:
        client_id = "fake-main"
        client_kind = "fake"
        capabilities: ClassVar[dict[str, bool]] = {"inspect": True, "list_files": True, "completion_time": True}

        def inspect_torrent(self, infohash):
            return {
                "hash": infohash,
                "progress": 1.0,
                "added_on": 1789150747,
                "completion_on": 1789151447,
                "files": [{"name": "Show/S01E03.mkv", "size": 1, "progress": 1.0}],
            }

    (tmp_path / "Show").mkdir()
    (tmp_path / "Show" / "S01E03.mkv").write_bytes(b"x")
    topic = {"id": "topic-repair", "title": "Show [01x01-03 из 3]", "hash": "ABC123", "save_path": str(tmp_path)}
    synthetic = {
        "identity": "episode:s01e03",
        "kind": "episode",
        "label": "S01E03",
        "relative_path": "Show/S01E03.mkv",
        "first_seen_at": "2026-09-12T23:29:18+03:00",
        "completed_observed_at": "2026-09-12T23:29:18+03:00",
        "new_after_baseline": False,
        "status": "completed",
    }
    history = {
        "schema_version": 1,
        "topics": {
            "topic-repair": {
                "baseline_at": "2026-09-12T23:31:38+03:00",
                "items": {"episode:s01e03": synthetic},
                "last_completed": dict(synthetic),
            }
        },
    }

    reconcile_topic(topic, ClientWithCompletion(), history, now="2026-09-12T23:35:00+03:00")
    record = history["topics"]["topic-repair"]
    item = next(iter(record["items"].values()))

    assert "completed_observed_at" not in item
    assert "last_completed" not in record
    assert record["last_event"]["kind"] == "torrent_completed"
    assert record["last_event"]["label"] == "S01E03"


def test_reconcile_repairs_legacy_bare_episode_file_event(tmp_path: Path):
    class LegacyClient:
        client_id = "fake-main"
        client_kind = "fake"
        capabilities: ClassVar[dict[str, bool]] = {
            "inspect": True,
            "list_files": True,
            "completion_time": False,
        }

        def inspect_torrent(self, infohash):
            return {
                "hash": infohash,
                "progress": 1.0,
                "completion_on": None,
                "files": [
                    {
                        "name": "Show/Koukaku Kidoutai (2026) S1 - 10.mkv",
                        "size": 1,
                        "progress": 1.0,
                    }
                ],
            }

    show = tmp_path / "Show"
    show.mkdir()
    (show / "Koukaku Kidoutai (2026) S1 - 10.mkv").write_bytes(b"x")
    identity = "file:show/koukaku kidoutai (2026) s1 - 10.mkv:1"
    topic = {
        "id": "topic-legacy-label",
        "title": "Show D [S01]",
        "hash": "ABC123",
        "save_path": str(tmp_path),
    }
    history = {
        "schema_version": 1,
        "topics": {
            "topic-legacy-label": {
                "baseline_at": "2026-09-14T21:00:00+03:00",
                "items": {
                    identity: {
                        "identity": identity,
                        "kind": "file",
                        "label": "Koukaku Kidoutai (2026) S1 - 10.mkv",
                        "relative_path": "Show/Koukaku Kidoutai (2026) S1 - 10.mkv",
                        "source_hash": "ABC123",
                        "size": 1,
                        "status": "completed",
                    }
                },
                "last_event": {
                    "kind": "file_completed",
                    "label": "Koukaku Kidoutai (2026) S1 - 10.mkv",
                    "item": identity,
                    "relative_path": "Show/Koukaku Kidoutai (2026) S1 - 10.mkv",
                    "at": "2026-09-14T22:01:11+03:00",
                },
            }
        },
    }

    result = reconcile_topic(
        topic,
        LegacyClient(),
        history,
        now="2026-09-24T09:30:00+03:00",
    )

    event = history["topics"]["topic-legacy-label"]["last_event"]
    item = next(iter(history["topics"]["topic-legacy-label"]["items"].values()))
    assert result["events"] == []
    assert item["kind"] == "episode"
    assert item["label"] == "S01E10"
    assert event["kind"] == "episode_completed"
    assert event["label"] == "S01E10"


def test_ona_history_gains_episode_progress_without_repeating_completion(tmp_path: Path):
    class Client:
        client_id = "fake-main"
        client_kind = "fake"
        capabilities: ClassVar[dict[str, bool]] = {"inspect": True}

        def inspect_torrent(self, infohash):
            return {
                "hash": infohash,
                "files": [{"name": "Show/Show.ONA.S01E02.mkv", "size": 10, "progress": 1.0, "priority": 1}],
            }

    folder = tmp_path / "Show"
    folder.mkdir()
    (folder / "Show.ONA.S01E02.mkv").write_bytes(b"0123456789")
    rel = "Show/Show.ONA.S01E02.mkv"
    identity = item_identity(rel, None, 10)
    old_at = "2026-09-14T22:01:11+03:00"
    history = {
        "topics": {
            "ona": {
                "baseline_at": "2026-09-14T21:00:00+03:00",
                "items": {
                    identity: {
                        "identity": identity,
                        "kind": "file",
                        "label": "Show.ONA.S01E02.mkv",
                        "relative_path": rel,
                        "source_hash": "ABC",
                        "size": 10,
                        "status": "completed",
                        "completed_observed_at": old_at,
                    }
                },
                "last_event": {
                    "kind": "file_completed",
                    "label": "Show.ONA.S01E02.mkv",
                    "item": identity,
                    "relative_path": rel,
                    "at": old_at,
                },
            }
        }
    }
    topic = {"id": "ona", "title": "Show [2 из 2]", "hash": "ABC", "save_path": str(tmp_path)}

    result = reconcile_topic(topic, Client(), history, "2026-10-03T01:00:00+03:00")

    record = history["topics"]["ona"]
    assert result["events"] == []
    assert result["summary"]["completed"] == 1
    assert result["summary"]["expected"] == 2
    assert record["items"][identity]["episode_key"] == "episode:s01e02"
    assert record["items"][identity]["completed_observed_at"] == old_at
    assert record["last_event"]["kind"] == "episode_completed"
    assert record["last_event"]["label"] == "S01E02"
    assert record["last_event"]["at"] == old_at


def test_multiseason_ona_release_uses_known_future_total(tmp_path: Path):
    class Client:
        client_id = "fake-main"
        client_kind = "fake"
        capabilities: ClassVar[dict[str, bool]] = {"inspect": True}

        def inspect_torrent(self, infohash):
            return {
                "hash": infohash,
                "files": [
                    {"name": f"Show/Show.ONA.{season}.mkv", "size": 10, "progress": 1.0, "priority": 1}
                    for season in ("S01E01", "S02E01")
                ],
            }

    folder = tmp_path / "Show"
    folder.mkdir()
    for season in ("S01E01", "S02E01"):
        (folder / f"Show.ONA.{season}.mkv").write_bytes(b"0123456789")
    topic = {
        "id": "ona-multiseason",
        "title": "Show (1-2 сезоны: 1-2 серии из 4)",
        "hash": "ABC",
        "save_path": str(tmp_path),
    }

    result = reconcile_topic(topic, Client(), {"topics": {}}, "2026-10-03T01:00:00+03:00")

    assert result["summary"]["completed"] == 2
    assert result["summary"]["expected"] == 4
    assert result["summary"]["completion_known"] is True
    assert result["summary"]["is_complete"] is False


def test_reconcile_applies_corroborated_season_hint_to_bare_anime_episodes(
    tmp_path: Path,
):
    class AnimeClient:
        client_id = "fake-main"
        client_kind = "fake"
        capabilities: ClassVar[dict[str, bool]] = {
            "inspect": True,
            "list_files": True,
            "completion_time": True,
        }

        def inspect_torrent(self, infohash):
            return {
                "hash": infohash,
                "progress": 0.5,
                "added_on": 1790230524,
                "completion_on": None,
                "files": [
                    {
                        "name": "Some Show 3/Some Show 3 - 10 RAW.mkv",
                        "size": 2,
                        "progress": 1.0,
                    },
                    {
                        "name": "Some Show 3/Some Show 3 - 13 RAW.mkv",
                        "size": 2,
                        "progress": 0.5,
                    },
                ],
            }

    topic = {
        "id": "topic-season-hint",
        "title": "Сериал В 3: Другой мир [13 из 14]",
        "hash": "ABC123",
        "save_path": str(tmp_path),
    }
    history = {"schema_version": 1, "topics": {}}
    show = tmp_path / "Some Show 3"
    show.mkdir()
    (show / "Some Show 3 - 10 RAW.mkv").write_bytes(b"ok")

    reconcile_topic(
        topic,
        AnimeClient(),
        history,
        now="2026-09-24T10:00:00+03:00",
    )

    record = history["topics"]["topic-season-hint"]
    assert {item["episode_key"] for item in record["items"].values()} == {
        "episode:s03e10",
        "episode:s03e13",
    }
    assert {item["label"] for item in record["items"].values()} == {
        "S03E10",
        "S03E13",
    }
    assert record["last_event"]["label"] == "S03E13"


def test_single_non_episode_file_uses_filename_label(tmp_path: Path):
    class SingleFileClient:
        client_id = "fake-main"
        client_kind = "fake"
        capabilities: ClassVar[dict[str, bool]] = {"inspect": True, "list_files": True, "completion_time": True}

        def inspect_torrent(self, infohash):
            return {
                "hash": infohash,
                "progress": 1.0,
                "added_on": 1789150747,
                "completion_on": 1789151447,
                "files": [{"name": "Movie/movie.mkv", "size": 1, "progress": 1.0}],
            }

    (tmp_path / "Movie").mkdir()
    (tmp_path / "Movie" / "movie.mkv").write_bytes(b"x")
    topic = {"id": "topic-file", "title": "Movie", "hash": "ABC123", "save_path": str(tmp_path)}
    history = {"schema_version": 1, "topics": {}}

    reconcile_topic(topic, SingleFileClient(), history, now="2026-09-12T23:40:00+03:00")
    event = history["topics"]["topic-file"]["last_event"]
    assert event["kind"] == "torrent_completed"
    assert event["label"] == "movie.mkv"


def test_same_hash_size_readback_correction_is_silent(tmp_path: Path):
    class ReuploadClient:
        client_id = "fake-main"
        client_kind = "fake"
        capabilities: ClassVar[dict[str, bool]] = {"inspect": True, "list_files": True, "completion_time": False}
        size = 1

        def inspect_torrent(self, infohash):
            return {
                "hash": infohash,
                "progress": 1.0,
                "files": [{"name": "Movie/movie.mkv", "size": self.size, "progress": 1.0}],
            }

    (tmp_path / "Movie").mkdir()
    path = tmp_path / "Movie" / "movie.mkv"
    path.write_bytes(b"x")
    client = ReuploadClient()
    topic = {"id": "topic-reupload", "title": "Movie", "hash": "ABC123", "save_path": str(tmp_path)}
    history = {"schema_version": 1, "topics": {}}

    reconcile_topic(topic, client, history, now="2026-09-12T23:40:00+03:00")
    client.size = 2
    path.write_bytes(b"xy")
    result = reconcile_topic(topic, client, history, now="2026-09-12T23:41:00+03:00")

    assert result["events"] == []
    assert len(history["topics"]["topic-reupload"]["items"]) == 1


def test_multiple_non_episode_files_use_aggregate_files_label(tmp_path: Path):
    class MultiFileClient:
        client_id = "fake-main"
        client_kind = "fake"
        capabilities: ClassVar[dict[str, bool]] = {"inspect": True, "list_files": True, "completion_time": True}

        def inspect_torrent(self, infohash):
            return {
                "hash": infohash,
                "progress": 1.0,
                "completion_on": 1789151447,
                "files": [
                    {"name": "bundle/a.bin", "size": 1, "progress": 1.0},
                    {"name": "bundle/b.bin", "size": 1, "progress": 1.0},
                ],
            }

    (tmp_path / "bundle").mkdir()
    (tmp_path / "bundle" / "a.bin").write_bytes(b"x")
    (tmp_path / "bundle" / "b.bin").write_bytes(b"x")
    topic = {"id": "topic-files", "title": "Bundle", "hash": "ABC123", "save_path": str(tmp_path)}
    history = {"schema_version": 1, "topics": {}}

    reconcile_topic(topic, MultiFileClient(), history, now="2026-09-12T23:42:00+03:00")
    assert history["topics"]["topic-files"]["last_event"]["label"] == "файлы"


def test_same_path_and_size_with_new_hash_is_new_revision(tmp_path: Path):
    class ReuploadClient:
        client_id = "fake-main"
        client_kind = "fake"
        capabilities: ClassVar[dict[str, bool]] = {"inspect": True, "list_files": True, "completion_time": False}

        def inspect_torrent(self, infohash):
            return {
                "hash": infohash,
                "progress": 1.0,
                "files": [{"name": "Movie/movie.mkv", "size": 10, "progress": 1.0}],
            }

    folder = tmp_path / "Movie"
    folder.mkdir()
    (folder / "movie.mkv").write_bytes(b"0123456789")
    client = ReuploadClient()
    history = {"schema_version": 1, "topics": {}}
    topic = {"id": "topic-hash-reupload", "title": "Movie", "hash": "HASH-ONE", "save_path": str(tmp_path)}

    reconcile_topic(topic, client, history, now="2026-09-12T23:40:00+03:00")
    topic["hash"] = "HASH-TWO"
    result = reconcile_topic(topic, client, history, now="2026-09-12T23:41:00+03:00")

    assert result["events"] == ["revision_updated"]
    assert len(history["topics"]["topic-hash-reupload"]["items"]) == 2


def test_all_mode_uses_tracker_total_not_current_selection_count():
    items = {
        f"file:show/s04e{episode:02d}.mkv:1": {
            "identity": f"file:show/s04e{episode:02d}.mkv:1",
            "kind": "episode",
            "episode_key": f"episode:s04e{episode:02d}",
            "episode_keys": [f"episode:s04e{episode:02d}"],
            "status": "completed",
        }
        for episode in range(1, 23)
    }
    summary = summarize_completion(items, {"kind": "episodes", "total": 24})
    assert summary["completed"] == 22
    assert summary["expected"] == 24


def test_narrow_selection_filters_old_completed_history():
    items = {
        f"episode:s01e{episode:02d}": {
            "kind": "episode",
            "episode_key": f"episode:s01e{episode:02d}",
            "status": "completed",
        }
        for episode in range(1, 13)
    }
    expected = {
        "kind": "episodes",
        "total": 4,
        "keys": [f"episode:s01e{episode:02d}" for episode in range(9, 13)],
    }
    summary = summarize_completion(items, expected)
    assert summary["completed"] == 4
    assert summary["expected"] == 4


def test_episode_completes_only_when_all_selected_occurrences_complete(tmp_path: Path):
    class VideoAndSubtitleClient:
        client_id = "fake-main"
        client_kind = "fake"
        capabilities: ClassVar[dict[str, bool]] = {"inspect": True, "list_files": True}
        subtitle_progress = 0.0

        def inspect_torrent(self, infohash):
            return {
                "hash": infohash,
                "files": [
                    {"name": "Show/S01E01.mkv", "size": 1, "progress": 1.0, "priority": 1},
                    {
                        "name": "Show/S01E01.srt",
                        "size": 1,
                        "progress": self.subtitle_progress,
                        "priority": 1,
                    },
                    {"name": "Show/extra.txt", "size": 1, "progress": 1.0, "priority": 0},
                ],
            }

    folder = tmp_path / "Show"
    folder.mkdir()
    (folder / "S01E01.mkv").write_bytes(b"x")
    client = VideoAndSubtitleClient()
    topic = {
        "id": "topic-members",
        "title": "Show",
        "hash": "HASH",
        "save_path": str(tmp_path),
        "selected_episode_keys": ["episode:s01e01"],
    }
    history = {"schema_version": 1, "topics": {}}

    first = reconcile_topic(topic, client, history, now="2026-09-12T20:00:00+03:00")
    assert first["summary"]["completed"] == 0
    assert len(history["topics"]["topic-members"]["items"]) == 2

    (folder / "S01E01.srt").write_bytes(b"x")
    client.subtitle_progress = 1.0
    second = reconcile_topic(topic, client, history, now="2026-09-12T20:01:00+03:00")
    assert second["events"] == ["episode_completed"]
    assert second["summary"]["completed"] == 1


def test_renamed_file_in_new_revision_supersedes_old_episode_member(tmp_path: Path):
    class RenamedClient:
        client_id = "fake-main"
        client_kind = "fake"
        capabilities: ClassVar[dict[str, bool]] = {"inspect": True, "list_files": True}

        def inspect_torrent(self, infohash):
            return {
                "hash": infohash,
                "files": [
                    {
                        "name": "Show/renamed.S01E01.mkv",
                        "size": 1,
                        "progress": 1.0,
                        "priority": 1,
                    }
                ],
            }

    folder = tmp_path / "Show"
    folder.mkdir()
    (folder / "renamed.S01E01.mkv").write_bytes(b"x")
    history = {
        "schema_version": 1,
        "topics": {
            "topic-rename": {
                "baseline_at": "2026-09-12T20:00:00+03:00",
                "items": {
                    "old": {
                        "identity": "old",
                        "kind": "episode",
                        "episode_key": "episode:s01e01",
                        "episode_keys": ["episode:s01e01"],
                        "relative_path": "Show/old.S01E01.mkv",
                        "source_hash": "OLD",
                        "status": "downloading",
                    }
                },
            }
        },
    }
    topic = {
        "id": "topic-rename",
        "title": "Show",
        "hash": "NEW",
        "save_path": str(tmp_path),
        "selection": {"mode": "episodes", "value": "S01E01"},
        "selected_episode_keys": ["episode:s01e01"],
    }

    result = reconcile_topic(topic, RenamedClient(), history, now="2026-09-12T20:01:00+03:00")

    assert history["topics"]["topic-rename"]["items"]["old"]["superseded"] is True
    assert result["summary"]["completed"] == 1
    assert result["events"] == ["new_file", "episode_completed"]


def test_dual_numbering_rekey_is_silent_and_does_not_overcount(tmp_path: Path):
    class Client:
        client_id = "fake-main"
        client_kind = "fake"
        capabilities: ClassVar[dict[str, bool]] = {"inspect": True, "list_files": True}

        def inspect_torrent(self, infohash):
            return {
                "hash": infohash,
                "files": [
                    {"name": "Show/Show.S03E02-E074.mkv", "size": 1, "progress": 1.0},
                    {"name": "Show/Show.S03E03-E075.mkv", "size": 1, "progress": 1.0},
                ],
            }

    folder = tmp_path / "Show"
    folder.mkdir()
    for name in ("Show.S03E02-E074.mkv", "Show.S03E03-E075.mkv"):
        (folder / name).write_bytes(b"x")
    topic = {
        "id": "dual",
        "title": "Show [03x02-03 из 74]",
        "hash": "HASH",
        "save_path": str(tmp_path),
    }
    history = {
        "schema_version": 1,
        "topics": {
            "dual": {
                "baseline_at": "2026-01-01T00:00:00+00:00",
                "items": {
                    f"file:show/show.s03e{episode:02d}-e0{episode + 72}.mkv:1": {
                        "identity": f"file:show/show.s03e{episode:02d}-e0{episode + 72}.mkv:1",
                        "relative_path": f"Show/Show.S03E{episode:02d}-E0{episode + 72}.mkv",
                        "source_hash": "HASH",
                        "size": 1,
                        "episode_key": f"episode:s03e{episode:02d}",
                        "episode_keys": [f"episode:s03e{episode:02d}"],
                        "label": f"S03E{episode:02d}",
                        "status": "completed",
                    }
                    for episode in (2, 3)
                },
            }
        },
    }
    result = reconcile_topic(topic, Client(), history, now="2026-01-01T01:00:00+00:00")
    assert result["events"] == []
    assert result["summary"]["completed"] == 2
    assert result["summary"]["completion_inconsistent"] is False


def test_carried_revision_waits_for_fresh_evidence_and_stays_event_silent(tmp_path: Path):
    class Client:
        client_id = "fake-main"
        client_kind = "fake"
        capabilities: ClassVar[dict[str, bool]] = {"inspect": True, "list_files": True}
        progress = 0.0

        def inspect_torrent(self, infohash):
            return {
                "hash": infohash,
                "files": [{"name": "Show/S01E01.mkv", "size": 1, "progress": self.progress}],
            }

    folder = tmp_path / "Show"
    folder.mkdir()
    path = folder / "S01E01.mkv"
    path.write_bytes(b"x")
    client = Client()
    topic = {"id": "carry", "title": "Show из 1", "hash": "OLD", "save_path": str(tmp_path)}
    history = {"schema_version": 1, "topics": {}}
    client.progress = 1.0
    reconcile_topic(topic, client, history, now="2026-01-01T00:00:00+00:00")
    topic["hash"] = "NEW"
    client.progress = 0.0
    first = reconcile_topic(topic, client, history, now="2026-01-01T01:00:00+00:00")
    assert first["events"] == ["revision_updated"]
    assert first["summary"]["completed"] == 0
    client.progress = 1.0
    second = reconcile_topic(topic, client, history, now="2026-01-01T02:00:00+00:00")
    assert second["events"] == []
    assert second["summary"]["completed"] == 1


def test_hash_rollback_reactivates_matching_history(tmp_path: Path):
    class Client:
        client_id = "fake-main"
        client_kind = "fake"
        capabilities: ClassVar[dict[str, bool]] = {"inspect": True, "list_files": True}

        def inspect_torrent(self, infohash):
            return {
                "hash": infohash,
                "files": [{"name": "Show/S01E01.mkv", "size": 1, "progress": 1.0}],
            }

    folder = tmp_path / "Show"
    folder.mkdir()
    (folder / "S01E01.mkv").write_bytes(b"x")
    topic = {"id": "rollback", "title": "Show из 1", "hash": "H1", "save_path": str(tmp_path)}
    history = {"schema_version": 1, "topics": {}}
    reconcile_topic(topic, Client(), history, now="2026-01-01T00:00:00+00:00")
    topic["hash"] = "H2"
    reconcile_topic(topic, Client(), history, now="2026-01-01T01:00:00+00:00")
    topic["hash"] = "h1"
    result = reconcile_topic(topic, Client(), history, now="2026-01-01T02:00:00+00:00")
    assert result["summary"]["completed"] == 1
    assert result["events"] == []


def test_changed_size_on_new_hash_is_revision_not_new_file(tmp_path: Path):
    class Client:
        client_id = "fake-main"
        client_kind = "fake"
        capabilities: ClassVar[dict[str, bool]] = {"inspect": True, "list_files": True}
        size = 1

        def inspect_torrent(self, infohash):
            return {
                "hash": infohash,
                "files": [{"name": "Movie/movie.mkv", "size": self.size, "progress": 1.0}],
            }

    folder = tmp_path / "Movie"
    folder.mkdir()
    path = folder / "movie.mkv"
    path.write_bytes(b"x")
    client = Client()
    topic = {"id": "resize", "title": "Movie", "hash": "H1", "save_path": str(tmp_path)}
    history = {"schema_version": 1, "topics": {}}
    reconcile_topic(topic, client, history, now="2026-01-01T00:00:00+00:00")
    topic["hash"] = "H2"
    client.size = 2
    path.write_bytes(b"xx")
    result = reconcile_topic(topic, client, history, now="2026-01-01T01:00:00+00:00")
    assert "revision_updated" in result["events"]
    assert "new_file" not in result["events"]


def test_client_save_path_drift_is_explicit_error(tmp_path: Path):
    class Client:
        client_id = "fake-main"
        client_kind = "fake"
        capabilities: ClassVar[dict[str, bool]] = {"inspect": True, "list_files": True}

        def inspect_torrent(self, infohash):
            return {"hash": infohash, "save_path": str(tmp_path / "other"), "files": []}

    topic = {"id": "path", "title": "Show", "hash": "H", "save_path": str(tmp_path)}
    with raises_code("progress.path_differs", RuntimeError):
        reconcile_topic(topic, Client(), {"schema_version": 1, "topics": {}}, now="2026-01-01T00:00:00+00:00")


def test_impossible_completed_count_is_capped_and_flagged():
    items = {
        f"item-{episode}": {
            "episode_key": f"episode:s01e{episode:02d}",
            "status": "completed",
        }
        for episode in range(1, 4)
    }
    summary = summarize_completion(items, {"kind": "episodes", "total": 2})
    assert summary["completed"] == 2
    assert summary["is_complete"] is False
    assert summary["completion_inconsistent"] is True


def test_partial_dual_numbering_uses_full_file_set_for_disambiguation(tmp_path: Path):
    class Client:
        client_id = "fake-main"
        client_kind = "fake"
        capabilities: ClassVar[dict[str, bool]] = {"inspect": True, "list_files": True}

        def inspect_torrent(self, infohash):
            return {
                "hash": infohash,
                "files": [
                    {
                        "name": f"Show/Show.S02E{episode:02d}-E{episode + 12:02d}.mkv",
                        "size": 1,
                        "progress": 1.0 if episode == 5 else 0.0,
                        "priority": 1 if episode == 5 else 0,
                    }
                    for episode in range(1, 11)
                ],
            }

    folder = tmp_path / "Show"
    folder.mkdir()
    (folder / "Show.S02E05-E17.mkv").write_bytes(b"x")
    topic = {
        "id": "partial-dual",
        "title": "Show",
        "hash": "HASH",
        "save_path": str(tmp_path),
        "selection": {"mode": "episodes", "value": "S02E05"},
        "selected_episode_keys": ["episode:s02e05"],
    }
    history = {"schema_version": 1, "topics": {}}
    reconcile_topic(topic, Client(), history, now="2026-01-01T00:00:00+00:00")
    item = next(iter(history["topics"]["partial-dual"]["items"].values()))
    assert item["episode_keys"] == ["episode:s02e05"]
    assert item["label"] == "S02E05"


def test_same_hash_size_metadata_upgrade_is_silent(tmp_path: Path):
    class Client:
        client_id = "fake-main"
        client_kind = "fake"
        capabilities: ClassVar[dict[str, bool]] = {"inspect": True, "list_files": True}
        size = None

        def inspect_torrent(self, infohash):
            return {
                "hash": infohash,
                "files": [{"name": "Show/S01E01.mkv", "size": self.size, "progress": 0.5}],
            }

    client = Client()
    topic = {"id": "size-upgrade", "title": "Show", "hash": "HASH", "save_path": str(tmp_path)}
    history = {"schema_version": 1, "topics": {}}
    reconcile_topic(topic, client, history, now="2026-01-01T00:00:00+00:00")
    client.size = 10
    result = reconcile_topic(topic, client, history, now="2026-01-01T01:00:00+00:00")
    assert result["events"] == []
    assert len(history["topics"]["size-upgrade"]["items"]) == 1


def test_completed_status_is_revalidated_when_client_progress_regresses(tmp_path: Path):
    client = FakeClient()
    topic = {
        "id": "regress",
        "title": "Show [1 из 1]",
        "hash": "HASH",
        "save_path": str(tmp_path),
    }
    history = {
        "schema_version": 1,
        "topics": {"regress": {"baseline_at": "2026-01-01T00:00:00+00:00", "items": {}}},
    }
    folder = tmp_path / "Show"
    folder.mkdir()
    (folder / "S01E01.mkv").write_bytes(b"0123456789")
    client.progress = 1.0
    reconcile_topic(topic, client, history, now="2026-01-01T01:00:00+00:00")

    client.progress = 0.5
    result = reconcile_topic(topic, client, history, now="2026-01-01T02:00:00+00:00")

    item = next(iter(history["topics"]["regress"]["items"].values()))
    assert item["status"] == "downloading"
    assert result["summary"]["completed"] == 0
    assert "last_completed" not in history["topics"]["regress"]


def test_preallocated_full_size_file_is_not_complete_before_exact_client_completion(
    tmp_path: Path,
):
    client = FakeClient()
    client.progress = 0.9995
    folder = tmp_path / "Show"
    folder.mkdir()
    (folder / "S01E01.mkv").write_bytes(b"0123456789")
    topic = {
        "id": "preallocated",
        "title": "Show [1 из 1]",
        "hash": "HASH",
        "save_path": str(tmp_path),
    }

    result = reconcile_topic(
        topic,
        client,
        {"schema_version": 1, "topics": {}},
        now="2026-01-01T00:00:00+00:00",
    )

    assert result["summary"]["completed"] == 0


def test_unknown_total_series_does_not_count_fonts_as_completed_episodes():
    items = {
        "episode": {
            "episode_key": "episode:s01e01",
            "episode_keys": ["episode:s01e01"],
            "status": "completed",
        },
        "font": {"identity": "file:font.ttf", "status": "completed"},
    }
    assert summarize_completion(items, None)["completed"] == 1


def test_superseded_completion_is_not_exposed_as_last_completed(tmp_path: Path):
    class Client:
        client_id = "fake-main"
        client_kind = "fake"
        capabilities: ClassVar[dict[str, bool]] = {"inspect": True, "list_files": True}

        def inspect_torrent(self, infohash):
            return {
                "hash": infohash,
                "files": [{"name": "Show/S01E02.mkv", "size": 1, "progress": 0.0}],
            }

    history = {
        "schema_version": 1,
        "topics": {
            "superseded": {
                "baseline_at": "2026-01-01T00:00:00+00:00",
                "items": {
                    "old": {
                        "identity": "old",
                        "relative_path": "Show/S01E01.mkv",
                        "source_hash": "OLD",
                        "episode_key": "episode:s01e01",
                        "episode_keys": ["episode:s01e01"],
                        "label": "S01E01",
                        "status": "completed",
                        "completed_observed_at": "2026-01-01T00:30:00+00:00",
                    }
                },
            }
        },
    }
    topic = {
        "id": "superseded",
        "title": "Show [2 из 2]",
        "hash": "NEW",
        "save_path": str(tmp_path),
    }

    reconcile_topic(topic, Client(), history, now="2026-01-01T01:00:00+00:00")

    assert "last_completed" not in history["topics"]["superseded"]


def test_multi_episode_file_uses_range_label(tmp_path: Path):
    class Client:
        client_id = "fake-main"
        client_kind = "fake"
        capabilities: ClassVar[dict[str, bool]] = {"inspect": True, "list_files": True}

        def inspect_torrent(self, infohash):
            return {
                "hash": infohash,
                "files": [{"name": "Show/S01E01-E03.mkv", "size": 1, "progress": 0.5}],
            }

    topic = {
        "id": "range-label",
        "title": "Show [3 из 3]",
        "hash": "HASH",
        "save_path": str(tmp_path),
    }
    history = {"schema_version": 1, "topics": {}}

    reconcile_topic(topic, Client(), history, now="2026-01-01T00:00:00+00:00")

    item = next(iter(history["topics"]["range-label"]["items"].values()))
    assert item["label"] == "S01E01–03"
    assert item["episode_keys"] == [
        "episode:s01e01",
        "episode:s01e02",
        "episode:s01e03",
    ]


def test_move_in_progress_is_not_a_save_path_error_and_clears_when_done(tmp_path: Path):
    old, new = tmp_path / "old", tmp_path / "new"

    class Client:
        client_id = "fake-main"
        client_kind = "fake"
        capabilities: ClassVar[dict[str, bool]] = {"inspect": True, "list_files": True}
        path = old
        state = "moving"

        def inspect_torrent(self, infohash):
            return {"hash": infohash, "save_path": str(self.path), "state": self.state, "files": []}

    client = Client()
    topic = {
        "id": "moving",
        "title": "Show",
        "hash": "H",
        "save_path": str(new),
        "move_pending": {"from": str(old), "to": str(new), "since": "2026-01-01T00:00:00+00:00"},
    }
    history = {"schema_version": 1, "topics": {}}

    reconcile_topic(topic, client, history, now="2026-01-01T00:00:00+00:00")
    assert "move_pending" in topic

    client.path, client.state = new, "uploading"
    reconcile_topic(topic, client, history, now="2026-01-01T00:05:00+00:00")
    assert "move_pending" not in topic

    client.path = tmp_path / "elsewhere"
    with raises_code("progress.path_differs", RuntimeError):
        reconcile_topic(topic, client, history, now="2026-01-01T00:10:00+00:00")


def test_history_index_skips_moved_items_and_finds_readded_ones():
    from tow.progress import _HistoryIndex

    items = {"a": {"relative_path": "Show/E01.mkv"}, "b": {"relative_path": "show/e01.MKV"}}
    index = _HistoryIndex(items)
    assert [identity for identity, _ in index.at_path("SHOW/E01.mkv")] == ["a", "b"]

    items["c"] = items.pop("a")  # migrated to another identity
    items["b"]["relative_path"] = "Show/E02.mkv"  # renamed
    index.add("c")
    index.add("b")

    assert [identity for identity, _ in index.at_path("show/e01.mkv")] == ["c"]
    assert [identity for identity, _ in index.at_path("show/e02.mkv")] == ["b"]


def test_a_file_confirmed_today_is_not_looked_at_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    client = FakeClient()
    client.progress = 1.0
    topic = {"id": "quiet", "title": "Show [1 из 1]", "hash": "ABC", "save_path": str(tmp_path)}
    history: dict = {"topics": {"quiet": {"baseline_at": "2026-09-12T20:00:00+03:00", "items": {}}}}
    looks = []
    monkeypatch.setattr("tow.progress.filesystem_confirmation", lambda *_a, **_k: looks.append(1) or True)
    reconcile_topic(topic, client, history, "2026-09-12T21:00:00+03:00")
    reconcile_topic(topic, client, history, "2026-09-12T21:30:00+03:00")
    reconcile_topic(topic, client, history, "2026-09-12T22:00:00+03:00")
    assert len(looks) == 1  # sleeping drives are not woken every 30 minutes
    item = next(iter(history["topics"]["quiet"]["items"].values()))
    item["confirmed_ts"] -= 25 * 3600  # a day later the disk is looked at again
    reconcile_topic(topic, client, history, "2026-09-13T22:30:00+03:00")
    assert len(looks) == 2
