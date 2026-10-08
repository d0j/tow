"""qBittorrent, Transmission and Deluge adapters against stateful fake servers (no real client,
no network): the shared contract of ``tow.clients.managed`` runs against all three.

The same contract was checked once against live transmission-daemon 4.1.3 and Deluge 2.2.0;
these tests keep it from regressing.
"""

from __future__ import annotations

import base64
import json
import math
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from helpers import bencode, make_torrent
from qbittorrentapi.torrents import TorrentFilesList, TorrentInfoList

from tow.clients import qbittorrent
from tow.clients.deluge import DelugeClient
from tow.clients.managed import ClientError, ManagedClient
from tow.clients.transmission import TransmissionClient, base_url
from tow.torrent import parse_torrent_metadata

PIECE = 16384
SIZES = {"e01.mkv": 40000, "e02.mkv": 30000, "info.nfo": 100}
INFO = {
    b"name": "Show S01",
    b"piece length": PIECE,
    b"pieces": b"\0" * 20 * math.ceil(sum(SIZES.values()) / PIECE),
    b"files": [{b"length": size, b"path": [name]} for name, size in SIZES.items()],
}
TORRENT = make_torrent(INFO)
META = parse_torrent_metadata(TORRENT)
H = META.client_hash
K = H.lower()  # how the fake servers key their torrents
E01, E02, NFO = (next(f.index for f in META.files if f.path.endswith(name)) for name in SIZES)


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    monkeypatch.setattr(ManagedClient, "PAUSE", 0)
    monkeypatch.setattr(ManagedClient, "POLLS", 5)


class Torrent:
    def __init__(self, content: bytes, path: str, labels: list[str], *, paused: bool) -> None:
        meta = parse_torrent_metadata(content)
        self.hash = meta.client_hash.lower()
        self.name = meta.name
        self.files = [(f"{meta.name}/{f.path}", f.size) for f in meta.files]
        self.wanted = [True] * len(self.files)
        self.path = path
        self.labels = list(labels)
        self.running = not paused


# --- a fake transmission-daemon -------------------------------------------------------------


class FakeTransmission:
    SESSION = "sid-1"

    def __init__(self, *, rpc_version: int = 19, password: str = "pw") -> None:
        self.torrents: dict[str, Torrent] = {}
        self.rpc_version = rpc_version
        self.password = password
        self.calls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        expected = "Basic " + base64.b64encode(f"tow:{self.password}".encode()).decode()
        if request.headers.get("authorization") != expected:
            return httpx.Response(401)
        if request.headers.get("X-Transmission-Session-Id") != self.SESSION:
            return httpx.Response(409, headers={"X-Transmission-Session-Id": self.SESSION})
        body = json.loads(request.content)
        method, args = body["method"], body.get("arguments") or {}
        self.calls.append(method)
        result: dict[str, Any] = {}
        ids = [str(i).lower() for i in args.get("ids") or []]
        targets = [self.torrents[i] for i in ids if i in self.torrents]
        if method == "session-get":
            result = {"version": "4.1.3", "rpc-version": self.rpc_version}
        elif method == "torrent-get":
            rows = targets if "ids" in args else list(self.torrents.values())
            fields = args.get("fields")
            result = {
                "torrents": [
                    {key: value for key, value in self._row(t).items() if fields is None or key in fields} for t in rows
                ]
            }
        elif method == "torrent-add":
            torrent = Torrent(
                base64.b64decode(args["metainfo"]),
                args["download-dir"],
                args.get("labels") or [],
                paused=args["paused"],
            )
            if torrent.hash in self.torrents:
                result = {"torrent-duplicate": {"hashString": torrent.hash}}
            else:
                self.torrents[torrent.hash] = torrent
                result = {"torrent-added": {"hashString": torrent.hash}}
        elif method == "torrent-set":
            for t in targets:
                for index in args.get("files-wanted") or []:
                    t.wanted[index] = True
                for index in args.get("files-unwanted") or []:
                    t.wanted[index] = False
                if "labels" in args:
                    t.labels = list(args["labels"])
        elif method in ("torrent-start", "torrent-stop"):
            for t in targets:
                t.running = method == "torrent-start"
        elif method == "torrent-set-location":
            for t in targets:
                t.path = args["location"]
        else:
            return httpx.Response(200, json={"result": f"unknown method {method}"})
        return httpx.Response(200, json={"result": "success", "arguments": result})

    @staticmethod
    def _row(t: Torrent) -> dict[str, Any]:
        return {
            "hashString": t.hash,
            "name": t.name,
            "status": 4 if t.running else 0,
            "error": 0,
            "percentDone": 0.0,
            "downloadedEver": 0,
            "addedDate": 1000,
            "doneDate": 0,
            "downloadDir": t.path,
            "labels": t.labels,
            "files": [{"name": name, "length": size, "bytesCompleted": 0} for name, size in t.files],
            "fileStats": [{"wanted": wanted, "priority": 0} for wanted in t.wanted],
        }


def _transmission(server: FakeTransmission, password: str = "pw") -> TransmissionClient:
    return TransmissionClient("127.0.0.1", 9091, "tow", password, transport=httpx.MockTransport(server.handler))


# --- a fake Deluge Web ----------------------------------------------------------------------


class FakeDeluge:
    def __init__(self, *, password: str = "deluge", host_online: bool = True) -> None:
        self.torrents: dict[str, Torrent] = {}
        self.password = password
        self.host_online = host_online
        self.logged_in = False
        self.connected = False
        self.plugins: list[str] = []
        self.labels: set[str] = set()
        self.calls: list[str] = []
        self.prefetch: tuple[str, str] | None = None
        # Deluge Web's list of the daemon's methods: read when it attaches, and on a plugin event
        # (which it may miss: then a plugin's methods stay unknown until it attaches again).
        self.misses_plugin_event = False
        self.known_plugins: list[str] = []
        # The daemons Deluge Web knows, and the one it is attached to.
        self.hosts = ["host-1"]
        self.attached_to: str | None = None
        self.reports_connected = True

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method, params = body["method"], body["params"]
        self.calls.append(method)
        if method == "auth.login":
            self.logged_in = params[0] == self.password
            return self._ok(body, self.logged_in)
        if not self.logged_in:
            return self._error(body, "Not authenticated", 1)
        if method.startswith(("core.", "label.", "daemon.")) and not self.connected:
            return self._error(body, "Unknown method", 2)
        if method.startswith("label.") and "Label" not in self.known_plugins:
            return self._error(body, "Unknown method", 2)
        result = getattr(self, "m_" + method.replace(".", "_"))(*params)
        return self._ok(body, result)

    @staticmethod
    def _ok(body: dict, result: Any) -> httpx.Response:
        return httpx.Response(200, json={"result": result, "error": None, "id": body["id"]})

    @staticmethod
    def _error(body: dict, message: str, code: int) -> httpx.Response:
        return httpx.Response(200, json={"result": None, "error": {"message": message, "code": code}, "id": body["id"]})

    def m_web_connected(self):
        return self.connected

    def m_web_get_hosts(self):
        return [[host, "127.0.0.1", 58846 + i, "localclient"] for i, host in enumerate(self.hosts)]

    def m_web_get_host_status(self, host_id):
        if self.connected and host_id == self.attached_to and self.reports_connected:
            return [host_id, "Connected", "2.2.0"]
        return [host_id, "Online" if self.host_online else "Offline", "2.2.0"]

    def m_web_connect(self, host_id):
        self.connected = True
        self.attached_to = host_id
        self.known_plugins = list(self.plugins)
        return []

    def m_web_disconnect(self):
        self.connected = False
        self.attached_to = None
        return "disconnected"

    def m_daemon_get_version(self):
        return "2.2.0"

    def m_core_get_libtorrent_version(self):
        return "2.1.1.0"

    def m_core_get_enabled_plugins(self):
        return list(self.plugins)

    def m_core_enable_plugin(self, name):
        self.plugins.append(name)
        if not self.misses_plugin_event:
            self.known_plugins = list(self.plugins)
        return True

    def m_label_get_labels(self):
        return sorted(self.labels)

    def m_label_add(self, name):
        self.labels.add(name)

    def m_label_set_torrent(self, torrent_id, label):
        assert label in self.labels or label == ""
        self.torrents[torrent_id].labels = [label] if label else []

    def m_core_add_torrent_file(self, filename, dump, options):
        torrent = Torrent(base64.b64decode(dump), options["download_location"], [], paused=options["add_paused"])
        if torrent.hash in self.torrents:
            return None
        self.torrents[torrent.hash] = torrent
        return torrent.hash

    def m_core_get_session_state(self):
        return sorted(self.torrents)

    def m_core_get_torrent_status(self, torrent_id, keys):
        t = self.torrents.get(torrent_id)
        if t is None:
            return {}
        row = {
            "hash": t.hash,
            "name": t.name,
            "state": "Downloading" if t.running else "Paused",
            "progress": 0.0,
            "all_time_download": 0,
            "time_added": 1000,
            "completed_time": 0,
            "download_location": t.path,
            "files": [{"index": i, "path": name, "size": size} for i, (name, size) in enumerate(t.files)],
            "file_priorities": [4 if wanted else 0 for wanted in t.wanted],
            "file_progress": [0.0] * len(t.files),
            "label": t.labels[0] if t.labels else "",
        }
        return {key: value for key, value in row.items() if key in keys}

    def m_core_set_torrent_options(self, ids, options):
        for torrent_id in ids:
            self.torrents[torrent_id].wanted = [p > 0 for p in options["file_priorities"]]

    def m_core_pause_torrents(self, ids):
        for torrent_id in ids:
            self.torrents[torrent_id].running = False

    def m_core_resume_torrents(self, ids):
        for torrent_id in ids:
            self.torrents[torrent_id].running = True

    def m_core_move_storage(self, ids, path):
        for torrent_id in ids:
            self.torrents[torrent_id].path = path

    def m_core_prefetch_magnet_metadata(self, magnet, timeout):
        return list(self.prefetch) if self.prefetch else [magnet, ""]


def _deluge(server: FakeDeluge, password: str = "deluge") -> DelugeClient:
    return DelugeClient("127.0.0.1", 8112, password, transport=httpx.MockTransport(server.handler))


# --- a fake qBittorrent Web API (what qbittorrentapi.Client answers) -------------------------


class FakeQbit:
    def __init__(self) -> None:
        self.torrents: dict[str, Torrent] = {}
        self.app = SimpleNamespace(version="5.1.2", web_api_version="2.11.4")
        self.calls: list[str] = []

    def torrents_info(self, torrent_hashes: str | None = None, limit: int | None = None) -> TorrentInfoList:
        self.calls.append("torrents_info")
        rows = [t for key, t in self.torrents.items() if torrent_hashes is None or key == torrent_hashes.lower()]
        return TorrentInfoList([self._row(t) for t in rows][:limit], client=None)

    @staticmethod
    def _row(t: Torrent) -> dict[str, Any]:
        return {
            "hash": t.hash,
            "infohash_v1": t.hash,
            "infohash_v2": "",
            "name": t.name,
            "tags": ", ".join(t.labels),
            "state": "downloading" if t.running else "stoppedDL",
            "save_path": t.path,
            "content_path": f"{t.path}/{t.name}",
            "progress": 0.0,
            "downloaded": 0,
            "added_on": 1000,
            "completion_on": -1,
        }

    def torrents_files(self, *, torrent_hash: str) -> TorrentFilesList:
        self.calls.append("torrents_files")
        t = self.torrents.get(torrent_hash.lower())
        files = list(zip(t.files, t.wanted, strict=True)) if t else []
        rows = [
            {"index": i, "name": name, "size": size, "progress": 0.0, "priority": int(wanted)}
            for i, ((name, size), wanted) in enumerate(files)
        ]
        return TorrentFilesList(rows, client=None)

    def torrents_add(self, *, torrent_files: bytes, save_path: str, tags: str, is_stopped: bool, **_options: Any):
        self.calls.append("torrents_add")
        torrent = Torrent(torrent_files, save_path, tags.split(","), paused=is_stopped)
        if torrent.hash in self.torrents:
            return "Fails."
        self.torrents[torrent.hash] = torrent
        return "Ok."

    def torrents_file_priority(self, *, torrent_hash: str, file_ids: list[int], priority: int) -> None:
        self.calls.append("torrents_file_priority")
        for index in file_ids:
            self.torrents[torrent_hash.lower()].wanted[index] = priority > 0

    def torrents_stop(self, *, torrent_hashes: str) -> None:
        self.calls.append("torrents_stop")
        self.torrents[torrent_hashes.lower()].running = False

    def torrents_start(self, *, torrent_hashes: str) -> None:
        self.calls.append("torrents_start")
        self.torrents[torrent_hashes.lower()].running = True

    def torrents_add_tags(self, *, tags: str, torrent_hashes: str) -> None:
        self.calls.append("torrents_add_tags")
        self.torrents[torrent_hashes.lower()].labels.append(tags)

    def torrents_remove_tags(self, *, tags: str, torrent_hashes: str) -> None:
        self.calls.append("torrents_remove_tags")
        torrent = self.torrents[torrent_hashes.lower()]
        torrent.labels = [label for label in torrent.labels if label != tags]

    def torrents_set_location(self, *, location: str, torrent_hashes: str) -> None:
        self.calls.append("torrents_set_location")
        self.torrents[torrent_hashes.lower()].path = location


def _qbittorrent(server: FakeQbit, monkeypatch: pytest.MonkeyPatch) -> qbittorrent.QBittorrentClient:
    monkeypatch.setattr(qbittorrent, "Client", lambda **_kwargs: server)
    return qbittorrent.QBittorrentClient("127.0.0.1", 8080, "tow", "pw")


# --- the shared contract, for every client ----------------------------------------------------

# What restoring a selection or a start looks like on each fake server.
WRITES = {"torrent-set", "torrent-start", "core.set_torrent_options", "core.resume_torrents"}
WRITES |= {"torrents_file_priority", "torrents_start"}


@pytest.fixture(params=["qbittorrent", "transmission", "deluge"])
def client(request, monkeypatch):
    if request.param == "qbittorrent":
        server: Any = FakeQbit()
        return _qbittorrent(server, monkeypatch), server
    if request.param == "transmission":
        server = FakeTransmission()
        return _transmission(server), server
    server = FakeDeluge()
    return _deluge(server), server


def _overlay(adapter, monkeypatch, **fields: Any) -> None:
    """The adapter reads ``fields`` (a state, a progress) over what its client reports: values
    no client answers with, for the judgment every client shares."""
    real = adapter.inspect_torrent
    monkeypatch.setattr(adapter, "inspect_torrent", lambda infohash: (info := real(infohash)) and {**info, **fields})


def _wanted(info: dict) -> list[bool]:
    return [row["priority"] > 0 for row in info["files"]]


def test_add_selects_files_starts_and_clears_pending(client, tmp_path):
    adapter, server = client
    info = adapter.add_torrent_selected(TORRENT, str(tmp_path), H, [E01])
    assert info["tags"] == ["tow"]
    assert _wanted(info) == [index == E01 for index in range(3)]
    assert info["state"] == "downloading"
    assert info["save_path"] == str(tmp_path)
    assert adapter.has_hash(H)
    assert server.torrents[K].running


def test_add_happens_stopped_before_the_selection(client, tmp_path, monkeypatch):
    adapter, server = client
    seen: list[tuple[bool, list[bool]]] = []
    original = adapter._set_wanted

    def spy(infohash, wanted, all_ids):
        seen.append((server.torrents[K].running, list(server.torrents[K].wanted)))
        original(infohash, wanted, all_ids)

    monkeypatch.setattr(adapter, "_set_wanted", spy)
    adapter.add_torrent_selected(TORRENT, str(tmp_path), H, [E02])
    assert seen == [(False, [True, True, True])]


def test_an_add_without_start_stays_stopped_selected_and_released(client, tmp_path):
    """Files that do not fit yet: added, selected, read back and released, but not started;
    TOW starts it later (start_owned_torrent), its start read back."""
    adapter, server = client
    info = adapter.add_torrent_selected(TORRENT, str(tmp_path), H, [E01], start=False)
    assert info["tags"] == ["tow"]
    assert _wanted(info) == [index == E01 for index in range(3)]
    assert info["state"].startswith(("stopped", "paused"))
    assert not server.torrents[K].running
    started = adapter.start_owned_torrent(H)
    assert server.torrents[K].running
    assert not started["state"].startswith(("stopped", "paused"))
    assert adapter.start_owned_torrent(H)["state"] == started["state"]  # running: left as it is


def test_start_owned_torrent_refuses_a_foreign_or_missing_torrent(client, tmp_path):
    adapter, server = client
    with pytest.raises(ClientError, match="нет в клиенте"):
        adapter.start_owned_torrent(H)
    adapter.add_torrent_selected(TORRENT, str(tmp_path), H, [E01], start=False)
    adapter._remove_label(H, adapter._owner_tags(H) or [], "tow")  # the owner took TOW's mark away
    with pytest.raises(ClientError):
        adapter.start_owned_torrent(H)
    assert not server.torrents[K].running


def test_duplicate_and_bad_selection_are_refused(client, tmp_path):
    adapter, server = client
    with pytest.raises(ClientError, match="неверные номера"):
        adapter.add_torrent_selected(TORRENT, str(tmp_path), H, [7])
    with pytest.raises(ClientError, match="не указана папка"):
        adapter.add_torrent_selected(TORRENT, "", H, [E01])
    adapter.add_torrent_selected(TORRENT, str(tmp_path), H, [E01])
    # qBittorrent refuses the repeat itself (TOW accepts a repeated add only while it is pending).
    with pytest.raises(ClientError, match="«Fails.»" if isinstance(server, FakeQbit) else "уже есть"):
        adapter.add_torrent_selected(TORRENT, str(tmp_path), H, [E01])


def test_selection_change_and_stop(client, tmp_path):
    adapter, server = client
    adapter.add_torrent_selected(TORRENT, str(tmp_path), H, [E01])
    info = adapter.configure_torrent_selection(TORRENT, H, [E01, NFO])
    assert _wanted(info) == [index in (E01, NFO) for index in range(3)]
    assert server.torrents[K].running  # it was running, so it runs again
    stopped = adapter.stop_owned_torrent(H)
    assert stopped["state"] == "stoppedDL"
    assert adapter.stop_owned_torrent(H)["state"] == "stoppedDL"  # idempotent


def test_ownership_checks_read_tags_without_file_lists(client, tmp_path, monkeypatch):
    adapter, server = client
    adapter.add_torrent_selected(TORRENT, str(tmp_path), H, [E01])
    full_reads = []
    original = adapter.inspect_torrent
    monkeypatch.setattr(adapter, "inspect_torrent", lambda infohash: full_reads.append(infohash) or original(infohash))
    assert adapter.set_location(H, str(tmp_path / "moved")) == "ok"
    assert full_reads == [], "an ownership check must not read the whole file list"
    server.torrents[K].labels = ["manual"]
    with pytest.raises(ClientError):
        adapter.set_location(H, str(tmp_path / "again"))
    assert server.torrents[K].path == str(tmp_path / "moved")
    server.torrents[K].labels = ["tow"]
    full_reads.clear()
    adapter.configure_torrent_selection(TORRENT, H, [E01, NFO])
    # The selection's own read-backs only: before, after the change, and the state polls.
    assert len(full_reads) <= 5, full_reads


def test_location_change(client, tmp_path):
    adapter, server = client
    adapter.add_torrent_selected(TORRENT, str(tmp_path), H, [E01])
    assert adapter.set_location(H, str(tmp_path / "moved")) == "ok"
    assert server.torrents[K].path == str(tmp_path / "moved")


@pytest.mark.parametrize("malformed", ["missing", "short"])
def test_missing_file_flags_are_unknown_and_refuse_selection(client, tmp_path, monkeypatch, malformed):
    adapter, server = client
    adapter.add_torrent_selected(TORRENT, str(tmp_path), H, [E01])
    if isinstance(server, FakeTransmission):
        original = server._row

        def row(torrent):
            result = original(torrent)
            result["fileStats"] = [] if malformed == "missing" else result["fileStats"][:1]
            return result

        monkeypatch.setattr(server, "_row", row)
    elif isinstance(server, FakeQbit):
        original = server.torrents_files

        def file_rows(**kwargs):
            rows = original(**kwargs)
            for row in rows if malformed == "missing" else rows[1:]:
                row["priority"] = None
            return rows

        monkeypatch.setattr(server, "torrents_files", file_rows)
    else:
        original = server.m_core_get_torrent_status

        def status(*args):
            result = original(*args)
            result["file_priorities"] = [] if malformed == "missing" else result["file_priorities"][:1]
            return result

        monkeypatch.setattr(server, "m_core_get_torrent_status", status)
    info = adapter.inspect_torrent(H)
    assert info["files"][-1]["priority"] is None
    with pytest.raises(ClientError) as error:
        adapter.configure_torrent_selection(TORRENT, H, [E02])
    assert error.value.code == "client.managed.wrong_selection"
    assert server.torrents[K].running


def test_foreign_torrents_are_never_touched(client, tmp_path):
    adapter, server = client
    server.torrents[K] = Torrent(TORRENT, str(tmp_path), [], paused=False)
    with pytest.raises(ClientError, match="не через TOW"):
        adapter.stop_owned_torrent(H)
    with pytest.raises(ClientError, match="не через TOW"):
        adapter.configure_torrent_selection(TORRENT, H, [E01])
    with pytest.raises(ClientError, match="не через TOW"):
        adapter.set_location(H, str(tmp_path / "moved"))
    assert server.torrents[K].running
    assert server.torrents[K].wanted == [True, True, True]


def test_pending_add_is_finished_by_a_selection_update(client, tmp_path):
    adapter, server = client
    labels = ["tow-pending"] if isinstance(server, FakeDeluge) else ["tow", "tow-pending"]
    if isinstance(server, FakeDeluge):
        server.connected, server.attached_to = True, "host-1"
        server.plugins.append("Label")
        server.labels.update({"tow", "tow-pending"})
        server.logged_in = True
    server.torrents[K] = Torrent(TORRENT, str(tmp_path), labels, paused=True)
    info = adapter.configure_torrent_selection(TORRENT, H, [E02])
    assert info["tags"] == ["tow"]
    assert info["state"] == "downloading"


def test_wrong_save_path_read_back_stops_the_add(client, tmp_path, monkeypatch):
    adapter, server = client
    original = adapter.inspect_torrent

    def elsewhere(infohash):
        info = original(infohash)
        return {**info, "save_path": "D:/other"} if info else info

    monkeypatch.setattr(adapter, "inspect_torrent", elsewhere)
    with pytest.raises(ClientError, match="не в ту папку"):
        adapter.add_torrent_selected(TORRENT, str(tmp_path), H, [E01])
    assert not server.torrents[K].running


def test_selection_read_back_mismatch_is_an_error(client, tmp_path, monkeypatch):
    adapter, server = client
    monkeypatch.setattr(adapter, "_set_wanted", lambda *_a: None)  # the client ignores the request
    with pytest.raises(ClientError, match="не тот выбор"):
        adapter.add_torrent_selected(TORRENT, str(tmp_path), H, [E01])
    assert not server.torrents[K].running


@pytest.mark.parametrize("state", ["stoppedDL", "pausedDL"])
@pytest.mark.parametrize(
    "progress",
    [
        None,
        True,
        False,
        -1,
        0.9,
        1.1,
        float("inf"),
        float("-inf"),
        float("nan"),
        "bad",
        [],
        {},
        [1],
        {"value": 1},
        10**1000,
    ],
)
def test_stopped_invalid_progress_never_confirms_start(client, tmp_path, monkeypatch, state, progress):
    adapter, server = client
    server.torrents[K] = Torrent(TORRENT, str(tmp_path), ["tow"], paused=True)
    _overlay(adapter, monkeypatch, state=state, progress=progress)
    with pytest.raises(ClientError) as error:
        adapter._wait_started(H)
    assert error.value.code == "client.managed.start_unconfirmed"


@pytest.mark.parametrize("state", ["stoppedUP", "stoppedDL", "pausedDL"])
@pytest.mark.parametrize("progress", [1, 1.0, "1.0"])
def test_completed_stopped_torrent_can_confirm_start(client, tmp_path, monkeypatch, state, progress):
    adapter, server = client
    server.torrents[K] = Torrent(TORRENT, str(tmp_path), ["tow"], paused=True)
    _overlay(adapter, monkeypatch, state=state, progress=progress)
    info = adapter._wait_started(H)
    assert (info["state"], info["progress"]) == (state, progress)


@pytest.mark.parametrize("state", ["error", "missingFiles", "unknown"])
def test_ownership_wait_preserves_unsafe_state(client, tmp_path, monkeypatch, state):
    adapter, _ = client
    _overlay(adapter, monkeypatch, state=state)  # the add lands in a state TOW must not go on from
    with pytest.raises(ClientError) as error:
        adapter.add_torrent_selected(TORRENT, str(tmp_path), H, [E01])
    assert error.value.code == "client.managed.error_state"
    assert error.value.params["state"] == state.casefold()


def test_rollback_requires_confirmed_stop_before_restoring_files(client, tmp_path, monkeypatch, caplog):
    adapter, server = client
    adapter.add_torrent_selected(TORRENT, str(tmp_path), H, [E01])  # running, E01 only
    torrent = server.torrents[K]
    real_stop = adapter._stop
    steps: list[str] = []

    def stop(infohash):
        if steps:
            steps.append("stop ignored")
            return
        steps.append("stop")
        real_stop(infohash)

    def set_wanted(_infohash, _wanted, _ids):
        steps.append("selection ignored")
        torrent.running = True  # ... and the client started the torrent again

    monkeypatch.setattr(adapter, "_stop", stop)
    monkeypatch.setattr(adapter, "_set_wanted", set_wanted)
    server.calls.clear()
    with pytest.raises(ClientError) as error:
        adapter.configure_torrent_selection(TORRENT, H, [E02])
    assert error.value.code == "client.managed.wrong_selection"
    assert steps == ["stop", "selection ignored", "stop ignored"]
    assert not WRITES.intersection(server.calls), server.calls  # no files restored, no start
    assert torrent.wanted == [index == E01 for index in range(3)]
    assert str(adapter._fail("client.managed.stop_unconfirmed")) in caplog.text


# --- client specifics -----------------------------------------------------------------------


def test_transmission_errors_are_plain():
    server = FakeTransmission()
    assert _transmission(server).ping() == "4.1.3 rpc 19"
    with pytest.raises(ClientError, match="неверный логин или пароль"):
        _transmission(server, "wrong").ping()
    with pytest.raises(ClientError, match=r"Transmission 3\.0 или новее"):
        _transmission(FakeTransmission(rpc_version=15)).ping()

    def unreachable(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(ClientError, match="нет связи"):
        TransmissionClient("127.0.0.1", 9091, transport=httpx.MockTransport(unreachable)).ping()
    for status, text in ((403, "rpc-whitelist"), (421, "rpc-host-whitelist"), (404, "нет RPC"), (500, "ошибка 500")):
        transport = httpx.MockTransport(lambda _r, s=status: httpx.Response(s))
        with pytest.raises(ClientError, match=text) as refused:
            TransmissionClient("127.0.0.1", 9091, transport=transport).ping()
        assert refused.value.error_class == "qbit"
        assert refused.value.text("en").startswith("Transmission: ")


def test_transmission_category_and_tags_become_labels(tmp_path):
    server = FakeTransmission()
    adapter = _transmission(server)
    adapter.add_category = "serials"
    adapter.add_tags = ["kids", "tow"]
    adapter.add_torrent_selected(TORRENT, str(tmp_path), H, [E01])
    assert sorted(server.torrents[K].labels) == ["kids", "serials", "tow"]


def test_transmission_does_not_fetch_magnets():
    adapter = _transmission(FakeTransmission())
    assert adapter.capabilities["magnet_metadata"] is False
    with pytest.raises(ClientError, match=r"нужен \.torrent"):
        adapter.materialize_magnet(f"magnet:?xt=urn:btih:{H}", "D:/x", H)


@pytest.mark.parametrize(
    ("host", "port", "expected"),
    [
        ("127.0.0.1", 9091, "http://127.0.0.1:9091"),
        ("https://nas.local", 443, "https://nas.local:443"),
        ("http://nas:9999/", 9091, "http://nas:9999"),
        # An IPv6 address: its colons are not a port.
        ("[fd00::5]", 9091, "http://[fd00::5]:9091"),
        ("[::1]", 8112, "http://[::1]:8112"),
        ("http://[fd00::5]", 9091, "http://[fd00::5]:9091"),
        ("[fd00::5]:9999", 9091, "http://[fd00::5]:9999"),
        ("fd00::5", 9091, "http://[fd00::5]:9091"),
        ("nas:9091/transmission/rpc", 1, "http://nas:9091/transmission/rpc"),
    ],
)
def test_base_url(host, port, expected):
    assert base_url(host, port) == expected


def test_deluge_check_changes_nothing_and_adding_turns_on_labels(tmp_path):
    server = FakeDeluge()
    adapter = _deluge(server)
    assert adapter.ping() == "2.2.0 libtorrent 2.1.1.0"
    assert server.plugins == []  # «Проверить» and dry runs never change the client
    server.logged_in = False  # the web session expired
    assert adapter.ping() == "2.2.0 libtorrent 2.1.1.0"
    adapter.add_torrent_selected(TORRENT, str(tmp_path), H, [E01])
    assert server.plugins == ["Label"]
    assert server.labels == {"tow", "tow-pending"}


def test_deluge_reattaches_when_its_web_ui_does_not_know_a_method(tmp_path):
    """Deluge Web learns the daemon's methods when it attaches; the Label plugin TOW turned on is
    announced by an event it may miss. A new login does not refresh the list, attaching does."""
    server = FakeDeluge()
    server.misses_plugin_event = True
    adapter = _deluge(server)
    adapter.ping()
    adapter.add_torrent_selected(TORRENT, str(tmp_path), H, [E01])
    assert server.labels == {"tow", "tow-pending"}
    assert server.calls.count("web.disconnect") == 1
    assert server.calls.count("web.connect") == 2


def test_deluge_reattaches_to_the_daemon_it_was_attached_to(tmp_path):
    """With several daemons the new attach took the first one online: TOW then added to
    (and read) another daemon's torrents than the one the owner had attached."""
    server = FakeDeluge()
    server.misses_plugin_event = True
    server.hosts = ["host-1", "host-2"]
    server.connected, server.attached_to = True, "host-2"  # attached by the owner
    adapter = _deluge(server)
    adapter.ping()
    adapter.add_torrent_selected(TORRENT, str(tmp_path), H, [E01])
    assert server.labels == {"tow", "tow-pending"}
    assert server.calls.count("web.disconnect") == 1
    assert server.attached_to == "host-2"


def test_deluge_stays_attached_when_it_cannot_tell_to_which_daemon(tmp_path):
    """No daemon says "Connected": attaching again could pick another one, so it is not detached."""
    server = FakeDeluge()
    server.misses_plugin_event = True
    server.hosts = ["host-1", "host-2"]
    server.connected, server.attached_to = True, "host-2"
    server.reports_connected = False
    adapter = _deluge(server)
    adapter.ping()
    with pytest.raises(ClientError):
        adapter.add_torrent_selected(TORRENT, str(tmp_path), H, [E01])
    assert "web.disconnect" not in server.calls
    assert server.attached_to == "host-2"


def test_deluge_in_a_preview_does_not_attach_the_web_ui_to_a_daemon():
    server = FakeDeluge()
    adapter = _deluge(server)
    adapter.read_only = True
    with pytest.raises(ClientError, match="предпросмотр его не подключает"):
        adapter.ping()
    assert "web.connect" not in server.calls
    assert server.connected is False
    server.connected = True  # attached already (by the owner, or a real check): a preview reads
    assert adapter.ping() == "2.2.0 libtorrent 2.1.1.0"


def test_a_dry_run_opens_clients_read_only(monkeypatch):
    from tow import check
    from tow.clients import factory as client_factory
    from tow.store import save_state

    # A paused topic: the run has something to check, so it opens the client (not the site).
    save_state({"topics": [{"id": "t1", "title": "Show", "url": "http://rutor.info/torrent/1", "paused": True}]})
    server = FakeDeluge()
    monkeypatch.setattr(client_factory, "default_client_id", lambda _cfg: "main")
    monkeypatch.setattr(client_factory, "from_secrets", lambda _cfg, _secrets, _id=None: _deluge(server))
    out = check.run_check(apply=False, notify=False)
    assert out["qbit"].startswith("down: Deluge")
    assert "web.connect" not in server.calls
    check.run_check(apply=True, notify=False)
    assert "web.connect" in server.calls  # a real check may attach it


def test_deluge_errors_are_plain():
    with pytest.raises(ClientError, match="неверный пароль"):
        _deluge(FakeDeluge(), "wrong").ping()
    with pytest.raises(ClientError, match="не видит демон"):
        _deluge(FakeDeluge(host_online=False)).ping()


def test_deluge_fetches_magnet_metadata_without_adding():
    server = FakeDeluge()
    server.prefetch = (H, base64.b64encode(bencode(INFO)).decode())
    adapter = _deluge(server)
    content = adapter.materialize_magnet(f"magnet:?xt=urn:btih:{H}&dn=x", None, H)
    assert parse_torrent_metadata(content).client_hash == H
    assert server.torrents == {}
    server.prefetch = None
    with pytest.raises(ClientError, match="нет пиров"):
        adapter.materialize_magnet(f"magnet:?xt=urn:btih:{H}", None, H)
    with pytest.raises(ClientError, match="не совпадает"):
        adapter.materialize_magnet(f"magnet:?xt=urn:btih:{'0' * 40}", None, H)
    other = {**INFO, b"name": "Other"}
    server.prefetch = (H, base64.b64encode(bencode(other)).decode())
    with pytest.raises(ClientError, match="не совпадают с её хешем"):
        adapter.materialize_magnet(f"magnet:?xt=urn:btih:{H}", None, H)


def test_deluge_magnet_keeps_the_trackers():
    server = FakeDeluge()
    server.prefetch = (H, base64.b64encode(bencode(INFO)).decode())
    magnet = (
        f"magnet:?xt=urn:btih:{H}&tr=http%3A%2F%2Fbt.private.example%2Fann%3Fpk%3Dx"
        "&tr=udp%3A%2F%2Fopen.example%3A80&tr=javascript%3Ax"
    )
    content = _deluge(server).materialize_magnet(magnet, None, H)
    meta = parse_torrent_metadata(content)
    assert meta.client_hash == H
    assert content.startswith(b"d8:announce34:http://bt.private.example/ann?pk=x13:announce-listll")
    assert b"udp://open.example:80" in content
    assert b"javascript" not in content


def test_deluge_native_preview_only_reads_attached_daemon_and_prefetches():
    server = FakeDeluge()
    server.connected = True
    server.prefetch = (H, base64.b64encode(bencode(INFO)).decode())
    assert _deluge(server).preview_magnet(f"magnet:?xt=urn:btih:{H}")
    assert server.calls == ["auth.login", "web.connected", "core.prefetch_magnet_metadata"]
    assert not server.torrents
    assert not server.plugins
    assert not server.labels


def test_deluge_native_preview_never_attaches_to_another_daemon():
    server = FakeDeluge()
    with pytest.raises(ClientError) as error:
        _deluge(server).preview_magnet(f"magnet:?xt=urn:btih:{H}")
    assert error.value.code == "client.deluge.not_attached_preview"
    assert server.calls == ["auth.login", "web.connected"]


def test_deluge_native_error_cannot_echo_a_tracker_passkey(monkeypatch):
    server = FakeDeluge()
    server.connected = True
    adapter = _deluge(server)
    monkeypatch.setattr(
        adapter,
        "materialize_magnet",
        lambda *_args: (_ for _ in ()).throw(
            ClientError("client.managed.rpc_refused", method="prefetch", answer="passkey=private")
        ),
    )
    with pytest.raises(ClientError) as error:
        adapter.preview_magnet(f"magnet:?xt=urn:btih:{H}")
    assert error.value.code == "content.magnet_failed"
    assert "private" not in str(error.value)
    assert adapter._metadata_preview is False


@pytest.mark.parametrize("encoded", ["@@@", "a", "é", 3, ["text"]])
def test_deluge_metadata_requires_strict_base64(encoded):
    server = FakeDeluge()
    server.connected = True
    server.prefetch = (H, encoded)
    with pytest.raises(ClientError) as error:
        _deluge(server).preview_magnet(f"magnet:?xt=urn:btih:{H}")
    assert error.value.code in {"content.magnet_failed", "content.too_large"}
    assert server.torrents == {}


def test_deluge_metadata_size_is_bounded_before_decoding(monkeypatch):
    from tow.clients import deluge

    server = FakeDeluge()
    server.connected = True
    server.prefetch = (H, base64.b64encode(bencode(INFO)).decode())
    monkeypatch.setattr(deluge, "MAX_TORRENT_BYTES", 3)
    monkeypatch.setattr(deluge.base64, "b64decode", lambda *_args, **_kwargs: pytest.fail("oversized decode"))
    with pytest.raises(ClientError) as error:
        _deluge(server).preview_magnet(f"magnet:?xt=urn:btih:{H}")
    assert error.value.code == "content.too_large"


def test_deluge_preview_refuses_wrong_v2_hash_in_hybrid_magnet():
    server = FakeDeluge()
    server.connected = True
    server.prefetch = (H, base64.b64encode(bencode(INFO)).decode())
    with pytest.raises(ClientError) as error:
        _deluge(server).preview_magnet(f"magnet:?xt=urn:btih:{H}&xt=urn:btmh:1220{'a' * 64}")
    assert error.value.code == "client.deluge.magnet_data_mismatch"


def test_deluge_dry_run_never_prefetches():
    server = FakeDeluge()
    adapter = _deluge(server)
    adapter.read_only = True
    with pytest.raises(ClientError) as error:
        adapter.preview_magnet(f"magnet:?xt=urn:btih:{H}")
    assert error.value.code == "content.magnet_unsupported"
    assert server.calls == []


def test_transmission_refuses_native_preview_without_any_request():
    server = FakeTransmission()
    adapter = _transmission(server)
    assert adapter.capabilities["metadata_preview"] is False
    with pytest.raises(ClientError) as error:
        adapter.preview_magnet(f"magnet:?xt=urn:btih:{H}")
    assert error.value.code == "content.magnet_unsupported"
    assert server.calls == []


class HybridTransmission(FakeTransmission):
    """Lists every torrent under another hash, as clients do for hybrid v1+v2 torrents."""

    def __init__(self, alias: str) -> None:
        super().__init__()
        self.alias = alias.lower()

    def handler(self, request):
        response = super().handler(request)
        for key, torrent in list(self.torrents.items()):
            if key != self.alias:
                torrent.hash = self.alias
                self.torrents = {self.alias: torrent}
        return response


def test_a_hybrid_torrent_listed_under_another_hash_is_followed(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from tow.clients import managed

    alias = "A" * 40
    real = managed.parse_torrent_metadata

    def hybrid(content):
        meta = real(content)
        return SimpleNamespace(
            name=meta.name, files=meta.files, client_hash=meta.client_hash, hash_v1=alias, hash_v2=None
        )

    monkeypatch.setattr(managed, "parse_torrent_metadata", hybrid)
    server = HybridTransmission(alias)
    info = _transmission(server).add_torrent_selected(TORRENT, str(tmp_path), H, [E01])
    assert info["hash"].upper() == alias  # the hash the client really uses goes back to the check
    assert info["tags"] == ["tow"]


def test_free_space_and_file_checks_of_a_remote_client(monkeypatch):
    from tow.check import free_space_problem
    from tow.progress import filesystem_confirmation
    from tow.torrent import TorrentFile

    files = (TorrentFile(0, "big.mkv", 10**15),)
    assert free_space_problem("/downloads/tv", files, [0]) is None  # a remote client: not judged by C:
    assert free_space_problem("Q:\\no-such-drive\\tv", files, [0]) is None
    assert filesystem_confirmation("/downloads/tv", "big.mkv", 1) is None  # unverified, not "missing"


# --- an add whose marking step fails never leaves an unmarked torrent behind silently ------


class NoLabelDeluge(FakeDeluge):
    def m_core_enable_plugin(self, name):
        return False  # a build without the plugin: Deluge answers False and enables nothing


def test_deluge_without_its_label_plugin_adds_nothing(tmp_path):
    server = NoLabelDeluge()
    with pytest.raises(ClientError) as raised:
        _deluge(server).add_torrent_selected(TORRENT, str(tmp_path), H, [E01])
    assert raised.value.code == "client.deluge.no_label_plugin"
    assert server.torrents == {}


class OldTransmission(FakeTransmission):
    """Transmission 3.0 (RPC 16): labels are ignored on torrent-add; setting them may time out."""

    def __init__(self, label_failures: int) -> None:
        super().__init__(rpc_version=16)
        self.label_failures = label_failures

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        if body.get("method") == "torrent-add":
            body["arguments"].pop("labels", None)
            request = httpx.Request("POST", request.url, headers=request.headers, json=body)
        if body.get("method") == "torrent-set" and "labels" in body.get("arguments", {}) and self.label_failures:
            self.label_failures -= 1
            raise httpx.ReadTimeout("slow daemon")
        return super().handler(request)


def test_transmission_label_timeout_after_the_add_is_marked_again(tmp_path):
    server = OldTransmission(label_failures=1)
    _transmission(server).add_torrent_selected(TORRENT, str(tmp_path), H, [E01])
    assert server.torrents[K].labels == ["tow"]
    assert server.torrents[K].running


def test_torrent_that_cannot_be_marked_is_reported_not_left_silently(tmp_path):
    server = OldTransmission(label_failures=5)
    with pytest.raises(ClientError) as raised:
        _transmission(server).add_torrent_selected(TORRENT, str(tmp_path), H, [E01])
    assert raised.value.code == "client.managed.added_unmarked"
    torrent = server.torrents[K]
    assert (torrent.running, torrent.labels) == (False, [])  # paused, and the owner is told


def test_deluge_answer_that_is_not_deluges_is_an_error_not_a_removed_torrent():
    server = FakeDeluge()
    adapter = _deluge(server)
    adapter.inspect_torrent(H)  # log in and attach
    real = server.handler

    def proxy_glitch(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content)["method"] == "core.get_torrent_status":
            return httpx.Response(200, content=b"null")
        return real(request)

    adapter._http = httpx.Client(transport=httpx.MockTransport(proxy_glitch))
    with pytest.raises(ClientError) as raised:
        adapter.has_hash(H)
    assert raised.value.code == "client.deluge.not_web"


@pytest.mark.parametrize("make", [lambda: (FakeTransmission(), _transmission), lambda: (FakeDeluge(), _deluge)])
def test_clients_say_whether_they_list_any_torrent(tmp_path, make):
    server, adapter_of = make()
    adapter = adapter_of(server)
    assert adapter.has_any_torrent() is False
    adapter.add_torrent_selected(TORRENT, str(tmp_path), H, [E01])
    assert adapter.has_any_torrent() is True
