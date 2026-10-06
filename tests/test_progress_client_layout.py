from __future__ import annotations

from copy import deepcopy
from typing import ClassVar

import pytest

from tow.progress import _file_rule_plan, reconcile_topic
from tow.selection import SelectionError, normalize_policy

HASH = "a" * 40
CANONICAL = "Season 1/ShowA.S01E01.mkv"


class Client:
    client_id = "fake-main"
    client_kind = "fake"
    capabilities: ClassVar = {"inspect": True, "list_files": True}

    def __init__(self, info):
        self.info = info

    def inspect_torrent(self, infohash):
        assert infohash == HASH
        return deepcopy(self.info)


def topic_for(mode, save_path, canonical=CANONICAL):
    return {
        "id": "test",
        "title": "Show A",
        "hash": HASH,
        "save_path": save_path,
        "selection": normalize_policy(mode, "Season 1/*.mkv", files=[{"path": canonical, "size": 1}], source_hash=HASH),
        "selection_hash": HASH,
        "selection_verified": True,
        "selected_episode_keys": ["episode:s01e01"],
    }


@pytest.mark.parametrize("mode", ["files", "exact"])
@pytest.mark.parametrize("prefix", ["", "ShowRoot/"])
def test_native_root_layout_keeps_policy_priorities_and_history_paths(tmp_path, mode, prefix):
    native = prefix + CANONICAL
    path = tmp_path / native
    path.parent.mkdir(parents=True)
    path.write_bytes(b"x")
    info = {
        "save_path": str(tmp_path),
        "content_path": str(tmp_path / "ShowRoot"),
        "files": [
            {"name": prefix + "Season 1/ShowA.S01E02.txt", "size": 3, "progress": 1.0, "priority": 1},
            {"name": native, "size": 1, "progress": 1.0, "priority": 1},
        ],
    }
    topic = topic_for(mode, str(tmp_path))
    saved_topic, saved_info = deepcopy(topic), deepcopy(info)
    history = {"topics": {}}
    first = reconcile_topic(topic, Client(info), history, "2026-09-10T20:00:00+00:00")
    second = reconcile_topic(topic, Client(info), history, "2026-09-10T21:00:00+00:00")
    for result in (first, second):
        assert result["summary"]["completed"] == result["summary"]["expected"] == 1
        assert result["summary"]["is_complete"] is True
        assert result["events"] == []
    assert {item["relative_path"] for item in history["topics"]["test"]["items"].values()} == {native}
    assert topic == saved_topic
    assert info == saved_info


@pytest.mark.parametrize(
    ("save_path", "content_path", "native", "canonical"),
    [
        (r"D:\TV", r"d:\tv\ShowRoot", "ShowRoot/" + CANONICAL, CANONICAL),
        (r"\\nas\TV", r"\\nas\TV\ShowRoot", "ShowRoot/" + CANONICAL, CANONICAL),
        ("/srv/media", "/srv/media/ShowRoot", "SHOWROOT/season 1/showa.s01e01.mkv", CANONICAL),
        ("/srv/media", "/srv/media/ShowRoot", "ShowRoot/Season 1/ShowA_.S01E01.mkv", "Season 1/ShowA:.S01E01.mkv"),
        ("/srv/media", "/srv/media/ShowRoot", "Season 1/ShowA_.S01E01.mkv", "Season 1/ShowA:.S01E01.mkv"),
    ],
)
def test_exact_identity_uses_shared_portable_mapping(save_path, content_path, native, canonical):
    plan = _file_rule_plan(
        topic_for("exact", save_path, canonical),
        [(None, native, 1, True)],
        1,
        content_path=content_path,
        save_path=save_path,
    )
    assert plan.selected_indices == (0,)
    assert plan.selected_files == (canonical,)


@pytest.mark.parametrize("mode", ["files", "exact"])
@pytest.mark.parametrize(
    "content_path", ["", "/elsewhere/ShowRoot", "/srv/media/nested/ShowRoot", "/srv/media/../ShowRoot"]
)
def test_unknown_or_foreign_root_never_becomes_a_suffix_guess(mode, content_path):
    with pytest.raises(SelectionError):
        _file_rule_plan(
            topic_for(mode, "/srv/media"),
            [(None, "ShowRoot/" + CANONICAL, 1, True)],
            1,
            content_path=content_path,
            save_path="/srv/media",
        )


@pytest.mark.parametrize("size", [None, 2])
def test_exact_unknown_or_changed_size_is_still_refused(size):
    with pytest.raises(SelectionError) as error:
        _file_rule_plan(
            topic_for("exact", "/srv/media"),
            [(None, "ShowRoot/" + CANONICAL, size, True)],
            1,
            content_path="/srv/media/ShowRoot",
            save_path="/srv/media",
        )
    assert error.value.code == "selection.exact_changed"


def test_exact_ambiguous_flat_and_root_aliases_are_refused():
    rows = [(None, CANONICAL, 1, True), (None, "ShowRoot/" + CANONICAL, 1, True)]
    with pytest.raises(SelectionError) as error:
        _file_rule_plan(
            topic_for("exact", "/srv/media"), rows, 1, content_path="/srv/media/ShowRoot", save_path="/srv/media"
        )
    assert error.value.code == "selection.exact_changed"


@pytest.mark.parametrize("mode", ["files", "exact"])
def test_root_mapping_does_not_enable_a_disabled_native_file(tmp_path, mode):
    info = {
        "save_path": str(tmp_path),
        "content_path": str(tmp_path / "ShowRoot"),
        "files": [{"name": "ShowRoot/" + CANONICAL, "size": 1, "progress": 1.0, "priority": 0}],
    }
    result = reconcile_topic(topic_for(mode, str(tmp_path)), Client(info), {"topics": {}}, "2026-09-10T20:00:00+00:00")
    assert result["summary"]["expected"] == 1
    assert result["summary"]["completed"] == 0
    assert result["summary"]["is_complete"] is False
    assert result["events"] == []


@pytest.mark.parametrize("prefix", ["", "ShowRoot/"])
def test_sanitized_literal_path_still_uses_native_disk_evidence(tmp_path, prefix):
    native = prefix + "Season 1/ShowA_.S01E01.mkv"
    path = tmp_path / native
    path.parent.mkdir(parents=True)
    path.write_bytes(b"x")
    info = {
        "save_path": str(tmp_path),
        "content_path": str(tmp_path / "ShowRoot"),
        "files": [{"name": native, "size": 1, "progress": 1.0, "priority": 1}],
    }
    topic = topic_for("exact", str(tmp_path), "Season 1/ShowA:.S01E01.mkv")
    history = {"topics": {}}
    result = reconcile_topic(topic, Client(info), history, "2026-09-10T20:00:00+00:00")
    assert result["summary"]["completed"] == 1
    assert result["summary"]["is_complete"] is True
    item = next(iter(history["topics"]["test"]["items"].values()))
    assert item["relative_path"] == native
    assert item["status"] == "completed"
    assert type(item["confirmed_ts"]) is int


@pytest.mark.parametrize("mode", ["files", "exact"])
def test_single_file_content_path_is_not_mistaken_for_a_directory(tmp_path, mode):
    native = "ShowA.S01E01.mkv"
    (tmp_path / native).write_bytes(b"x")
    topic = topic_for(mode, str(tmp_path), native)
    if mode == "files":
        topic["selection"] = normalize_policy("files", "*.mkv")
    info = {
        "save_path": str(tmp_path),
        "content_path": str(tmp_path / native),
        "files": [{"name": native, "size": 1, "progress": 1.0, "priority": 1}],
    }
    result = reconcile_topic(topic, Client(info), {"topics": {}}, "2026-09-10T20:00:00+00:00")
    assert result["summary"]["completed"] == 1
    assert result["summary"]["is_complete"] is True


@pytest.mark.parametrize("mode", ["files", "exact"])
def test_reordered_native_rows_preserve_the_correct_wanted_index(mode):
    topic = topic_for(mode, "/srv/media")
    prepared = [
        (None, "ShowRoot/Other/ShowA.S01E01.mkv", 1, True),
        (None, "ShowRoot/" + CANONICAL, 1, True),
        (None, "ShowRoot/Season 1/ShowA.S01E01.txt", 1, True),
    ]
    plan = _file_rule_plan(topic, prepared, 1, content_path="/srv/media/ShowRoot", save_path="/srv/media")
    assert plan.selected_indices == (1,)


def test_sanitized_duplicate_native_identities_are_not_resolved_by_position():
    topic = topic_for("exact", "/srv/media", "Season 1/ShowA:.S01E01.mkv")
    prepared = [
        (None, "ShowRoot/Season 1/ShowA_.S01E01.mkv", 1, True),
        (None, "ShowRoot/Season 1/SHOWA_.S01E01.mkv", 1, True),
    ]
    with pytest.raises(SelectionError) as error:
        _file_rule_plan(topic, prepared, 1, content_path="/srv/media/ShowRoot", save_path="/srv/media")
    assert error.value.code == "selection.exact_changed"
