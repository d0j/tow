"""A topic's life as a sequence of checks, with a fake site and a fake torrent client: what the
state stores, what Home shows (the status-colour contract), what History records and what the
messenger says must agree after every step. Synthetic titles, hashes and folders."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

from tow import check, torrent_cache
from tow.check import notices as check_notices
from tow.check import reconcile as check_reconcile
from tow.check import run as check_run
from tow.check import topic as topic_step
from tow.clients import factory as client_factory
from tow.errors import TowError
from tow.log import history_events
from tow.status import project_home_status
from tow.store import load_download_history, load_state, save_download_history, save_state
from tow.torrent import TorrentFile

OLD, NEW = "A" * 40, "B" * 40
FILES = (TorrentFile(0, "Show/e01.mkv", 1024), TorrentFile(1, "Show/e02.mkv", 1024))


class Site:
    """The topic's site: its current revision is the hash it serves as the .torrent."""

    name = "fake"

    def __init__(self) -> None:
        self.current = OLD
        self.fetches = 0
        self.on_fetch: Any = None

    def fetch_torrent(self, url, secrets, ua, *, ignore_cool=False, persist=True):
        self.fetches += 1
        if self.on_fetch is not None:
            self.on_fetch()
        return self.current.encode()

    def fetch_title(self, url, secrets, ua, *, ignore_cool=False, persist=True):
        return "Show [01x01-02 из 2]"


class Client:
    """Torrents per hash with their state, files, priorities and TOW's mark."""

    client_id = "main"
    client_kind = "fake"
    capabilities: ClassVar[dict[str, bool]] = {
        "inspect": True,
        "add": True,
        "stopped_add": True,
        "file_selection": True,
        "priority_readback": True,
        "start_stop": True,
    }

    def __init__(self) -> None:
        self.torrents: dict[str, dict[str, Any]] = {}
        self.adds: list[str] = []
        self.starts_confirmed = True

    def ping(self) -> str:
        return "ok"

    def has_any_torrent(self) -> bool:
        return bool(self.torrents)

    def has_hash(self, h):
        return h in self.torrents

    def inspect_torrent(self, h):
        torrent = self.torrents.get(h)
        return None if torrent is None else {**torrent, "files": [dict(row) for row in torrent["files"]]}

    def put(self, h: str, save_path: str, *, state: str = "uploading", tags=("tow",)) -> None:
        files = [{"index": f.index, "name": f.path, "size": f.size, "progress": 1.0, "priority": 1} for f in FILES]
        self.torrents[h] = {"hash": h, "save_path": save_path, "state": state, "tags": list(tags), "files": files}

    def add_torrent_selected(self, content, save_path, h, selected, *, start=True):
        self.adds.append(h)
        self.put(h, save_path, state="downloading" if start else "stoppedDL")
        for row in self.torrents[h]["files"]:
            row["progress"] = 0.0
            row["priority"] = 1 if row["index"] in set(selected) else 0
        return {"hash": h}

    def configure_torrent_selection(self, content, h, selected, *, ensure_started=False):
        for row in self.torrents[h]["files"]:
            row["priority"] = 1 if row["index"] in set(selected) else 0
        if ensure_started:
            self.torrents[h]["state"] = "downloading"
        return self.inspect_torrent(h)

    def start_owned_torrent(self, h):
        if self.starts_confirmed:
            self.torrents[h]["state"] = "downloading"
        return self.inspect_torrent(h)

    def stop_owned_torrent(self, h):
        self.torrents[h]["state"] = "stoppedUP"
        return self.inspect_torrent(h)


class Reconcile:
    """The client presence part of the progress reconcile, and a failure on request."""

    def __init__(self) -> None:
        self.failure: Exception | None = None
        self.events: list[str] = []

    def __call__(self, topic, client, history, now):
        if self.failure is not None:
            raise self.failure
        record = history.setdefault("topics", {}).setdefault(str(topic["id"]), {"items": {}})
        events, self.events = list(self.events), []
        present = client.inspect_torrent(str(topic.get("hash") or "")) is not None
        if not present and record.get("client_present") is True:
            events.append("client_removed")
        if present and record.get("client_present") is False:
            events.append("client_restored")
        record["client_present"] = present
        return {"events": events, "summary": {}, "completed_episode_label": "S01E02"}


@pytest.fixture
def world(monkeypatch, tmp_path):
    site, client, reconcile = Site(), Client(), Reconcile()
    cfg = {"trackers": {}, "client": {"id": "main", "kind": "fake"}}
    monkeypatch.setattr(check_run, "load_config", lambda: cfg)
    monkeypatch.setattr(check_run, "load_trackers", lambda cfg: {"fake": site})
    monkeypatch.setattr(topic_step, "match_tracker", lambda trackers, url: site)
    monkeypatch.setattr(
        topic_step,
        "parse_torrent_metadata",
        lambda blob: SimpleNamespace(
            infohash=blob.decode(),
            client_hash=blob.decode(),
            hash_v1=blob.decode(),
            hash_v2=None,
            name="Show",
            is_multi=True,
            files=FILES,
        ),
    )
    monkeypatch.setattr(client_factory, "default_client_id", lambda cfg: "main")
    monkeypatch.setattr(client_factory, "from_secrets", lambda cfg, secrets, client_id=None: client)
    monkeypatch.setattr(check_reconcile, "reconcile_topic", reconcile)
    cached: dict[str, bytes] = {}  # the metadata store keeps the synthetic blobs as they are
    monkeypatch.setattr(torrent_cache, "remember", lambda blob, url: cached.__setitem__(url, blob))
    monkeypatch.setattr(torrent_cache, "read", cached.get)
    sent: list[str] = []
    monkeypatch.setattr(
        check_notices, "_audited_send", lambda _secrets, *, text, **_kw: sent.append(text.split("\n")[0]) or True
    )
    folder = tmp_path / "tv"
    folder.mkdir()
    save_download_history({"schema_version": 1, "topics": {}})
    save_state(
        {
            "topics": [
                {"id": "t1", "title": "Show", "url": "https://tracker.example/1", "save_path": str(folder)},
            ]
        }
    )
    return SimpleNamespace(site=site, client=client, reconcile=reconcile, sent=sent, folder=str(folder))


def _check(**kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("how", "auto")
    return check.run_check(apply=True, notify=True, **kwargs)["results"][0]


def _topic() -> dict[str, Any]:
    return load_state()["topics"][0]


def _home() -> tuple[str, str]:
    """The status dot and the site icon of the topic's Home row."""
    record = (load_download_history().get("topics") or {}).get("t1")
    status = project_home_status(_topic(), record)
    return status.torrent_tone, status.tracker_tone


def _history(group: str = "") -> list[str]:
    """The kinds History shows for the topic, oldest first."""
    rows = history_events(group=group, limit=500)
    return [str(row.get("kind")) for row in reversed(rows) if str(row.get("topic") or row.get("topic_id")) == "t1"]


def _set_topic(**fields: Any) -> None:
    state = load_state()
    state["topics"][0].update(fields)
    save_state(state)


def test_a_new_topic_is_added_then_a_new_version_waits_for_the_previous_one_then_replaces_it(world):
    row = _check(how="manual", ids=["t1"])
    assert row["added"] is True
    assert _home() == ("ok", "ok")
    assert len(world.sent) == 1
    assert world.sent[0].startswith("Show — Fake — S01E01–02 из 2 — добавлено в торрент-клиент")

    world.site.current = NEW  # a new version on the site; the previous one still seeds the same files
    row = _check()
    assert row["ok"] is False
    assert _topic()["last_error_code"] == "check.previous_revision_active"
    assert _home() == ("bad", "ok")  # the client side needs the owner; the site answered
    assert len(world.sent) == 2
    errors_before = _history("errors")
    assert errors_before.count("check_fail") == 1

    _check()  # still waiting: neither logged nor said again
    assert _history("errors") == errors_before
    assert len(world.sent) == 2

    world.client.stop_owned_torrent(OLD)  # "stop the previous one and add"
    row = _check(how="manual", ids=["t1"])
    assert row["added"] is True
    assert row["changed"] is True
    assert _topic()["hash"] == NEW
    assert _topic()["previous_hashes"] == [OLD]
    assert _home() == ("new", "ok")
    assert world.sent[-1].endswith("(сбой устранён)")
    assert "новая версия добавлена в торрент-клиент" in world.sent[-1]
    assert _history("downloads").count("client_added") == 2


def test_a_pause_during_a_check_keeps_the_topics_last_result(world):
    world.client.put(OLD, world.folder)
    _set_topic(
        hash=OLD,
        last_ok=False,
        last_error="старая ошибка",
        last_error_code="check.previous_revision_active",
        last_error_params={"file": "Show/e01.mkv", "hash": NEW},
        last_error_class="qbit",
        error_notified=True,
    )
    world.site.current = NEW

    def paused_meanwhile() -> None:
        _set_topic(paused=True)

    world.site.on_fetch = paused_meanwhile
    sent_before = list(world.sent)

    row = _check()

    topic = _topic()
    assert row["skipped"]
    assert world.client.adds == []
    assert topic["paused"] is True
    # Nothing was handed to the client: the waiting new version is still the topic's state,
    # never a green "ok" nor a "works again" message.
    assert topic["last_error_code"] == "check.previous_revision_active"
    assert topic["last_ok"] is False
    assert _home()[0] == "bad"
    assert world.sent == sent_before


def test_a_reconcile_failure_in_a_progress_pass_ends_with_the_next_pass_that_works(world):
    _check(how="manual", ids=["t1"])
    assert _home()[0] == "ok"
    world.reconcile.failure = TowError("progress.path_differs", topic=world.folder, client=r"D:\Other")

    check.run_check(apply=True, notify=True, how="progress", progress_only=True)
    assert _topic()["last_error_code"] == "check.reconcile_failed"
    assert _home()[0] == "bad"
    assert "reconcile_failed" in _history("errors")
    said = len(world.sent)

    world.reconcile.failure = None  # the owner put the torrent back into its folder
    check.run_check(apply=True, notify=True, how="progress", progress_only=True)

    topic = _topic()
    assert not topic.get("last_error")
    assert "last_error_code" not in topic
    assert _home()[0] == "ok"
    assert len(world.sent) == said + 1
    assert world.sent[-1] == "Show — снова работает"
