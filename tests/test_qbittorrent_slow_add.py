"""A qBittorrent add that answers slowly: the library sends it again, qBittorrent refuses the
repeat as a duplicate - and the torrent the first request added is TOW's add, not a refusal.

The real qbittorrent-api client talks to a fake Web UI behind ``requests`` (no network)."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from helpers import make_torrent

from tow.clients import qbittorrent
from tow.clients.managed import ClientError
from tow.torrent import parse_torrent_metadata

TORRENT = make_torrent({b"length": 1, b"name": b"e01.mkv", b"piece length": 16384, b"pieces": b"x" * 20})
H = parse_torrent_metadata(TORRENT).client_hash
K = H.lower()


class FakeWebUI:
    def __init__(self, *, first_add_times_out: bool) -> None:
        self.adds = 0
        self.timeouts: list[Any] = []
        self.first_add_times_out = first_add_times_out
        self.torrents: dict[str, dict[str, Any]] = {}

    def _resp(self, request: requests.PreparedRequest, body: Any, status: int = 200) -> requests.Response:
        response = requests.Response()
        response.status_code = status
        response._content = (body if isinstance(body, str) else json.dumps(body)).encode()
        response.headers["Content-Type"] = "text/plain" if isinstance(body, str) else "application/json"
        response.url = request.url or ""
        response.request = request
        response.encoding = "utf-8"
        return response

    def send(self, _adapter: Any, request: requests.PreparedRequest, **kwargs: Any) -> requests.Response:
        path = urlsplit(request.url or "").path
        query = parse_qs(urlsplit(request.url or "").query)
        if path.endswith("/auth/login"):
            return self._resp(request, "Ok.")
        if path.endswith("/app/webapiVersion"):
            return self._resp(request, "2.11.4")
        if path.endswith("/app/version"):
            return self._resp(request, "v5.0.4")
        if path.endswith("/torrents/add"):
            self.adds += 1
            self.timeouts.append(kwargs.get("timeout"))
            if K in self.torrents:
                return self._resp(request, "Fails.")  # a duplicate (WebAPI before 2.14)
            self.torrents[K] = {"hash": K, "tags": "tow, tow-pending", "state": "stoppedDL", "save_path": "x"}
            if self.first_add_times_out and self.adds == 1:
                raise requests.exceptions.ReadTimeout("qBittorrent busy reading a large .torrent")
            return self._resp(request, "Ok.")
        if path.endswith("/torrents/info"):
            wanted = (query.get("hashes") or [""])[0].lower()
            rows = [t for h, t in self.torrents.items() if not wanted or h == wanted]
            return self._resp(request, rows[: int((query.get("limit") or [len(rows)])[0])])
        if path.endswith("/torrents/files"):
            return self._resp(request, [])
        return self._resp(request, "")


@pytest.fixture
def webui(monkeypatch):
    def install(**options: Any) -> FakeWebUI:
        fake = FakeWebUI(**options)
        monkeypatch.setattr(
            requests.adapters.HTTPAdapter, "send", lambda adapter, request, **kw: fake.send(adapter, request, **kw)
        )
        monkeypatch.setattr("qbittorrentapi.request.sleep", lambda *_: None)
        monkeypatch.setattr(qbittorrent.time, "sleep", lambda *_: None)
        return fake

    return install


def test_add_gets_a_longer_timeout_than_other_requests(webui, tmp_path):
    fake = webui(first_add_times_out=False)
    client = qbittorrent.QBittorrentClient("127.0.0.1", 18999, "u", "p")
    with pytest.raises(ClientError):  # the fake cannot finish the add; only the request matters here
        client.add_torrent_selected(TORRENT, str(tmp_path), H, [0])
    assert fake.timeouts == [qbittorrent.ADD_TIMEOUT_SEC]


def test_slow_add_repeated_by_the_library_is_not_reported_as_refused(webui, tmp_path):
    fake = webui(first_add_times_out=True)
    client = qbittorrent.QBittorrentClient("127.0.0.1", 18999, "u", "p")
    with pytest.raises(ClientError) as raised:
        client.add_torrent_selected(TORRENT, str(tmp_path), H, [0])
    assert fake.adds == 2  # the library's own repeat
    # Not "the client refused it": the add went on to its read-back, where the fake's folder
    # ("x") is the next thing to differ.
    assert raised.value.code == "client.managed.wrong_folder"


def test_any_torrent_is_asked_with_a_single_row(webui):
    fake = webui(first_add_times_out=False)
    client = qbittorrent.QBittorrentClient("127.0.0.1", 18999, "u", "p")
    assert client.has_any_torrent() is False
    fake.torrents = {"a" * 40: {"hash": "a" * 40}, "b" * 40: {"hash": "b" * 40}}
    assert client.has_any_torrent() is True
