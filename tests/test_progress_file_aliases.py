from __future__ import annotations

from copy import deepcopy

import pytest

from tow import bundle
from tow.check_steps import merge_check_results, store_revision
from tow.progress import _file_rule_evidence, _selection_fingerprint
from tow.selection import SelectionError, normalize_file_aliases, normalize_policy, resolve_selection
from tow.torrent import MAX_FILES, TorrentFile, windows_path_key

HASH = "a" * 40


def topic_for(pattern, files):
    return {
        "id": "test",
        "hash": HASH,
        "selection_hash": HASH,
        "selection_verified": True,
        "selection": normalize_policy("files", pattern),
        "file_aliases": {"hash": HASH, "files": files},
    }


def plan_for(topic, rows):
    return _file_rule_evidence(topic, rows, 1, content_path="/srv/media/ShowRoot", save_path="/srv/media")[0]


@pytest.mark.parametrize("prefix", ["", "ShowRoot/"])
@pytest.mark.parametrize(
    ("canonical", "pattern"),
    [
        ("Season|1/ShowA.S01E01.mkv", "Season|1/*.mkv"),
        ("Season1./ShowA.S01E01.mkv", "Season1./*.mkv"),
        ("CON/ShowA.S01E01.mkv", "CON/*.mkv"),
        ("Show?.S01E01.mkv", "Show[?].*.mkv"),
    ],
)
def test_mask_replays_original_names_without_rewriting_glob_semantics(canonical, pattern, prefix):
    topic = topic_for(pattern, [{"path": canonical, "size": 1}])
    before = deepcopy(topic)
    plan = plan_for(topic, [(None, prefix + windows_path_key(canonical), 1, True)])
    assert plan.selected_indices == (0,)
    assert plan.selected_files == (canonical,)
    assert plan.selected_episode_keys == ("episode:s01e01",)
    assert topic == before


def test_an_unwanted_alias_cannot_start_matching_a_different_literal_mask():
    topic = topic_for("Season_1/*.mkv; Other/*.mkv", [{"path": "Season|1/ShowA.S01E01.mkv", "size": 1}])
    rows = [(None, "ShowRoot/Season_1/ShowA.S01E01.mkv", 1, True), (None, "ShowRoot/Other/ShowA.S01E02.mkv", 1, True)]
    plan = plan_for(topic, rows)
    assert plan.selected_indices == (1,)
    assert plan.selected_episode_keys == ("episode:s01e02",)


@pytest.mark.parametrize("size", [None, 2])
def test_alias_identity_requires_its_original_size(size):
    topic = topic_for("*.mkv", [{"path": "Show?.S01E01.mkv", "size": 1}])
    with pytest.raises(SelectionError) as error:
        plan_for(topic, [(None, "ShowRoot/Show_.S01E01.mkv", size, True)])
    assert error.value.code == "selection.file_map_changed"


def test_ambiguous_alias_is_refused_instead_of_matching_by_position():
    topic = topic_for("*.mkv", [{"path": "Show?.S01E01.mkv", "size": 1}])
    with pytest.raises(SelectionError) as error:
        plan_for(topic, [(None, "Show_.S01E01.mkv", 1, True), (None, "ShowRoot/Show_.S01E01.mkv", 1, True)])
    assert error.value.code == "selection.file_map_changed"


@pytest.mark.parametrize(
    "aliases",
    [
        None,
        [],
        {"hash": "b" * 40, "files": []},
        {"hash": HASH, "files": "bad"},
        {"hash": HASH, "files": [{"path": "../x.mkv", "size": 1}]},
        {"hash": HASH, "files": [{"path": "Show?.mkv", "size": True}]},
    ],
)
def test_invalid_or_other_revision_aliases_cannot_be_used_for_current_rule(aliases):
    topic = topic_for("*.mkv", [])
    topic["file_aliases"] = aliases
    with pytest.raises(SelectionError) as error:
        plan_for(topic, [(None, "ShowRoot/ShowA.S01E01.mkv", 1, True)])
    assert error.value.code == "selection.file_map_changed"


@pytest.mark.parametrize(
    "flags", [{"selection_dirty": True}, {"selection_verified": False}, {"selection_hash": "b" * 40}]
)
def test_unconfirmed_rule_does_not_trust_even_well_formed_aliases(flags):
    topic = topic_for("*.mkv", [{"path": "Show?.S01E01.mkv", "size": 1}])
    topic.update(flags)
    assert plan_for(topic, [(None, "ShowRoot/Show_.S01E01.mkv", 1, True)]) is None


def test_alias_snapshot_covers_unwanted_files_and_is_not_the_200_path_ui_preview():
    files = tuple(TorrentFile(index, f"Show?.S01E{index + 1:03}.mkv", 1) for index in range(230))
    policy = normalize_policy("files", "*.mkv")
    plan = resolve_selection(files, policy, preferred_season=1)
    topic = {"selection": policy}
    store_revision(
        topic, h=HASH, old="", keep_previous=False, selection_verified=True, plan=plan, once=False, files=files
    )
    assert len(topic["selected_files"]) == 200
    assert topic["selected_files_truncated"] is True
    assert len(topic["file_aliases"]["files"]) == 230
    actual = plan_for(topic, [(None, "ShowRoot/" + windows_path_key(row.path), 1, True) for row in files])
    assert actual.selected_indices == tuple(range(230))


def test_revision_records_only_altered_names_but_includes_unwanted_aliases():
    files = (
        TorrentFile(0, "Show?.S01E01.mkv", 1),
        TorrentFile(1, "ShowA.S01E02.mkv", 2),
        TorrentFile(2, ".pad/pad?.bin", 3, is_pad=True),
    )
    policy = normalize_policy("files", "ShowA*.mkv")
    topic = {"selection": policy}
    store_revision(
        topic,
        h=HASH,
        old="",
        keep_previous=False,
        selection_verified=True,
        plan=resolve_selection(files, policy),
        once=False,
        files=files,
    )
    assert topic["file_aliases"] == {"hash": HASH, "files": [{"path": "Show?.S01E01.mkv", "size": 1}]}
    ordinary = (TorrentFile(0, "ShowA.S01E02.mkv", 2),)
    store_revision(
        topic,
        h="b" * 40,
        old=HASH,
        keep_previous=True,
        selection_verified=True,
        plan=resolve_selection(ordinary, policy),
        once=False,
        files=ordinary,
    )
    assert topic["file_aliases"] == {"hash": "b" * 40, "files": []}
    store_revision(
        topic,
        h="c" * 40,
        old="b" * 40,
        keep_previous=True,
        selection_verified=True,
        plan=resolve_selection(ordinary, normalize_policy("all")),
        once=False,
        files=ordinary,
    )
    assert "file_aliases" not in topic


def test_merge_clears_old_alias_snapshot_and_keeps_a_concurrent_owner_rule():
    source = {"id": "test", "hash": HASH, "selection": {"mode": "files", "value": "*.mkv"}}
    current = {**source, "selection": {"mode": "files", "value": "*.mp4"}, "file_aliases": {"hash": HASH, "files": []}}
    disk = {"topics": [current]}
    merge_check_results(disk, {"topics": [source]}, edited={"test": ["selection"]})
    assert current["selection"]["value"] == "*.mp4"
    assert current["selection_dirty"] is True
    assert "file_aliases" not in current


@pytest.mark.parametrize(
    "value", [None, [], {"hash": HASH, "files": "bad"}, {"hash": HASH, "files": [{"path": "../x.mkv", "size": 1}]}]
)
def test_import_refuses_malformed_alias_metadata(value):
    with pytest.raises(bundle.ExportImportError):
        bundle._validate_state_topic(0, {"file_aliases": value})


@pytest.mark.parametrize("mode", ["all", "files"])
def test_alias_context_is_in_the_expected_target_fingerprint(mode):
    topic = topic_for("*.mkv", [{"path": "Show?.S01E01.mkv", "size": 1}])
    topic["selection"] = normalize_policy(mode, "*.mkv")
    before = _selection_fingerprint(topic)
    topic["file_aliases"]["files"][0]["size"] = 2
    assert _selection_fingerprint(topic) != before


@pytest.mark.parametrize("mode", ["all", "files"])
def test_alias_order_and_hash_case_do_not_change_the_same_metadata_context(mode):
    files = [{"path": "Show?.S01E01.mkv", "size": 1}, {"path": "Show?.S01E02.mkv", "size": 1}]
    topic = topic_for("*.mkv", files)
    topic["selection"] = normalize_policy(mode, "*.mkv")
    before = _selection_fingerprint(topic)
    topic["file_aliases"] = {"hash": HASH.upper(), "files": list(reversed(files))}
    assert _selection_fingerprint(topic) == before


def test_a_complete_verified_legacy_path_list_can_supply_original_names_read_only():
    topic = topic_for("Season|1/*.mkv", [])
    topic.pop("file_aliases")
    topic.update(selected_files=["Season|1/ShowA.S01E01.mkv"], selected_file_count=1, selected_files_truncated=False)
    before = deepcopy(topic)
    plan = plan_for(
        topic,
        [(None, "ShowRoot/season_1/showa.s01e01.mkv", 1, True), (None, "ShowRoot/Other/ShowA.S01E99.mkv", 1, True)],
    )
    assert plan.selected_indices == (0,)
    assert plan.selected_files == ("Season|1/ShowA.S01E01.mkv",)
    assert topic == before


def test_a_truncated_legacy_path_list_is_not_promoted_to_a_complete_target():
    topic = topic_for("*.mkv", [])
    topic.pop("file_aliases")
    topic.update(selected_files=["Show?.S01E01.mkv"], selected_file_count=201, selected_files_truncated=True)
    with pytest.raises(SelectionError) as error:
        plan_for(topic, [(None, "ShowRoot/Show_.S01E01.mkv", 1, True)])
    assert error.value.code == "selection.file_map_changed"


def test_legacy_selection_cannot_gain_an_unwanted_file_from_a_sanitized_name():
    topic = topic_for("Season_1/*.mkv; Other/*.mkv", [])
    topic.pop("file_aliases")
    topic.update(selected_files=["Other/ShowA.S01E02.mkv"], selected_file_count=1, selected_files_truncated=False)
    plan = plan_for(
        topic,
        [(None, "ShowRoot/Season_1/ShowA.S01E01.mkv", 1, True), (None, "ShowRoot/Other/ShowA.S01E02.mkv", 1, True)],
    )
    assert plan.selected_indices == (1,)


def test_truncated_legacy_names_are_incomplete_even_when_the_visible_preview_has_no_alias():
    topic = topic_for("*.mkv", [])
    topic.pop("file_aliases")
    topic.update(selected_files=["ShowA.S01E01.mkv"], selected_file_count=201, selected_files_truncated=True)
    with pytest.raises(SelectionError) as error:
        plan_for(topic, [(None, "ShowRoot/ShowA.S01E01.mkv", 1, True)])
    assert error.value.code == "selection.file_map_changed"


def test_new_ordinary_mask_revision_records_an_empty_authoritative_alias_snapshot():
    files = tuple(TorrentFile(index, f"ShowA.S01E{index + 1:03}.mkv", 1) for index in range(230))
    policy = normalize_policy("files", "*.mkv")
    topic = {"selection": policy}
    store_revision(
        topic,
        h=HASH,
        old="",
        keep_previous=False,
        selection_verified=True,
        plan=resolve_selection(files, policy, preferred_season=1),
        once=False,
        files=files,
    )
    assert topic["file_aliases"] == {"hash": HASH, "files": []}
    actual = plan_for(topic, [(None, "ShowRoot/" + row.path, 1, True) for row in files])
    assert actual.selected_indices == tuple(range(230))


def test_alias_snapshot_accepts_the_existing_torrent_file_limit_and_reordered_client_indices():
    aliases = [{"path": f"file{index:05}?.bin", "size": index} for index in range(MAX_FILES)]
    topic = topic_for("*.bin", aliases)
    rows = [(None, "ShowRoot/" + windows_path_key(item["path"]), item["size"], True) for item in reversed(aliases)]
    plan = plan_for(topic, rows)
    assert len(plan.selected_files) == MAX_FILES
    assert plan.selected_indices == tuple(range(MAX_FILES))
    assert plan.selected_files[0] == aliases[-1]["path"]
    with pytest.raises(SelectionError) as error:
        normalize_file_aliases({"hash": HASH, "files": [*aliases, {"path": "overflow?.bin", "size": 1}]})
    assert error.value.code == "selection.file_map_changed"


@pytest.mark.parametrize(
    "files",
    [
        [{"path": "Show?.mkv", "size": 1}, {"path": "Show_.mkv", "size": 1}],
        [{"path": "Show?.mkv", "size": -1}],
        [{"path": "Show?.mkv", "size": 2**63}],
    ],
)
def test_portable_collisions_and_out_of_range_sizes_are_refused(files):
    with pytest.raises(SelectionError) as error:
        normalize_file_aliases({"hash": HASH, "files": files})
    assert error.value.code == "selection.file_map_changed"


@pytest.mark.parametrize(
    "flags",
    [{"selected_files": "bad"}, {"selected_file_count": True}, {"selected_file_count": 2}, {"selected_files": []}],
)
def test_legacy_names_cannot_hide_a_damaged_or_incomplete_selection(flags):
    topic = topic_for("*.mkv", [])
    topic.pop("file_aliases")
    topic.update(selected_files=["ShowA.S01E01.mkv"], selected_file_count=1)
    topic.update(flags)
    with pytest.raises(SelectionError) as error:
        plan_for(topic, [(None, "ShowRoot/ShowA.S01E01.mkv", 1, True)])
    assert error.value.code == "selection.file_map_changed"


@pytest.mark.parametrize("mode", ["all", "episodes", "files", "exact"])
def test_original_names_define_episode_events_while_disk_and_history_keep_native_names(tmp_path, mode):
    from test_progress_client_layout import Client

    from tow.progress import reconcile_topic

    original = "ShowA.S01E01|S02E01.mkv"
    native = "ShowRoot/" + windows_path_key(original)
    target = tmp_path / native
    target.parent.mkdir(parents=True)
    target.write_bytes(b"x")
    topic = topic_for("*.mkv", [{"path": original, "size": 1}])
    topic["selection"] = normalize_policy(
        mode, "S01E01" if mode == "episodes" else "*.mkv", files=[{"path": original, "size": 1}], source_hash=HASH
    )
    topic.update(save_path=str(tmp_path), selected_episode_keys=["episode:s01e01"])
    info = {
        "save_path": str(tmp_path),
        "content_path": str(tmp_path / "ShowRoot"),
        "files": [{"name": native, "size": 1, "priority": 1, "progress": 0.5}],
    }
    history = {"topics": {}}
    first = reconcile_topic(topic, Client(info), history, "2026-09-10T20:00:00+00:00")
    assert first["summary"]["expected"] == 1
    assert first["summary"]["is_complete"] is False
    info["files"][0]["progress"] = 1.0
    second = reconcile_topic(topic, Client(info), history, "2026-09-10T21:00:00+00:00")
    assert second["summary"]["completed"] == second["summary"]["expected"] == 1
    assert second["events"] == ["episode_completed"]
    items = history["topics"]["test"]["items"]
    assert {item["relative_path"] for item in items.values()} == {native}
    assert {key for item in items.values() for key in item["episode_keys"]} == {"episode:s01e01"}
    third = reconcile_topic(topic, Client(info), history, "2026-09-10T22:00:00+00:00")
    assert third["events"] == []


@pytest.mark.parametrize(
    "aliases", [None, {"hash": "b" * 40, "files": []}, {"hash": HASH, "files": [{"path": "../escape.mkv", "size": 1}]}]
)
def test_empty_client_metadata_cannot_reuse_a_target_with_invalid_alias_context(tmp_path, aliases):
    from test_progress_client_layout import Client

    from tow.progress import reconcile_topic

    topic = topic_for("*.mkv", [])
    topic.update(save_path=str(tmp_path), file_aliases=aliases)
    with pytest.raises(SelectionError) as error:
        reconcile_topic(topic, Client({"files": []}), {"topics": {}}, "2026-09-10T20:00:00+00:00")
    assert error.value.code == "selection.file_map_changed"


def test_unverified_revision_cannot_publish_original_names():
    topic = {"selection": normalize_policy("files", "*.mkv")}
    store_revision(
        topic,
        h=HASH,
        old="",
        keep_previous=False,
        selection_verified=False,
        plan=None,
        once=False,
        files=(TorrentFile(0, "Show?.mkv", 1),),
    )
    assert "file_aliases" not in topic


@pytest.mark.parametrize("mode", ["all", "episodes", "files"])
def test_refresh_repairs_old_native_episode_labels_without_reannouncing_completion(tmp_path, mode):
    from test_progress_client_layout import Client

    from tow.progress import reconcile_topic

    original = "ShowA.S01E01|S02E01.mkv"
    native = "ShowRoot/" + windows_path_key(original)
    target = tmp_path / native
    target.parent.mkdir(parents=True)
    target.write_bytes(b"x")
    topic = topic_for("*.mkv", [{"path": original, "size": 1}])
    aliases = topic.pop("file_aliases")
    topic["selection"] = normalize_policy(mode, "S01E01" if mode == "episodes" else "*.mkv")
    topic.update(save_path=str(tmp_path), selected_episode_keys=["episode:s01e01", "episode:s02e01"])
    info = {
        "save_path": str(tmp_path),
        "content_path": str(tmp_path / "ShowRoot"),
        "files": [{"name": native, "size": 1, "priority": 1, "progress": 0.5}],
    }
    history = {"topics": {}}
    reconcile_topic(topic, Client(info), history, "2026-09-10T20:00:00+00:00")
    info["files"][0]["progress"] = 1.0
    old = reconcile_topic(topic, Client(info), history, "2026-09-10T21:00:00+00:00")
    assert old["events"] == ["episode_completed"]
    before = deepcopy(history["topics"]["test"]["last_event"])
    topic["file_aliases"] = aliases
    fixed = reconcile_topic(topic, Client(info), history, "2026-09-10T22:00:00+00:00")
    assert fixed["events"] == []
    record = history["topics"]["test"]
    assert record["last_event"]["at"] == before["at"]
    assert "S02" not in record["last_event"]["label"]
    assert {item["relative_path"] for item in record["items"].values()} == {native}
    assert {key for item in record["items"].values() for key in item["episode_keys"]} == {"episode:s01e01"}
