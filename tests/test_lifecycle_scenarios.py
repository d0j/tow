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
        self.down = False

    def ping(self) -> str:
        if self.down:
            raise ConnectionError("refused")
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

    def adopt_torrent(self, h, *, replace_label=False):
        self.torrents[h]["tags"].append("tow")

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


def test_a_changed_file_selection_reaches_the_client_and_history(world):
    _check(how="manual", ids=["t1"])
    _set_topic(selection_dirty=True)  # the owner chose the files again on the edit form

    row = _check()

    assert row["selection_updated"] is True
    assert _topic()["selection_dirty"] is False
    assert _history("downloads")[-1] == "client_selection_updated"
    assert _home()[0] == "ok"


def test_the_daily_digest_names_the_topics_whose_downloads_completed(world):
    from datetime import datetime

    from tow import delivery

    _check(how="manual", ids=["t1"])  # added to the client
    world.reconcile.events = ["episode_completed"]
    _check()
    sent: list[str] = []

    assert delivery.maybe_digest(
        cfg={"daily_digest_hour": 0},
        send=lambda *, text, **_kw: sent.append(text) or True,
        now=datetime.now().astimezone(),
    )

    assert sent == ["TOW за сутки: добавлено в клиент 1, новых серий и файлов 0, загружено 1\nЗагружено: Show"]


@pytest.fixture
def full_disk(monkeypatch):
    """The target drive's free space, as the test sets it (MiB)."""
    import shutil

    disk = {"free": 100 * 1024 * 1024}
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: SimpleNamespace(total=0, used=0, free=disk["free"]))
    return disk


def _space_pass() -> dict[str, Any]:
    return check.run_check(apply=True, notify=True, how="progress", space_only=True)["results"][0]


def test_a_start_the_space_pass_cannot_confirm_is_in_history_once(world, full_disk):
    row = _check(how="manual", ids=["t1"])
    assert row["status"] == "waiting_space"
    assert _home() == ("bad", "ok")
    said = len(world.sent)
    full_disk["free"] = 10 * 1024**3  # room now, but the client does not start it
    world.client.starts_confirmed = False

    for _ in range(2):
        row = _space_pass()
        assert row["ok"] is False

    topic = _topic()
    assert topic["last_error_code"] == "check.start_unconfirmed"
    assert "waiting_space" in topic  # it still waits to be started
    assert _home()[0] == "bad"
    assert _history("errors").count("check_fail") == 1  # said where the messenger says it, once
    assert len(world.sent) == said + 1


def test_a_waiting_start_held_back_by_the_previous_version_is_logged_once(world, full_disk):
    world.client.put(OLD, world.folder, state="stoppedUP")
    _set_topic(hash=OLD)
    world.site.current = NEW
    row = _check()
    assert row["status"] == "waiting_space"  # the new version is added stopped
    world.client.torrents[OLD]["state"] = "uploading"  # the owner seeds the previous version again
    full_disk["free"] = 10 * 1024**3  # room now, but the previous version runs on the same files

    for _ in range(3):
        row = _check()
        assert row["ok"] is False

    topic = _topic()
    assert topic["last_error_code"] == "check.previous_revision_active"
    assert _history("errors").count("check_fail") == 1
    assert world.client.torrents[NEW]["state"] == "stoppedDL"


def _data_files() -> dict[str, bytes]:
    from tow.paths import data_dir

    root = data_dir()
    return {str(path.relative_to(root)): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


def test_a_dry_run_writes_nothing_whatever_the_topics_wait_for(world, full_disk, monkeypatch, capsys):
    from tow import cli

    full_disk["free"] = 10 * 1024**3
    _check(how="manual", ids=["t1"])  # added; the next version waits for room below
    world.site.current = NEW
    world.client.torrents[OLD]["state"] = "stoppedUP"
    state = load_state()
    state["topics"] += [
        {"id": "t2", "title": "New", "url": "https://tracker.example/2", "save_path": world.folder},
        {"id": "t3", "title": "Paused", "url": "https://tracker.example/3", "save_path": world.folder, "paused": True},
    ]
    state["daily_limit"] = {"other": "2000-01-01"}  # yesterday's limit an apply would drop
    save_state(state)
    full_disk["free"] = 100 * 1024 * 1024
    _check(ids=["t1"])  # t1's new version is added stopped and waits for room
    assert _topic()["waiting_space"]["hash"] == NEW
    full_disk["free"] = 10 * 1024**3  # room now: an apply would start it
    world.reconcile.events = ["episode_completed"]
    remembered: list[str] = []
    monkeypatch.setattr(torrent_cache, "remember", lambda blob, url: remembered.append(url))
    before, adds, sent = _data_files(), list(world.client.adds), list(world.sent)

    for args in (
        ["check"],
        ["check", "--dry-run", "--notify"],
        ["check", "--space-only"],
        ["check", "--progress-only"],
    ):
        assert cli.main([*args, "--json"]) in {0, 2}
    out = check.run_check(apply=False, notify=True, how="manual")
    assert all(row.get("preview") or row.get("status") in {"preview", "skipped"} or row["ok"] for row in out["results"])

    assert _data_files() == before
    assert world.client.adds == adds
    assert world.client.torrents[NEW]["state"] == "stoppedDL"  # never started by a preview
    assert world.sent == sent
    assert remembered == []
    capsys.readouterr()


def _check_by_hand() -> str:
    """The row's "Check" button, as the owner presses it: the message Home shows after it."""
    from fastapi.testclient import TestClient
    from helpers import flash_of

    from tow.web import app

    browser = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    return flash_of(browser.post("/topics/t1/check", follow_redirects=False).headers["location"])


@pytest.mark.parametrize("tracking_mode", ["watch", "once"])
def test_check_starts_a_waiting_torrent_once_there_is_room_and_says_so(world, full_disk, tracking_mode):
    _set_topic(tracking_mode=tracking_mode, selection={"mode": "all", "value": "", "tracking_mode": tracking_mode})
    assert _check_by_hand().startswith("добавлено в торрент-клиент, но ещё не запущено: ждёт места на диске")
    assert _topic()["waiting_space"]["hash"] == OLD
    full_disk["free"] = 10 * 1024**3  # the owner freed space and presses "Check"
    fetches = world.site.fetches

    said = _check_by_hand()

    topic = _topic()
    assert world.client.torrents[OLD]["state"] == "downloading"
    assert said == "запущено в торрент-клиенте"
    assert "waiting_space" not in topic
    assert _home()[0] == "ok"
    assert _history("downloads")[-1] == "client_started"
    if tracking_mode == "once":
        assert topic["once_done"] is True
        assert world.site.fetches == fetches  # a one-time topic is not fetched again


def test_a_waiting_torrent_of_a_paused_site_is_said_once(world, full_disk):
    _check()
    assert _topic()["last_error_code"] == "check.waiting_space"
    said = len(world.sent)
    state = load_state()
    state["mirrors"] = {"fake": {"frozen": True}}  # the owner pauses the site
    save_state(state)

    for _ in range(3):
        _check()
        _space_pass()

    topic = _topic()
    assert topic["last_error_code"] == "check.waiting_space"  # what it waits for: the site is not asked
    assert _home()[0] == "bad"
    assert len(world.sent) == said  # the same wait, not news every hour


def test_a_site_whose_sign_in_expired_for_many_topics_says_so_once_and_once_again_when_it_works(world):
    state = load_state()
    first = state["topics"][0]
    state["topics"] = [
        {**first, "id": f"t{n}", "title": f"Show {n}", "url": f"https://tracker.example/{n}"} for n in range(1, 5)
    ]
    save_state(state)
    check.run_check(apply=True, notify=True, how="manual")  # all four added
    said = len(world.sent)

    def signed_out() -> None:
        raise TowError("tracker.sign_in_page", cls="tracker_auth", prefix="fake")

    world.site.on_fetch = signed_out
    check.run_check(apply=True, notify=True, how="auto")
    assert len(world.sent) == said + 1  # one message for the site, not one per topic
    assert world.sent[-1].startswith("Сбой — fake:")

    world.site.on_fetch = None  # the owner signed in again
    check.run_check(apply=True, notify=True, how="auto")

    assert len(world.sent) == said + 2
    assert world.sent[-1].startswith("fake: снова работает")


def test_a_client_that_goes_down_and_comes_back_is_said_once_each_way(world):
    _check(how="manual", ids=["t1"])
    said = len(world.sent)

    world.client.down = True
    for _ in range(2):
        row = _check()
        assert row["ok"] is False
    assert _home() == ("bad", "ok")  # the client's fault, not the site's
    assert world.sent[said:] == ["Торрент-клиент недоступен"]  # once, not per topic or per check
    assert _history("errors") == []  # the client's own event, not one per topic
    world.client.down = False
    _check()

    assert _home() == ("ok", "ok")
    assert world.sent[said:] == ["Торрент-клиент недоступен", "Торрент-клиент снова доступен"]


def test_a_torrent_already_in_the_client_is_adopted_by_the_owner_then_managed(world):
    world.client.put(OLD, world.folder, tags=())  # added by hand before the topic
    row = _check(how="manual", ids=["t1"])
    assert row["ok"] is False
    assert _topic()["last_error_code"] == "check.not_owned_existing"
    assert _home() == ("bad", "ok")
    from tow.adopt import adopt_topic, unmarked_hash

    assert unmarked_hash(_topic(), {"client": {"id": "main"}}) == OLD  # Home offers to adopt it

    adopt_topic("t1", how="manual")
    row = _check(how="manual", ids=["t1"])

    topic = _topic()
    assert row["ok"] is True
    assert topic["hash"] == OLD
    assert "tow" in world.client.torrents[OLD]["tags"]
    assert world.client.adds == []  # adopted, never added again
    assert _home() == ("ok", "ok")
    assert "client_adopted" in _history("downloads")
    assert world.sent[-1] == "Show — снова работает"


def test_a_torrent_removed_from_the_client_is_said_and_added_again_by_check(world):
    _check(how="manual", ids=["t1"])
    world.client.put("C" * 40, world.folder, tags=())  # the client lists other torrents too
    del world.client.torrents[OLD]  # the owner removed it in the client

    _check()  # a scheduled check reports it
    assert _topic()["last_error_code"] == "check.removed_from_client"
    assert _home() == ("bad", "ok")
    assert world.sent[-1].startswith("Show — Fake — S01E01–02 из 2 — торрент исчез из торрент-клиента")
    _check()
    assert world.client.adds == [OLD]  # a scheduled check never adds it again

    assert _check_by_hand() == "добавлено в торрент-клиент"

    assert world.client.adds == [OLD, OLD]
    assert _home() == ("ok", "ok")
    assert _history("downloads") == ["client_added", "client_removed", "client_added", "client_restored"]
