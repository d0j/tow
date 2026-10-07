"""The check pipeline end to end with a fake site and fake torrent clients (synthetic data):
what it adds, when it reports, and what it leaves to the next check."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

from tow import check
from tow.store import load_state, save_download_history, save_state
from tow.torrent import TorrentFile

OLD, NEW = "A" * 40, "B" * 40


class Tracker:
    name = "fake"

    def __init__(self, during_fetch: Any = None) -> None:
        self.during_fetch = during_fetch

    def fetch_torrent(self, url, secrets, ua, *, ignore_cool=False, persist=True):
        if self.during_fetch:
            self.during_fetch()
        return b"torrent"

    def fetch_title(self, url, secrets, ua, *, ignore_cool=False, persist=True):
        return "Show"


class Client:
    """A client that keeps torrents per hash."""

    client_kind = "fake"
    capabilities: ClassVar[dict[str, bool]] = {
        "inspect": True,
        "add": True,
        "stopped_add": True,
        "file_selection": True,
        "priority_readback": True,
        "start_stop": True,
    }

    def __init__(self, client_id: str = "main") -> None:
        self.client_id = client_id
        self.torrents: dict[str, dict[str, Any]] = {}
        self.adds: list[tuple[str, str]] = []

    def ping(self) -> str:
        return "ok"

    def has_hash(self, h):
        return h in self.torrents

    def inspect_torrent(self, h):
        t = self.torrents.get(h)
        return None if t is None else {**t, "files": [dict(f) for f in t["files"]]}

    def put(self, h: str, save_path: str, *, tags: list[str]) -> None:
        self.torrents[h] = {
            "hash": h,
            "save_path": save_path,
            "state": "stoppedDL",
            "tags": tags,
            "files": [{"index": 0, "name": "Show/e01.mkv", "size": 1, "priority": 1}],
        }

    def add_torrent_selected(self, content, save_path, h, selected):
        self.adds.append((h, save_path))
        self.put(h, save_path, tags=["tow"])
        return {"hash": h}

    def configure_torrent_selection(self, content, h, selected, *, ensure_started=False):
        return self.inspect_torrent(h)


def _wire(monkeypatch, clients: dict[str, Client], tracker: Any) -> None:
    cfg = {"trackers": {}, "client": {"id": "main", "kind": "fake"}}
    monkeypatch.setattr(check, "load_config", lambda: cfg)
    monkeypatch.setattr(check, "load_trackers", lambda cfg: {"fake": tracker})
    monkeypatch.setattr(check, "match_tracker", lambda trackers, url: tracker)
    monkeypatch.setattr(
        check,
        "parse_torrent_metadata",
        lambda blob: SimpleNamespace(
            infohash=NEW, client_hash=NEW, name="Show", is_multi=True, files=(TorrentFile(0, "Show/e01.mkv", 1),)
        ),
    )
    monkeypatch.setattr(check.client_factory, "default_client_id", lambda cfg: "main")
    monkeypatch.setattr(
        check.client_factory, "from_secrets", lambda cfg, secrets, client_id=None: clients[client_id or "main"]
    )
    monkeypatch.setattr(check, "free_space_problem", lambda *a, **k: None)
    monkeypatch.setattr(check, "reconcile_topic", lambda *a, **k: {"events": [], "summary": {}})


def _topic(**extra: Any) -> dict[str, Any]:
    return {"id": "t1", "title": "Show", "url": "https://tracker.example/1", "save_path": "/media/tv", **extra}


@pytest.fixture
def stores():
    save_download_history({"schema_version": 1, "topics": {}})


# --- a torrent without TOW's mark is never green ----------------------------------------------


def test_torrent_in_the_client_without_the_tow_mark_is_red(monkeypatch, stores):
    """E.g. TOW's own add whose marking failed (left paused, no label): "already in the client"
    was green, although TOW could never manage it."""
    save_state({"topics": [_topic(hash=OLD)]})
    client = Client()
    client.put(NEW, "/media/tv", tags=[])
    _wire(monkeypatch, {"main": client}, Tracker())
    row = check.run_check(apply=True, notify=False, how="test")["results"][0]
    saved = load_state()["topics"][0]
    assert row["ok"] is False
    assert saved["last_error_code"] == "check.not_owned_existing"
    assert saved["last_error_class"] == "qbit"
    assert client.adds == []


def test_torrent_with_the_tow_mark_already_there_is_accepted(monkeypatch, stores):
    save_state({"topics": [_topic(hash=OLD)]})
    client = Client()
    client.put(NEW, "/media/tv", tags=["tow"])
    _wire(monkeypatch, {"main": client}, Tracker())
    row = check.run_check(apply=True, notify=False, how="test")["results"][0]
    assert row["ok"] is True
    assert load_state()["topics"][0]["hash"] == NEW
    assert not load_state()["topics"][0].get("last_error")
