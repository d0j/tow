"""Revision identity checks obey the same session/cooldown policy as torrent checks."""

from __future__ import annotations

import time

import httpx
import pytest

from tow import http as thttp
from tow.config import load_config, save_config
from tow.errors import TowError
from tow.store import load_state, save_state
from tow.trackers.generic import GenericHttpTracker


@pytest.mark.parametrize("persist", [True, False])
def test_revision_all_fetch_mirrors_cooling_never_logs_in_at_other_host(monkeypatch, persist):
    fetch_host = "https://fetch.example"
    login_host = "https://login.example"
    spec = {
        "url_regex": r"^https://fetch\.example/topic/(\d+)$",
        "fetch_hosts": [fetch_host],
        "login_hosts": [login_host],
        "login_path": "/login",
        "infohash_path": "/identity/{id}",
    }
    cfg = load_config()
    cfg["trackers"] = {"sample": spec}
    save_config(cfg)
    state = load_state()
    state.setdefault("mirrors", {})["sample"] = {"cool": {fetch_host: time.time() + 3600}, "fail": {}, "active": None}
    save_state(state)
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "/", "set-cookie": "session=synthetic"})

    monkeypatch.setattr(thttp, "client", lambda **kw: httpx.Client(transport=httpx.MockTransport(handler)))
    secrets = {"trackers": {"sample": {"username": "synthetic-user", "password": "synthetic-password"}}}
    tracker = GenericHttpTracker("sample", spec)
    with pytest.raises(TowError) as caught:
        tracker.fetch_revision_hash(fetch_host + "/topic/1234567", secrets, None, persist=persist)
    assert caught.value.code == "mirrors.all_paused"
    assert requests == []
    assert "login_at" not in load_state()["mirrors"]["sample"]


def test_revision_available_fetch_host_still_allows_normal_login(monkeypatch):
    spec = {
        "url_regex": r"^https://fetch\.example/topic/(\d+)$",
        "fetch_hosts": ["https://fetch.example"],
        "login_hosts": ["https://login.example"],
        "login_path": "/login",
        "infohash_path": "/identity/{id}",
    }
    cfg = load_config()
    cfg["trackers"] = {"sample": spec}
    save_config(cfg)
    requests = []

    def handler(request):
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(302, headers={"location": "/", "set-cookie": "session=synthetic"})
        return httpx.Response(200, text="info hash: " + "a" * 40)

    monkeypatch.setattr(thttp, "client", lambda **kw: httpx.Client(transport=httpx.MockTransport(handler)))
    secrets = {"trackers": {"sample": {"username": "synthetic-user", "password": "synthetic-password"}}}
    assert (
        GenericHttpTracker("sample", spec).fetch_revision_hash("https://fetch.example/topic/1234567", secrets, None)
        == "A" * 40
    )
    assert [request.method for request in requests] == ["POST", "GET"]
