"""The qBittorrent adapter against the objects qbittorrent-api really returns.

qbittorrent-api wraps every answer: ``torrents_info`` is a ``TorrentInfoList`` of
``TorrentDictionary`` and ``torrents_files`` a ``TorrentFilesList`` of ``TorrentFile`` -
dict subclasses that also allow attribute access. The fakes below build those very classes
(offline, without a client) from JSON shaped like qBittorrent 4.x and 5.x Web API answers,
so a change in how the adapter reads them (item vs attribute access, tag string format,
4.x "paused*" vs 5.x "stopped*" states, files without "index" before 4.3.5) is caught.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any

import pytest
from helpers import make_torrent, multi_file_torrent, raises_code
from qbittorrentapi.torrents import TorrentDictionary, TorrentFile, TorrentFilesList, TorrentInfoList

from tow.clients import qbittorrent
from tow.torrent import parse_torrent_metadata

TORRENT = multi_file_torrent(
    b"Show",
    [
        {b"length": 10, b"path": [b"S01E01.mkv"]},
        {b"length": 20, b"path": [b"S01E02.mkv"]},
    ],
)
META = parse_torrent_metadata(TORRENT)
H = META.client_hash.lower()


def _torrent_json(**overrides: Any) -> dict[str, Any]:
    """A torrents/info entry as qBittorrent 5.x sends it (abridged, real key names)."""
    row = {
        "hash": H,
        "infohash_v1": H,
        "infohash_v2": "",
        "name": "Show",
        "tags": "tow, tow-pending",
        "category": "",
        "state": "stoppedDL",
        "save_path": "M:\\TV",
        "download_path": "",
        "content_path": "M:\\TV\\Show",
        "progress": 0.25,
        "size": 30,
        "total_size": 30,
        "amount_left": 22,
        "downloaded": 8,
        "uploaded": 0,
        "added_on": 1_790_000_000,
        "completion_on": -1,
        "dlspeed": 0,
        "eta": 8640000,
        "num_seeds": 0,
        "auto_tmm": False,
        "reannounce": 0,
    }
    row.update(overrides)
    return row


def _files_json(priorities: dict[int, int], *, with_index: bool = True) -> list[dict[str, Any]]:
    rows = [
        {
            "index": 0,
            "name": "Show/S01E01.mkv",
            "size": 10,
            "priority": priorities[0],
            "progress": 0.8,
            "is_seed": False,
            "piece_range": [0, 0],
            "availability": 1.0,
        },
        {
            "index": 1,
            "name": "Show/S01E02.mkv",
            "size": 20,
            "priority": priorities[1],
            "progress": 0.0,
            "is_seed": False,
            "piece_range": [0, 0],
            "availability": 0.5,
        },
    ]
    if not with_index:  # qBittorrent before 4.3.5 sent no "index"
        for row in rows:
            row.pop("index")
    return rows


class QbitApi:
    """The parts of qbittorrentapi.Client the adapter uses, answering with qbittorrentapi objects.

    ``api="5"`` has torrents_stop/start and stopped* states; ``api="4"`` only pause/resume
    and paused* states, as qBittorrent 4.x's Web API.
    """

    def __init__(self, *, api: str = "5", present: bool = True, **torrent: Any) -> None:
        self.api = api
        self.app = SimpleNamespace(version="5.1.2" if api == "5" else "4.6.7", web_api_version="2.11.4")
        torrent.setdefault("state", "stoppedDL" if api == "5" else "pausedDL")
        self.torrent = _torrent_json(**torrent) if present else None
        self.priorities = {0: 1, 1: 1}
        self.with_index = True
        self.calls: list[tuple] = []
        self.dropped: set[tuple] = set()  # (file_ids, priority) writes the client ignores
        if api == "4":
            self.torrents_pause = self._pause
            self.torrents_resume = self._resume
        else:
            self.torrents_stop = self._pause
            self.torrents_start = self._resume

    def _state(self, stopped: bool) -> str:
        done = float(self.torrent["progress"]) >= 1
        if self.api == "5":
            return ("stoppedUP" if done else "stoppedDL") if stopped else ("stalledUP" if done else "downloading")
        return ("pausedUP" if done else "pausedDL") if stopped else ("stalledUP" if done else "downloading")

    def _pause(self, *, torrent_hashes):
        self.calls.append(("stop", torrent_hashes))
        self.torrent["state"] = self._state(True)

    def _resume(self, *, torrent_hashes):
        self.calls.append(("start", torrent_hashes))
        self.torrent["state"] = self._state(False)

    def torrents_info(self, *, torrent_hashes=None):
        rows = []
        if self.torrent is not None and (torrent_hashes is None or torrent_hashes == self.torrent["hash"]):
            rows.append(copy.deepcopy(self.torrent))
        return TorrentInfoList(rows, client=None)

    def torrents_files(self, *, torrent_hash):
        return TorrentFilesList(_files_json(self.priorities, with_index=self.with_index), client=None)

    def torrents_file_priority(self, *, torrent_hash, file_ids, priority):
        self.calls.append(("priority", tuple(file_ids), priority))
        for index in file_ids:
            if (tuple(file_ids), priority) not in self.dropped:
                self.priorities[index] = priority

    def torrents_remove_tags(self, *, tags, torrent_hashes):
        self.calls.append(("remove_tags", tags))
        current = [tag.strip() for tag in self.torrent["tags"].split(",") if tag.strip()]
        self.torrent["tags"] = ", ".join(tag for tag in current if tag != tags)


def _adapter(monkeypatch, api: QbitApi) -> qbittorrent.QBittorrentClient:
    monkeypatch.setattr(qbittorrent, "Client", lambda **_kwargs: api)
    monkeypatch.setattr(qbittorrent.time, "sleep", lambda _seconds: None)
    return qbittorrent.QBittorrentClient("http://qbit", 8080, "user", "password")


# The start, ownership and rollback waits every client shares: test_clients_managed.py.


@pytest.mark.parametrize("state", ["error", "missingFiles", "unknown"])
def test_pending_release_cannot_confirm_unsafe_torrent(monkeypatch, state):
    api = QbitApi(state="downloading")
    adapter = _adapter(monkeypatch, api)
    real_remove = api.torrents_remove_tags

    def remove(**kwargs):
        real_remove(**kwargs)
        api.torrent["state"] = state

    monkeypatch.setattr(api, "torrents_remove_tags", remove)
    with raises_code("client.managed.error_state") as error:
        adapter._clear_pending(H)
    assert error.value.params["state"] == state.casefold()


def test_the_fakes_are_the_real_qbittorrentapi_types():
    api = QbitApi()
    row = api.torrents_info(torrent_hashes=H)[0]
    file = api.torrents_files(torrent_hash=H)[0]
    assert isinstance(row, TorrentDictionary)
    assert isinstance(file, TorrentFile)
    assert row["state"] == row.state == "stoppedDL"  # item and attribute access
    assert row.reannounce_in == 0  # qbittorrent-api renames "reannounce"
    assert file["priority"] == file.priority == 1


# --- reading -----------------------------------------------------------------------------------


def test_inspect_reads_a_qbittorrent_5_torrent_and_its_files(monkeypatch):
    api = QbitApi()
    api.priorities = {0: 1, 1: 0}
    info = _adapter(monkeypatch, api).inspect_torrent(H.upper())

    assert info == {
        "hash": H,
        "infohash_v1": H,
        "infohash_v2": "",
        "progress": 0.25,
        "downloaded": 8,
        "added_on": 1_790_000_000,
        "completion_on": -1,
        "save_path": "M:\\TV",
        "content_path": "M:\\TV\\Show",
        "state": "stoppedDL",
        "tags": ["tow", "tow-pending"],
        "files": [
            {"index": 0, "name": "Show/S01E01.mkv", "size": 10, "progress": 0.8, "priority": 1},
            {"index": 1, "name": "Show/S01E02.mkv", "size": 20, "progress": 0.0, "priority": 0},
        ],
    }


def test_inspect_of_an_old_qbittorrent_without_v2_ids_or_file_index(monkeypatch):
    api = QbitApi(api="4")
    del api.torrent["infohash_v1"]
    del api.torrent["infohash_v2"]
    api.with_index = False
    info = _adapter(monkeypatch, api).inspect_torrent(H)

    assert (info["infohash_v1"], info["infohash_v2"]) == ("", "")
    assert info["state"] == "pausedDL"
    # qbittorrent-api numbers the files itself when the server sends no index.
    assert [row["index"] for row in info["files"]] == [0, 1]


@pytest.mark.parametrize(
    ("raw", "tags"),
    [
        ("tow, tow-pending", ["tow", "tow-pending"]),
        ("tow,tow-pending", ["tow", "tow-pending"]),
        (" kids ,tow, ,tow ", ["kids", "tow"]),
        ("", []),
    ],
)
def test_tag_strings_are_split_trimmed_and_deduplicated(monkeypatch, raw, tags):
    api = QbitApi(tags=raw)
    assert _adapter(monkeypatch, api).inspect_torrent(H)["tags"] == tags


def test_a_torrent_the_client_does_not_have(monkeypatch):
    api = QbitApi(present=False)
    adapter = _adapter(monkeypatch, api)
    assert adapter.inspect_torrent(H) is None
    assert adapter.has_hash(H) is False  # an empty TorrentInfoList is falsy
    api.torrent = _torrent_json()
    assert adapter.has_hash(H.upper()) is True


def test_full_listing_finds_a_torrent_by_its_v1_id(monkeypatch):
    v2_id = "ab" * 32
    api = QbitApi(hash=v2_id[:40], infohash_v2=v2_id)
    adapter = _adapter(monkeypatch, api)
    # Filtered by the v1 hash qBittorrent answers nothing; the full listing has it.
    assert adapter._resolved_hash(H) == v2_id[:40].upper()
    assert adapter._resolved_hash(v2_id) == v2_id[:40].upper()
    assert adapter._resolved_hash("cd" * 20) is None


# --- stop / start states of qBittorrent 4.x and 5.x -------------------------------------------


@pytest.mark.parametrize(("api_version", "state"), [("5", "stoppedUP"), ("4", "pausedUP"), ("4", "pausedDL")])
def test_an_already_stopped_owned_torrent_is_left_as_it_is(monkeypatch, api_version, state):
    api = QbitApi(api=api_version, tags="tow", state=state)
    info = _adapter(monkeypatch, api).stop_owned_torrent(H)
    assert info["state"] == state
    assert api.calls == []


@pytest.mark.parametrize(("api_version", "stopped_state"), [("5", "stoppedDL"), ("4", "pausedDL")])
def test_a_running_owned_torrent_is_stopped_through_the_api_it_has(monkeypatch, api_version, stopped_state):
    api = QbitApi(api=api_version, tags="tow", state="downloading")
    info = _adapter(monkeypatch, api).stop_owned_torrent(H.upper())
    assert info["state"] == stopped_state
    assert api.calls == [("stop", H)]


def test_stop_refuses_missing_and_foreign_torrents(monkeypatch):
    adapter = _adapter(monkeypatch, QbitApi(present=False))
    with raises_code("client.managed.missing", RuntimeError):
        adapter.stop_owned_torrent(H)
    api = QbitApi(tags="manual, kids", state="downloading")
    with raises_code("client.managed.not_owned_stop", RuntimeError):
        _adapter(monkeypatch, api).stop_owned_torrent(H)
    assert api.calls == []


def test_missing_files_while_stopping_is_an_unsafe_state(monkeypatch):
    api = QbitApi(tags="tow", state="downloading")

    def stop(*, torrent_hashes):
        api.torrent["state"] = "missingFiles"

    api.torrents_stop = stop
    with raises_code("client.managed.error_state", RuntimeError):
        _adapter(monkeypatch, api).stop_owned_torrent(H)


@pytest.mark.parametrize("state", ["stalledUP", "uploading", "forcedDL", "queuedDL", "checkingDL", "metaDL"])
def test_active_states_confirm_a_start(monkeypatch, state):
    api = QbitApi(state=state)
    assert _adapter(monkeypatch, api)._wait_started(H)["state"] == state


def test_a_finished_torrent_paused_by_qbittorrent_4_counts_as_started(monkeypatch):
    api = QbitApi(api="4", state="pausedUP", progress=1.0, amount_left=0)
    assert _adapter(monkeypatch, api)._wait_started(H)["state"] == "pausedUP"


def test_missing_files_after_start_is_an_unsafe_state(monkeypatch):
    api = QbitApi(state="missingFiles")
    with raises_code("client.managed.error_state", RuntimeError):
        _adapter(monkeypatch, api)._wait_started(H)


# --- selection changes on 4.x shapes ----------------------------------------------------------


def test_selection_change_on_qbittorrent_4_pauses_applies_and_resumes(monkeypatch):
    api = QbitApi(api="4", tags="tow", state="downloading")
    info = _adapter(monkeypatch, api).configure_torrent_selection(TORRENT, H, [1])

    assert api.priorities == {0: 0, 1: 1}
    assert info["state"] == "downloading"
    assert [call[0] for call in api.calls] == ["stop", "priority", "priority", "start"]


def test_selection_change_of_a_stopped_torrent_leaves_it_stopped(monkeypatch):
    api = QbitApi(tags="tow")
    info = _adapter(monkeypatch, api).configure_torrent_selection(TORRENT, H, [0])
    assert api.priorities == {0: 1, 1: 0}
    assert info["state"] == "stoppedDL"
    assert ("start", H) not in api.calls


def test_a_pending_torrent_is_released_after_the_selection(monkeypatch):
    api = QbitApi(api="4")  # tags "tow, tow-pending", paused
    info = _adapter(monkeypatch, api).configure_torrent_selection(TORRENT, H, [0, 1])
    assert info["tags"] == ["tow"]
    assert info["state"] == "downloading"
    assert api.calls[-1] == ("remove_tags", "tow-pending")


def test_a_failed_selection_of_a_stopped_torrent_is_rolled_back_and_stays_stopped(monkeypatch):
    api = QbitApi(tags="tow")
    api.priorities = {0: 1, 1: 6}  # qBittorrent priorities: 1 normal, 6 high, 7 maximal
    api.dropped.add(((1,), 1))  # the client drops the new selection
    with raises_code("client.managed.wrong_selection", RuntimeError):
        _adapter(monkeypatch, api).configure_torrent_selection(TORRENT, H, [1])
    assert api.torrent["state"] == "stoppedDL"
    assert ("start", H) not in api.calls
    # The rollback re-applied the previous priorities, one call per priority value.
    assert api.calls[-2:] == [("priority", (0,), 1), ("priority", (1,), 6)]
    assert api.priorities == {0: 1, 1: 6}


def test_an_invalid_file_number_is_refused_and_a_running_torrent_restarted(monkeypatch):
    api = QbitApi(tags="tow", state="downloading")
    with raises_code("client.managed.bad_selection", RuntimeError):
        _adapter(monkeypatch, api).configure_torrent_selection(TORRENT, H, [5])
    assert api.priorities == {0: 1, 1: 1}
    assert api.torrent["state"] == "downloading"


def test_a_pure_v2_torrent_is_matched_only_by_its_own_id(monkeypatch):
    pure_v2 = make_torrent(
        {
            b"file tree": {b"S01E01.mkv": {b"": {b"length": 10, b"pieces root": b"r" * 32}}},
            b"meta version": 2,
            b"name": b"Show",
            b"piece length": 16384,
        }
    )
    api = QbitApi(tags="tow")
    with raises_code("client.managed.hash_changed_selection", RuntimeError):
        _adapter(monkeypatch, api).configure_torrent_selection(pure_v2, H, [0])
    assert api.calls == []
