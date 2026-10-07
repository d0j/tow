"""A real file must never be another real file's directory, on any client OS."""

import random
from itertools import combinations
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from helpers import make_torrent, multi_file_torrent

from tow import content
from tow.check import topic as topic_step
from tow.paths import tmp_dir
from tow.store import load_download_history, load_state, save_download_history, save_state
from tow.torrent import (
    TorrentFile,
    TorrentPathConflictError,
    _validate_unique_paths,
    looks_like_torrent,
    parse_torrent_metadata,
    windows_path_key,
)
from tow.web import app, services

URL = "https://tracker.example/topic/1"
CONFLICTS = [
    ("A", "A/B"),
    ("A/B", "A/B/C"),
    ("Season", "season/E01.mkv"),
    ("a:b", "a_b/E01.mkv"),
    ("Vol.", "Vol/E01.mkv"),
    ("CON", "CON_/E01.mkv"),
    ("é", "e\u0301/E01.mkv"),
    ("Сезон", "сезон/E01.mkv"),
]


def torrent(paths, kind="v1"):
    raw_paths = [tuple(part.encode() for part in path.split("/")) for path in paths]
    files = [{b"path": list(parts), b"length": 0} for parts in raw_paths]
    if kind == "v1":
        return multi_file_torrent(b"Show", files, pieces=b"")
    tree = {}
    for parts in raw_paths:
        node = tree
        for part in parts:
            node = node.setdefault(part, {})
        node[b""] = {b"length": 0}
    info = {b"name": b"Show", b"meta version": 2, b"piece length": 16384, b"file tree": tree}
    if kind == "hybrid":
        info.update({b"files": sorted(files, key=lambda row: row[b"path"]), b"pieces": b""})
    return make_torrent(info)


@pytest.mark.parametrize("paths", CONFLICTS)
@pytest.mark.parametrize("kind", ["v1", "v2", "hybrid"])
@pytest.mark.parametrize("reverse", [False, True])
def test_file_directory_conflicts_fail_before_selection(paths, kind, reverse):
    data = torrent(paths[::-1] if reverse else paths, kind)
    assert looks_like_torrent(data), "a filesystem policy failure is not an HTML login page"
    with pytest.raises(TorrentPathConflictError, match=r"file.*directory"):
        parse_torrent_metadata(data)


@pytest.mark.parametrize("kind", ["v1", "v2", "hybrid"])
@pytest.mark.parametrize("reverse", [False, True])
def test_unrelated_lexical_neighbor_cannot_hide_an_ancestor(kind, reverse):
    paths = ["A", "A-", "A/B"]
    with pytest.raises(TorrentPathConflictError, match=r"file.*directory"):
        parse_torrent_metadata(torrent(paths[::-1] if reverse else paths, kind))


@pytest.mark.parametrize(
    "paths",
    [
        ["A", "AB/C"],
        ["A-", "A/B"],
        ["E01.mkv", "E01.mkv.backup/E02.mkv"],
        ["Season/E01.mkv", "season/E02.mkv"],
        ["Season/E01.mkv", "Season/Subtitles/E01.srt"],
    ],
)
@pytest.mark.parametrize("kind", ["v1", "v2", "hybrid"])
def test_valid_prefixes_and_shared_directories_keep_their_files(paths, kind):
    data = torrent(paths, kind)
    metadata = parse_torrent_metadata(data)
    assert {row.path for row in metadata.files} == set(paths)
    assert all(not row.is_pad and row.size == 0 for row in metadata.files)


@pytest.mark.parametrize("pad_first", [False, True])
def test_virtual_padding_is_not_a_real_directory_conflict(pad_first):
    rows = [{b"path": [b"A"], b"length": 0}, {b"path": [b"A", b"B"], b"length": 0, b"attr": b"p"}]
    metadata = parse_torrent_metadata(multi_file_torrent(b"Show", rows[::-1] if pad_first else rows, pieces=b""))
    assert len(metadata.files) == 2
    assert sum(row.is_pad for row in metadata.files) == 1


def test_path_conflict_check_matches_pairwise_oracle_in_any_order():
    rng = random.Random(261012)
    names = ["A", "a", "A-", "AB", "A:B", "A_B", "Vol.", "Vol", "CON", "CON_", "é", "e\u0301"]
    for _ in range(600):
        rows = [
            TorrentFile(index, "/".join(rng.choices(names, k=rng.randrange(1, 5))), 0, rng.random() < 0.15)
            for index in range(rng.randrange(1, 10))
        ]
        keys = [windows_path_key(row.path) for row in rows if not row.is_pad]
        conflict = not keys or any(
            left == right or left.startswith(right + "/") or right.startswith(left + "/")
            for left, right in combinations(keys, 2)
        )
        for _order in range(3):
            rng.shuffle(rows)
            if conflict:
                with pytest.raises(ValueError, match="torrent contains"):
                    _validate_unique_paths(rows)
            else:
                _validate_unique_paths(rows)


@pytest.mark.parametrize("kind", ["v1", "v2", "hybrid"])
@pytest.mark.parametrize("language", ["en", "ru"])
def test_conflicting_preparation_preserves_state_and_existing_encrypted_cache(monkeypatch, kind, language):
    from tow import trackers

    good = torrent(["A", "AB/C"])
    bad = torrent(["PrivateFixture", "PrivateFixture/B"], kind)
    save_state({"topics": []})
    snapshot = content.prepare(good, URL, "main")
    folder = tmp_dir() / "content"
    cache_before = {path.name: path.read_bytes() for path in folder.iterdir()}
    tracker = SimpleNamespace(name="fixture", spec={})
    monkeypatch.setattr(
        services,
        "load_config",
        lambda: {"language": language, "clients": [{"id": "main", "kind": "qbittorrent", "default": True}]},
    )
    monkeypatch.setattr(trackers, "load_trackers", lambda *_args: {"fixture": tracker})
    monkeypatch.setattr(trackers, "match_tracker", lambda *_args: tracker)
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    response = client.post(
        "/content/prepare", data={"url": URL, "client_id": "main"}, files={"torrent": ("fixture.torrent", bad)}
    )
    assert response.status_code == 400
    assert response.json()["code"] == "content.path_conflict"
    assert "PrivateFixture" not in response.text
    expected = "file and a folder" if language == "en" else "файл и как папка"
    assert expected in response.json()["error"]
    assert response.headers["cache-control"] == "no-store"
    assert load_state()["topics"] == []
    assert {path.name: path.read_bytes() for path in folder.iterdir()} == cache_before
    assert content.read(snapshot["token"], URL, "main") == good


@pytest.mark.parametrize("apply", [False, True])
def test_conflicting_tracker_revision_never_mutates_client_or_history(monkeypatch, apply):
    from test_check_contract import FakeClient, _wire_fake_check

    from tow import check

    client = FakeClient()
    tracker = _wire_fake_check(monkeypatch, client)
    monkeypatch.setattr(topic_step, "parse_torrent_metadata", parse_torrent_metadata)
    monkeypatch.setattr(tracker, "fetch_torrent", lambda *_args, **_kwargs: torrent(["A", "A/B"]))
    save_state({"topics": [{"id": "fixture", "title": "Show", "url": URL, "save_path": "M:/TV", "hash": None}]})
    save_download_history({"schema_version": 1, "topics": {}})
    state_before = load_state()
    history_before = load_download_history()
    row = check.run_check(apply=apply, notify=False, how="test")["results"][0]
    assert row["ok"] is False
    assert row["error_record"]["code"] == "content.path_conflict"
    assert row["error_class"] == "error"
    assert client.add_calls == client.configure_calls == 0
    assert load_state()["topics"][0]["hash"] is None
    if apply:
        assert load_state()["topics"][0]["last_error_code"] == "content.path_conflict"
    else:
        assert load_state() == state_before
    assert load_download_history() == history_before
