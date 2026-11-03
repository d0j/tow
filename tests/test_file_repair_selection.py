"""Restoration authority cannot expand or silently replace the saved file selection."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
from test_file_repair import HASH, world

from tow import file_presence, file_repair
from tow.clients.files import recovery_fingerprint
from tow.errors import TowError
from tow.progress.expected import selected_native_indices
from tow.selection import normalize_policy
from tow.store import load_state, save_state


def selection(mode, path="S01E01.mkv"):
    return normalize_policy(
        mode,
        "1" if mode == "episodes" else "*E01.mkv",
        files=[{"path": path, "size": 10}],
        source_hash=HASH,
    )


def bind_missing(topic):
    topic.pop("files_missing", None)
    file_presence.observe(
        topic, {"missing_files": 1, "unverified_files": 0}, client_id="main", at="2026-09-12T20:00:00+00:00"
    )


@pytest.mark.parametrize("mode", ["all", "episodes", "files", "exact"])
@pytest.mark.parametrize("wanted", [(1, 1), (1, 0), (0, 1), (0, 0)])
def test_request_requires_exact_saved_scope_before_persisting_authority(tmp_path, monkeypatch, mode, wanted):
    client, _clock = world(tmp_path, monkeypatch)
    state = load_state()
    topic = state["topics"][0]
    topic["selection"] = selection(mode)
    topic["selected_episode_keys"] = ["episode:s01e01"]
    for row, priority in zip(client.info["files"], wanted, strict=True):
        row["priority"] = priority * 7  # Native priority levels are preserved, not normalized.
    # Native indices can be sparse and differ from their order in the response.
    client.info["files"][0]["index"], client.info["files"][1]["index"] = 9, 4
    bind_missing(topic)
    save_state(state)
    saved = deepcopy(state)
    native = deepcopy(client.info)
    expected = (1, 1) if mode == "all" else (1, 0)
    if wanted == expected:
        receipt = file_repair.request("synthetic")
        assert receipt["files"] == recovery_fingerprint(native["files"])
        assert receipt["phase"] == "requested"
    else:
        with pytest.raises(TowError) as error:
            file_repair.request("synthetic")
        assert error.value.code == "repair.selection_changed"
        assert load_state() == saved  # No authority, budget, or notification is created.
    assert client.info == native
    assert client.calls == []


@pytest.mark.parametrize("mode", ["all", "episodes", "files", "exact"])
@pytest.mark.parametrize("extra_wanted", [False, True])
def test_portable_alias_scope_and_nested_root_use_actual_native_indices_without_io(monkeypatch, mode, extra_wanted):
    canonical = "Season 1/ShowA:.S01E01.mkv"
    other = "Season 1/ShowA:.S01E02.mkv"
    topic = {
        "hash": HASH,
        "save_path": r"D:\TV",
        "selection": selection(mode, canonical),
        "selection_hash": HASH,
        "selection_verified": True,
        "selected_episode_keys": ["episode:s01e01"],
        "file_aliases": {"hash": HASH, "files": [{"path": path, "size": 10} for path in (canonical, other)]},
    }
    rows = [
        {"index": 99, "name": "ShowRoot/Season 1/ShowA_.S01E02.mkv", "size": 10, "priority": int(extra_wanted)},
        {"index": 5, "name": "ShowRoot/Season 1/ShowA_.S01E01.mkv", "size": 10, "priority": 7},
    ]
    original = deepcopy((topic, rows))

    def unexpected_io(*args, **kwargs):
        pytest.fail("selection authority must use lexical mapping, without probing client paths")

    monkeypatch.setattr(Path, "stat", unexpected_io)
    monkeypatch.setattr(Path, "resolve", unexpected_io)
    assert selected_native_indices(topic, rows, content_path=r"d:\tv\ShowRoot", save_path=r"D:\TV") == (
        {5, 99} if mode == "all" else {5}
    )
    assert (topic, rows) == original


@pytest.mark.parametrize("mode", ["episodes", "files", "exact"])
@pytest.mark.parametrize("extra_wanted", [False, True])
def test_native_sanitized_alias_extra_is_rejected_before_any_command(tmp_path, monkeypatch, mode, extra_wanted):
    client, _clock = world(tmp_path, monkeypatch)
    state = load_state()
    topic = state["topics"][0]
    canonical = "Season 1/ShowA:.S01E01.mkv"
    paths = [canonical, "Season 1/ShowA:.S01E02.mkv"]
    topic["selection"] = selection(mode, canonical)
    topic["selected_episode_keys"] = ["episode:s01e01"]
    topic["file_aliases"] = {"hash": HASH, "files": [{"path": path, "size": 10} for path in paths]}
    client.info["content_path"] = str(tmp_path / "ShowRoot")
    for row, path, index in zip(client.info["files"], paths, [17, 6], strict=True):
        row.update(index=index, name="ShowRoot/" + path.replace(":", "_"))
    client.info["files"][1]["priority"] = int(extra_wanted)
    bind_missing(topic)
    save_state(state)
    if extra_wanted:
        with pytest.raises(TowError) as error:
            file_repair.request("synthetic")
        assert error.value.code == "repair.selection_changed"
        assert "repair_pending" not in load_state()["topics"][0]
    else:
        assert file_repair.request("synthetic")["phase"] == "requested"
    assert client.calls == []


def test_selected_padding_is_not_added_to_the_restoration_media_scope(tmp_path, monkeypatch):
    client, _clock = world(tmp_path, monkeypatch)
    client.info["files"].append({"index": 18, "name": ".pad/123", "size": 123, "priority": 1, "progress": 0})
    assert file_repair.request("synthetic")["phase"] == "requested"
    assert client.calls == []
