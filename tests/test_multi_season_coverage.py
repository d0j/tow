from __future__ import annotations

from typing import ClassVar

import pytest

from tow.episodes import item_identity, parse_episode_coverage, resolve_episode_coverages
from tow.progress import reconcile_topic
from tow.selection import normalize_policy, resolve_selection
from tow.torrent import TorrentFile


@pytest.mark.parametrize(
    ("name", "keys"),
    [
        (
            "ShowA.S01E05-E08S02E01-E04.mkv",
            [*(f"episode:s01e{x:02d}" for x in range(5, 9)), *(f"episode:s02e{x:02d}" for x in range(1, 5))],
        ),
        ("ShowA.S01E08&S02E01.mkv", ["episode:s01e08", "episode:s02e01"]),
        ("ShowA.S01E08.S02E01.mkv", ["episode:s01e08", "episode:s02e01"]),
        ("ShowAS01x08.S02x01.mkv", ["episode:s01e08", "episode:s02e01"]),
        ("ShowAS01x08S02x01.mkv", ["episode:s01e08", "episode:s02e01"]),
        ("ShowA.S01E08&S02E01&S01E08.mkv", ["episode:s01e08", "episode:s02e01"]),
        ("ShowA.S01E08&S02E01&S2E03.mkv", ["episode:s01e08", "episode:s02e01", "episode:s02e03"]),
        ("ShowA.S01E08.S02E01-S2E03.mkv", ["episode:s01e08", "episode:s02e01", "episode:s02e02", "episode:s02e03"]),
    ],
)
def test_explicit_members_of_different_seasons_are_retained(name, keys):
    assert [label.key for label in parse_episode_coverage(name)] == keys


@pytest.mark.parametrize(
    "name",
    [
        "ShowA.S01E08-S02E01.mkv",
        "ShowA.S01E08&S00E01.mkv",
        "ShowA.S01E08&S02E00.mkv",
        "ShowA.S01E08&S02E01foo.mkv",
        "ShowA.S01E08&S02E01.5.mkv",
        "ShowA.S01E08&S02E03-E01.mkv",
        "ShowA.S01E08&S02E01-E2000.mkv",
        "ShowA.S01E08&S02E12345.mkv",
        "ShowA.S01E08&S123E01.mkv",
        "ShowA.S01E08&S123E01&S03E01.mkv",
        "ShowA.S01E08&S02E12345&S03E01.mkv",
    ],
)
def test_mixed_coverage_refuses_ambiguous_ranges_and_invalid_or_special_members(name):
    assert parse_episode_coverage(name) == ()


def test_descriptive_words_do_not_turn_referenced_markers_into_a_multi_season_file():
    assert [x.key for x in parse_episode_coverage("ShowA.S01E08.Descriptive.Title.S02E01.mkv")] == ["episode:s01e08"]


def test_only_basename_contributes_episode_coverage():
    assert [x.key for x in parse_episode_coverage("ShowA.S01E08/ShowA.S02E01.mkv")] == ["episode:s02e01"]


def test_explicit_mixed_season_ranges_do_not_become_absolute_number_aliases():
    name = "ShowA.S01E01-E20&S02E01-E20.mkv"
    coverage = resolve_episode_coverages([name])[0]
    assert len(coverage) == 40
    assert {label.season for label in coverage} == {1, 2}
    plan = resolve_selection([TorrentFile(0, name, 1)], normalize_policy("episodes", "S02E20"))
    assert plan.selected_indices == (0,)
    assert plan.selected_episode_keys == ("episode:s02e20",)


def test_overlapping_mixed_season_files_keep_their_explicit_members():
    names = [f"ShowA.S01E{start:02d}-E{start + 2:02d}&S02E{start:02d}-E{start + 2:02d}.mkv" for start in (1, 2, 3)]
    assert [len(coverage) for coverage in resolve_episode_coverages(names)] == [6, 6, 6]


@pytest.mark.parametrize("mode", ["all", "episodes", "files", "exact"])
def test_each_selection_mode_can_keep_the_second_season(mode):
    name = "ShowA.S01E08&S02E01.mkv"
    files = (
        TorrentFile(0, name, 1),
        TorrentFile(1, "ShowA.S02E01.en.srt", 1),
        TorrentFile(2, "ShowA.S02E02.mkv", 1),
        TorrentFile(3, "cover.jpg", 1),
    )
    if mode == "exact":
        policy = normalize_policy(mode, files=[{"path": name, "size": 1}], source_hash="0" * 40)
    else:
        policy = normalize_policy(mode, "S02E01" if mode == "episodes" else "*S01E08*.mkv" if mode == "files" else "")
    plan = resolve_selection(files, policy)
    assert "episode:s02e01" in plan.selected_episode_keys
    assert plan.selected_indices == ((0, 1) if mode == "episodes" else (0, 1, 2, 3) if mode == "all" else (0,))


class CompletedClient:
    client_id = "fake-main"
    client_kind = "fake"
    capabilities: ClassVar = {"inspect": True, "list_files": True, "completion_time": False}
    progress = 1.0

    def inspect_torrent(self, infohash):
        return {
            "hash": infohash,
            "progress": self.progress,
            "files": [{"name": "ShowA.S01E08&S02E01.mkv", "size": 1, "progress": self.progress}],
        }


@pytest.mark.parametrize("old_kind", ["file", "episode"])
@pytest.mark.parametrize("only_second_season", [False, True])
def test_completed_legacy_history_gains_second_season_silently(tmp_path, old_kind, only_second_season):
    path = "ShowA.S01E08&S02E01.mkv"
    (tmp_path / path).write_bytes(b"x")
    identity = item_identity(path, None, 1)
    at = "2026-09-10T20:00:00+00:00"
    item = {
        "identity": identity,
        "kind": old_kind,
        "label": "S01E08" if old_kind == "episode" else path,
        "relative_path": path,
        "source_hash": "ABC",
        "size": 1,
        "status": "completed",
        "first_seen_at": "2026-09-10T19:00:00+00:00",
        "completed_observed_at": at,
        "episode_keys": ["episode:s01e08"] if old_kind == "episode" else [],
    }
    record = {
        "baseline_at": "2026-09-10T19:00:00+00:00",
        "items": {identity: item},
        "last_event": {
            "kind": old_kind + "_completed",
            "label": item["label"],
            "item": identity,
            "relative_path": path,
            "at": at,
        },
    }
    history = {"schema_version": 1, "topics": {"test": record}}
    topic = {"id": "test", "title": "Show A", "hash": "ABC", "save_path": str(tmp_path)}
    if only_second_season:
        topic.update(
            {
                "selection": normalize_policy("episodes", "S02E01"),
                "selected_episode_keys": ["episode:s02e01"],
                "selection_verified": True,
            }
        )
    result = reconcile_topic(topic, CompletedClient(), history, now="2026-09-11T20:00:00+00:00")
    assert result["events"] == []
    assert result["summary"]["completed"] == (1 if only_second_season else 2)
    assert record["last_event"]["kind"] == "episode_completed"
    assert record["last_event"]["label"] == "S01E08, S02E01"
    assert record["last_event"]["at"] == at
    assert list(record["items"]) == [identity]
    assert item["episode_keys"] == ["episode:s01e08", "episode:s02e01"]
    assert item["relative_path"] == path
    assert item["completed_observed_at"] == at
    assert (tmp_path / path).read_bytes() == b"x"
    assert reconcile_topic(topic, CompletedClient(), history, now="2026-09-11T20:01:00+00:00")["events"] == []


def test_overlong_numeric_season_is_refused_without_converting_untrusted_integer():
    assert parse_episode_coverage("ShowA.S01E08&S" + "9" * 5000 + "E01.mkv") == ()


def test_new_completion_reports_both_seasons_once(tmp_path):
    path = "ShowA.S01E08&S02E01.mkv"
    (tmp_path / path).write_bytes(b"x")
    topic = {"id": "test", "title": "Show A", "hash": "ABC", "save_path": str(tmp_path)}
    history = {"schema_version": 1, "topics": {}}
    client = CompletedClient()
    client.progress = 0.5
    assert reconcile_topic(topic, client, history, now="2026-09-10T20:00:00+00:00")["events"] == []
    client.progress = 1.0
    result = reconcile_topic(topic, client, history, now="2026-09-10T20:01:00+00:00")
    assert result["events"] == ["episode_completed"]
    assert result["summary"]["completed"] == 2
    assert history["topics"]["test"]["last_event"]["label"] == "S01E08, S02E01"
    assert reconcile_topic(topic, client, history, now="2026-09-10T20:02:00+00:00")["events"] == []
