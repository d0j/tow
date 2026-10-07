from __future__ import annotations

from typing import ClassVar

import pytest

from tow.episodes import _normalize_explicit_markers, item_identity, parse_episode_coverage, resolve_episode_coverages
from tow.progress import reconcile_topic
from tow.selection import normalize_policy, resolve_selection
from tow.torrent import TorrentFile


@pytest.mark.parametrize(
    ("name", "numbers"),
    [
        ("ShowAS01x08.mkv", [8]),
        ("ShowAS01E08.mkv", [8]),
        ("ShowA_S01E08.mkv", [8]),
        ("ShowA.S01x08.mkv", [8]),
        ("Show2026S01x08.mkv", [8]),
        ("ShowAs01X08.mkv", [8]),
        ("СериалАS01х08.mkv", [8]),
        ("ShowAS01x08.en.srt", [8]),
        ("S01x08.mkv", [8]),
        ("s1x8.mkv", [8]),
        ("ShowAS01xE08.mkv", [8]),
        ("ShowA.S01xE08-xE10.mkv", [8, 9, 10]),
        ("ShowAS01x08-x10.mkv", [8, 9, 10]),
        ("ShowAS01x08-10.mkv", [8, 9, 10]),
        ("ShowAS01x08x10.mkv", [8, 10]),
        ("ShowAS01E08E10.mkv", [8, 10]),
        ("ShowAS01x08.S01x10.mkv", [8, 10]),
        ("ShowAS01x08&x10.mkv", [8, 10]),
        ("ShowAS01x08v2.mkv", [8]),
        ("ShowAS01x08.1080p.mkv", [8]),
        ("ShowAS01x08_audio.mkv", [8]),
        ("ShowAS01x08_1080p_x265.mkv", [8]),
        ("ShowAS01x08-10bit.mkv", [8]),
        ("ShowAS01x08-10 bit.mkv", [8]),
    ],
)
def test_attached_and_hybrid_explicit_markers_reuse_episode_semantics(name, numbers):
    assert [label.key for label in parse_episode_coverage(name)] == [f"episode:s01e{number:02d}" for number in numbers]


@pytest.mark.parametrize(
    "name",
    [
        "ShowAS00x08.mkv",
        "ShowAS01x00.mkv",
        "ShowAS01x08.5.mkv",
        "ShowAS01x08.5v2.mkv",
        "ShowAS01x12345.mkv",
        "ShowAS123x08.mkv",
        "ShowAS01x08bit.mkv",
        "ShowAS01x08foo.mkv",
        "ShowAS01x08v2foo.mkv",
        "ShowAS01x08x12345.mkv",
        "ShowA2026.1080x720.mkv",
        "ShowA.H264x10.mkv",
        "ShowAS01x08/cover.jpg",
    ],
)
def test_marker_normalization_does_not_invent_partial_or_special_episodes(name):
    assert parse_episode_coverage(name) == ()


@pytest.mark.parametrize("marker", ["S01x", "S01xE", "S01E"])
def test_selection_uses_hybrid_markers_for_the_same_episode_companions_as_canonical_names(marker):
    files = (
        TorrentFile(0, f"ShowA.{marker}08.mkv", 100),
        TorrentFile(1, f"ShowA.{marker}08.en.srt", 2),
        TorrentFile(2, f"ShowA.{marker}09.mkv", 100),
        TorrentFile(3, f"ShowA.{marker}08.jpg", 2),
        TorrentFile(4, "cover.jpg", 2),
    )
    plan = resolve_selection(files, normalize_policy("episodes", "S01E08"))
    assert plan.selected_indices == (0, 1, 3)
    assert plan.selected_episode_keys == ("episode:s01e08",)


@pytest.mark.parametrize(
    "name",
    [
        "Specials/ShowAS01x08.mkv",
        "Season 00/ShowAS01x08.mkv",
        "ShowAS01x08.sample.mkv",
        "ShowAS01x08-preview.mkv",
        "ShowAS01x08 OVA.mkv",
    ],
)
def test_normalized_markers_keep_special_and_preview_exclusions(name):
    assert resolve_episode_coverages([name]) == ((),)


def test_normalization_retains_metadata_and_does_not_rewrite_titles_or_invalid_chains():
    assert _normalize_explicit_markers("xTitleS01x08.1080x720.x265.mkv") == "xTitle S01e08.1080x720.x265.mkv"
    assert _normalize_explicit_markers("xTitleS01x08-10bit.mkv") == "xTitle S01e08-10bit.mkv"
    invalid = "ShowAS01x08" + ".S01x10" * 20_000 + "bit.mkv"
    assert _normalize_explicit_markers(invalid) == invalid


@pytest.mark.parametrize("suffix", ["x12345", "v2foo", "bit", "foo"])
def test_malformed_whole_marker_chains_are_not_partially_normalized(suffix):
    value = "ShowAS01x08" + suffix + ".mkv"
    assert _normalize_explicit_markers(value) == value
    assert parse_episode_coverage(value) == ()


@pytest.mark.parametrize("prefix", ["ShowA", "ShowA_", "ShowA."])
@pytest.mark.parametrize("marker", ["S01_E08", "S01.E08", "S01-E08", "S01 E08"])
def test_explicit_internal_separator_keeps_season(prefix, marker):
    assert [label.key for label in parse_episode_coverage(f"{prefix}{marker}.mkv")] == ["episode:s01e08"]


@pytest.mark.parametrize(
    "name",
    ["ShowAS01x08_x10.mkv", "ShowAS01x08.x10.mkv", "ShowAS01_E08.S01_E10.mkv", "ShowAS01.E08.S01.E10.mkv"],
)
def test_hybrid_and_internal_separators_keep_full_list(name):
    assert [label.key for label in parse_episode_coverage(name)] == ["episode:s01e08", "episode:s01e10"]


@pytest.mark.parametrize(
    "name",
    ["ShowAS01_E08.5.mkv", "ShowAS01.E08.5v2.mkv", "ShowAS00_E08.mkv", "ShowAS01_E08foo.mkv", "ShowAS01.E12345.mkv"],
)
def test_internal_separators_do_not_relax_invalid_or_special_marker_guards(name):
    assert parse_episode_coverage(name) == ()


def test_legacy_hybrid_file_event_is_repaired_without_renaming_or_reannouncing(tmp_path):
    class Client:
        client_id = "fake-main"
        client_kind = "fake"
        capabilities: ClassVar = {"inspect": True, "list_files": True, "completion_time": False}

        def inspect_torrent(self, infohash):
            return {
                "hash": infohash,
                "progress": 1.0,
                "completion_on": None,
                "files": [{"name": "ShowAS01x08.mkv", "size": 1, "progress": 1.0}],
            }

    (tmp_path / "ShowAS01x08.mkv").write_bytes(b"x")
    path = "ShowAS01x08.mkv"
    identity = item_identity(path, None, 1)
    at = "2026-09-10T20:00:00+00:00"
    record = {
        "baseline_at": "2026-09-10T19:00:00+00:00",
        "items": {
            identity: {
                "identity": identity,
                "kind": "file",
                "label": path,
                "relative_path": path,
                "source_hash": "ABC",
                "size": 1,
                "status": "completed",
                "completed_observed_at": at,
            }
        },
        "last_event": {"kind": "file_completed", "label": path, "item": identity, "relative_path": path, "at": at},
    }
    history = {"schema_version": 1, "topics": {"test": record}}
    topic = {"id": "test", "title": "Show A [S01]", "hash": "ABC", "save_path": str(tmp_path)}

    result = reconcile_topic(topic, Client(), history, now="2026-09-11T20:00:00+00:00")

    assert result["events"] == []
    assert record["last_event"]["kind"] == "episode_completed"
    assert record["last_event"]["label"] == "S01E08"
    assert record["last_event"]["at"] == at
    assert len(record["items"]) == 1
    item = next(iter(record["items"].values()))
    assert item["relative_path"] == path
    assert item["completed_observed_at"] == at
    assert (tmp_path / path).read_bytes() == b"x"
    assert reconcile_topic(topic, Client(), history, now="2026-09-11T20:01:00+00:00")["events"] == []


@pytest.mark.parametrize("name", ["Show.S02E03.x264.mkv", "Show S02E03 x265.mkv", "Show.S02E03.x264-GRP.mkv"])
def test_codec_after_an_episode_marker_is_not_an_episode(name):
    assert [label.key for label in parse_episode_coverage(name)] == ["episode:s02e03"]


def test_a_codec_cannot_turn_a_future_range_into_a_missing_episode():
    from tow.selection import SelectionPendingError

    files = (TorrentFile(0, "Show.S02E01.x264.mkv", 1),)
    with pytest.raises(SelectionPendingError):  # "not out yet": waited for quietly
        resolve_selection(files, normalize_policy("episodes", "S02E02-S02E10"))
    pair = (TorrentFile(0, "Show.S02E01.x264.mkv", 1), TorrentFile(1, "Show.S02E01.rus.srt", 1))
    assert resolve_selection(pair, normalize_policy("all")).selected_episode_keys == ("episode:s02e01",)


def test_number_before_season_word_is_the_season():
    from tow.episodes import parse_season_hint

    assert parse_season_hint("Шоу 2 сезон 3 серия [2024, WEB-DL]") == 2
    assert parse_season_hint("Шоу. Сезон 1 серии 1-10") == 1
    assert parse_season_hint("Сериал: сезон 5") == 5
