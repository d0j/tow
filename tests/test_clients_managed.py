"""Transmission and Deluge adapters against stateful fake servers (no real client, no network).

The same contract was checked once against live transmission-daemon 4.1.3 and Deluge 2.2.0;
these tests keep it from regressing.
"""

from __future__ import annotations

import base64
import json
import math
from typing import Any

import httpx
import pytest
from helpers import bencode, make_torrent

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
            result = {"torrents": [self._row(t) for t in rows]}
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
        if method.startswith("label.") and "Label" not in self.plugins:
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
        return [["host-1", "127.0.0.1", 58846, "localclient"]]

    def m_web_get_host_status(self, host_id):
        return [host_id, "Online" if self.host_online else "Offline", "2.2.0"]

    def m_web_connect(self, host_id):
        self.connected = True
        return []

    def m_daemon_get_version(self):
        return "2.2.0"

    def m_core_get_libtorrent_version(self):
        return "2.1.1.0"

    def m_core_get_enabled_plugins(self):
        return list(self.plugins)

    def m_core_enable_plugin(self, name):
        self.plugins.append(name)
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

    def m_core_get_torrent_status(self, torrent_id, keys):
        t = self.torrents.get(torrent_id)
        if t is None:
            return {}
        return {
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


# --- the shared contract, for both clients --------------------------------------------------


@pytest.fixture(params=["transmission", "deluge"])
def client(request):
    if request.param == "transmission":
        server: Any = FakeTransmission()
        return _transmission(server), server
    server = FakeDeluge()
    return _deluge(server), server


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


def test_duplicate_and_bad_selection_are_refused(client, tmp_path):
    adapter, _ = client
    with pytest.raises(ClientError, match="неверные номера"):
        adapter.add_torrent_selected(TORRENT, str(tmp_path), H, [7])
    with pytest.raises(ClientError, match="не указана папка"):
        adapter.add_torrent_selected(TORRENT, "", H, [E01])
    adapter.add_torrent_selected(TORRENT, str(tmp_path), H, [E01])
    with pytest.raises(ClientError, match="уже есть"):
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


def test_location_change(client, tmp_path):
    adapter, server = client
    adapter.add_torrent_selected(TORRENT, str(tmp_path), H, [E01])
    assert adapter.set_location(H, str(tmp_path / "moved")) == "ok"
    assert server.torrents[K].path == str(tmp_path / "moved")


def test_foreign_torrents_are_never_touched(client, tmp_path):
    adapter, server = client
    server.torrents[K] = Torrent(TORRENT, str(tmp_path), [], paused=False)
    with pytest.raises(ClientError, match="не через TOW"):
        adapter.stop_owned_torrent(H)
    with pytest.raises(ClientError, match="не через TOW"):
        adapter.configure_torrent_selection(TORRENT, H, [E01])
    assert server.torrents[K].running
    assert server.torrents[K].wanted == [True, True, True]


def test_pending_add_is_finished_by_a_selection_update(client, tmp_path):
    adapter, server = client
    labels = ["tow-pending"] if isinstance(server, FakeDeluge) else ["tow", "tow-pending"]
    if isinstance(server, FakeDeluge):
        server.connected = True
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
    from tow.store import save_state

    save_state({"topics": []})
    server = FakeDeluge()
    monkeypatch.setattr(check.client_factory, "default_client_id", lambda _cfg: "main")
    monkeypatch.setattr(check.client_factory, "from_secrets", lambda _cfg, _secrets, _id=None: _deluge(server))
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
