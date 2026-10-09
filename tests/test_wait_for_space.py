"""A revision whose selected files do not fit on the target drive (G6): added stopped with its
file selection and confirmed, then started by TOW itself once there is room - without a new
.torrent download while it waits. Fake site, fake client, synthetic sizes."""

from __future__ import annotations

import shutil
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

from tow import check
from tow.check import notices as check_notices
from tow.check import reconcile as check_reconcile
from tow.check import run as check_run
from tow.check import space
from tow.check import topic as topic_step
from tow.clients import factory as client_factory
from tow.store import load_state, save_download_history, save_secrets, save_state
from tow.torrent import TorrentFile

OLD, NEW = "A" * 40, "B" * 40
GIB = 1024**3
FILES = (TorrentFile(0, "Show/e01.mkv", 30 * GIB), TorrentFile(1, "Show/e02.mkv", 20 * GIB))


class Tracker:
    name = "fake"

    def __init__(self) -> None:
        self.fetches = 0

    def fetch_torrent(self, url, secrets, ua, *, ignore_cool=False, persist=True):
        self.fetches += 1
        return b"torrent"

    def fetch_title(self, url, secrets, ua, *, ignore_cool=False, persist=True):
        return "Show"


class Client:
    """Keeps torrents per hash with their state, files, priorities and TOW's mark."""

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
        self.files = FILES
        self.torrents: dict[str, dict[str, Any]] = {}
        self.adds: list[tuple[str, bool]] = []
        self.starts: list[str] = []

    def ping(self) -> str:
        return "ok"

    def has_hash(self, h):
        return h in self.torrents

    def inspect_torrent(self, h):
        torrent = self.torrents.get(h)
        return None if torrent is None else {**torrent, "files": [dict(row) for row in torrent["files"]]}

    def _wanted(self, h: str, selected) -> None:
        for row in self.torrents[h]["files"]:
            row["priority"] = 1 if row["index"] in set(selected) else 0

    def add_torrent_selected(self, content, save_path, h, selected, *, start=True):
        self.adds.append((h, start))
        files = [{"index": f.index, "name": f.path, "size": f.size, "progress": 0.0, "priority": 1} for f in self.files]
        self.torrents[h] = {"hash": h, "save_path": save_path, "state": "stoppedDL", "tags": ["tow"], "files": files}
        self._wanted(h, selected)
        if start:
            self.torrents[h]["state"] = "downloading"
        return {"hash": h}

    def configure_torrent_selection(self, content, h, selected, *, ensure_started=False, keep_stopped=False):
        was_stopped = self.torrents[h]["state"].startswith("stopped")
        self._wanted(h, selected)
        if not keep_stopped and (ensure_started or not was_stopped):
            self.torrents[h]["state"] = "downloading"
        return self.inspect_torrent(h)

    def start_owned_torrent(self, h):
        assert "tow" in self.torrents[h]["tags"]
        self.starts.append(h)
        self.torrents[h]["state"] = "downloading"
        return self.inspect_torrent(h)

    def stop_owned_torrent(self, h):
        self.torrents[h]["state"] = "stoppedDL"
        return self.inspect_torrent(h)


@pytest.fixture
def world(monkeypatch, tmp_path):
    """A topic on this computer's drive, its fake site and client; free space set by the test."""
    tracker, client = Tracker(), Client()
    cfg = {"trackers": {}, "client": {"id": "main", "kind": "fake"}}
    monkeypatch.setattr(check_run, "load_config", lambda: cfg)
    monkeypatch.setattr(check_run, "load_trackers", lambda cfg: {"fake": tracker})
    monkeypatch.setattr(topic_step, "match_tracker", lambda trackers, url: tracker)
    monkeypatch.setattr(
        topic_step,
        "parse_torrent_metadata",
        lambda blob: SimpleNamespace(infohash=NEW, client_hash=NEW, name="Show", is_multi=True, files=client.files),
    )
    monkeypatch.setattr(client_factory, "default_client_id", lambda cfg: "main")
    monkeypatch.setattr(client_factory, "from_secrets", lambda cfg, secrets, client_id=None: client)
    monkeypatch.setattr(check_reconcile, "reconcile_topic", lambda *a, **k: {"events": [], "summary": {}})
    sent: list[str] = []
    monkeypatch.setattr(
        check_notices, "_audited_send", lambda _secrets, *, text, **_kw: sent.append(text.split("\n")[0]) or True
    )
    disk = {"free": 10 * GIB}
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: SimpleNamespace(total=0, used=0, free=disk["free"]))
    save_download_history({"schema_version": 1, "topics": {}})
    folder = tmp_path / "tv"
    folder.mkdir()
    save_state(
        {"topics": [{"id": "t1", "title": "Show", "url": "https://tracker.example/1", "save_path": str(folder)}]}
    )
    return SimpleNamespace(tracker=tracker, client=client, sent=sent, disk=disk, folder=folder)


def _check(**kwargs: Any) -> dict[str, Any]:
    return check.run_check(apply=True, notify=True, how="test", **kwargs)["results"][0]


def _space_pass() -> list[dict[str, Any]]:
    return check.run_check(apply=True, notify=True, how="progress", space_only=True)["results"]


def _topic() -> dict[str, Any]:
    return load_state()["topics"][0]


def test_files_that_do_not_fit_are_added_stopped_and_wait(world):
    row = _check()
    topic = _topic()
    assert world.client.adds == [(NEW, False)]  # added once, not started
    torrent = world.client.torrents[NEW]
    assert torrent["state"] == "stoppedDL"
    assert torrent["tags"] == ["tow"]
    assert [f["priority"] for f in torrent["files"]] == [1, 1]  # its file selection, confirmed
    assert topic["hash"] == NEW
    assert topic["selection_verified"] is True
    waiting = topic["waiting_space"]
    assert (waiting["hash"], waiting["needed"], waiting["free"], waiting["kind"]) == (NEW, 50 * GIB, 10 * GIB, "added")
    assert row["ok"] is False
    assert topic["last_error_code"] == "check.waiting_space"
    assert topic["last_error_class"] == "disk"  # red: the owner can free space
    assert topic["last_error"].startswith("ждёт места на диске: не хватает 40,5 ГБ (нужно 50,0 ГБ, свободно 10,0 ГБ")
    assert topic["last_error"].endswith("TOW запустит раздачу сам, когда место появится")
    assert len(world.sent) == 1
    assert world.sent[0].startswith("Сбой — Show: ждёт места на диске: не хватает 40,5 ГБ")


def test_a_waiting_topic_is_not_downloaded_again_and_tells_the_owner_once(world):
    _check()
    fetched = world.tracker.fetches
    for _ in range(2):
        row = _check()
        assert row["status"] == "waiting_space"
    assert world.tracker.fetches == fetched  # no new .torrent from the site
    assert world.client.adds == [(NEW, False)]  # nothing added again
    assert len(world.sent) == 1  # the waiting error was said once
    assert _topic()["last_error_code"] == "check.waiting_space"


def test_the_space_pass_starts_it_when_there_is_room(world):
    _check()
    assert [row["status"] for row in _space_pass()] == ["skipped"]  # still short: nothing changes
    assert world.client.starts == []
    world.disk["free"] = 60 * GIB  # the owner freed space
    fetched = world.tracker.fetches
    rows = _space_pass()
    topic = _topic()
    assert world.client.starts == [NEW]
    assert world.client.torrents[NEW]["state"] == "downloading"
    assert rows[0]["ok"] is True
    assert rows[0]["started"] is True
    assert "waiting_space" not in topic
    assert not topic.get("last_error")
    assert topic["last_ok"] is True
    assert world.tracker.fetches == fetched  # the space pass never asks the site
    assert world.sent[-1].startswith("Show — добавлено в торрент-клиент")
    assert world.sent[-1].endswith("(сбой устранён)")
    assert len(world.sent) == 2
    _space_pass()  # nothing waits any more
    assert world.client.starts == [NEW]


def test_a_regular_check_starts_it_too_and_then_checks_the_site(world):
    _check()
    world.disk["free"] = 60 * GIB
    row = _check()
    assert world.client.starts == [NEW]
    assert row["ok"] is True
    assert "waiting_space" not in _topic()
    assert world.tracker.fetches == 2  # started, then checked as always


def test_the_space_pass_refreshes_the_numbers_without_counting_as_a_check(world):
    _check()
    before = _topic()["last_check"]
    world.disk["free"] = 30 * GIB
    _space_pass()
    topic = _topic()
    assert topic["waiting_space"]["free"] == 30 * GIB
    assert "не хватает 20,5 ГБ" in topic["last_error"]
    assert topic["last_check"] == before
    assert len(world.sent) == 1


def test_files_already_in_the_folder_count_as_there(world):
    world.client.files = (TorrentFile(0, "Show/e01.mkv", 300), TorrentFile(1, "Show/e02.mkv", 200))
    world.disk["free"] = space.FREE_SPACE_MARGIN + 250  # the second episode alone fits
    _check()
    assert _topic()["waiting_space"]["needed"] == 500
    (world.folder / "Show").mkdir()
    (world.folder / "Show" / "e01.mkv").write_bytes(b"x" * 300)  # the first one was already there
    _space_pass()
    assert world.client.starts == [NEW]


def test_the_owner_starting_it_himself_ends_the_wait(world):
    _check()
    world.client.torrents[NEW]["state"] = "downloading"
    _space_pass()
    topic = _topic()
    assert "waiting_space" not in topic
    assert not topic.get("last_error")
    assert world.client.starts == []


def test_the_owner_choosing_other_files_in_the_client_ends_the_wait(world):
    _check()
    world.client.torrents[NEW]["files"][0]["priority"] = 0
    _space_pass()
    assert "waiting_space" not in _topic()
    assert world.client.starts == []  # his choice: TOW does not start it


def test_a_torrent_removed_or_unmarked_meanwhile_ends_the_wait(world):
    _check()
    world.client.torrents[NEW]["tags"] = []
    _space_pass()
    topic = _topic()
    assert "waiting_space" not in topic
    assert topic["last_error_code"] == "check.not_owned_existing"
    save_state({"topics": [{**topic, "waiting_space": {"hash": NEW, "selection": "x"}}]})
    del world.client.torrents[NEW]
    _space_pass()
    topic = _topic()
    assert "waiting_space" not in topic
    assert topic["last_error_code"] == "check.removed_from_client"


def test_a_smaller_selection_that_fits_starts_it(world):
    _check()
    topic = _topic()
    topic["selection"] = {"mode": "files", "value": "*e02.mkv"}  # 20 GiB: still does not fit in 10
    topic["selection_dirty"] = True
    save_state({"topics": [topic]})
    world.disk["free"] = 60 * GIB
    _space_pass()  # the new selection is not in the client yet: left to the check
    assert world.client.starts == []
    world.disk["free"] = 10 * GIB
    _check()
    topic = _topic()
    assert [f["priority"] for f in world.client.torrents[NEW]["files"]] == [0, 1]
    assert world.client.torrents[NEW]["state"] == "stoppedDL"
    assert topic["waiting_space"]["needed"] == 20 * GIB
    assert len(world.sent) == 1  # the same wait, not news
    world.disk["free"] = 21 * GIB
    _space_pass()
    assert world.client.starts == [NEW]
    assert "waiting_space" not in _topic()


def test_a_selection_change_that_fits_starts_it_in_the_same_check(world):
    _check()
    topic = _topic()
    topic["selection"] = {"mode": "files", "value": "*e02.mkv"}
    topic["selection_dirty"] = True
    save_state({"topics": [topic]})
    world.disk["free"] = 21 * GIB
    row = _check()
    assert world.client.torrents[NEW]["state"] == "downloading"
    assert row["ok"] is True
    assert "waiting_space" not in _topic()


def test_a_new_revision_keeps_the_running_previous_one_untouched_while_it_waits(world):
    topic = _topic()
    topic["hash"] = OLD
    save_state({"topics": [topic]})
    world.client.torrents[OLD] = {
        "hash": OLD,
        "save_path": str(world.folder),
        "state": "stoppedUP",  # stopped by the owner, so the new one may be added
        "tags": ["tow"],
        "files": [{"index": 0, "name": "Show/e01.mkv", "size": 1, "progress": 1.0, "priority": 1}],
    }
    _check()
    topic = _topic()
    assert topic["waiting_space"]["kind"] == "updated"
    assert topic["previous_hashes"] == [OLD]
    world.client.torrents[OLD]["state"] = "uploading"  # the owner started the previous one again
    world.disk["free"] = 60 * GIB
    _space_pass()
    topic = _topic()
    assert world.client.starts == []  # not beside a previous revision on the same files
    assert topic["last_error_code"] == "check.previous_revision_active"
    assert topic["waiting_space"]["hash"] == NEW
    world.client.torrents[OLD]["state"] = "stoppedUP"
    _space_pass()
    assert world.client.starts == [NEW]
    assert world.client.torrents[OLD]["state"] == "stoppedUP"  # the old one is never touched


@pytest.mark.parametrize("passing", ["checkingResumeData", "moving", "checkingDL", "missingFiles", "error", "unknown"])
def test_a_state_the_client_passes_through_keeps_the_wait(world, passing):
    # Any state but "stopped" counted as the owner's start: a qBittorrent restart
    # (checkingResumeData) or a move of the files ended the wait, cleared the error, and the
    # torrent was never started once there was room.
    _check()
    world.client.torrents[NEW]["state"] = passing
    world.disk["free"] = 60 * GIB
    _space_pass()
    topic = _topic()
    assert topic["waiting_space"]["hash"] == NEW
    assert topic["last_error_code"] == "check.waiting_space"
    assert world.client.starts == []  # nothing is started in a state of the client's own
    world.client.torrents[NEW]["state"] = "stoppedDL"
    _space_pass()
    assert world.client.starts == [NEW]


def test_a_topic_paused_while_the_run_goes_on_is_not_started(world):
    _check()
    world.disk["free"] = 60 * GIB
    inspect = world.client.inspect_torrent

    def owner_pauses_meanwhile(h):
        state = load_state()
        state["topics"][0]["paused"] = True
        save_state(state)
        return inspect(h)

    world.client.inspect_torrent = owner_pauses_meanwhile
    row = _check()
    assert world.client.starts == []
    assert row["status"] == "skipped"
    assert _topic()["paused"] is True


def test_a_drive_that_is_not_there_for_now_does_not_start_the_waiting_torrent(world, monkeypatch):
    # "Cannot be measured" made a new add not wait - and started a waiting torrent on a drive
    # that was unplugged or a share that was asleep.
    _check()
    with monkeypatch.context() as gone:
        gone.setattr("tow.folders.seen_from_here", lambda _path: False)
        _space_pass()
        gone.setattr("tow.folders.seen_from_here", lambda _path: True)
        gone.setattr(shutil, "disk_usage", lambda _p: (_ for _ in ()).throw(OSError("asleep")))
        _space_pass()
    assert world.client.starts == []
    topic = _topic()
    assert topic["waiting_space"]["free"] == 10 * GIB
    assert topic["last_error_code"] == "check.waiting_space"
    world.disk["free"] = 60 * GIB
    _space_pass()
    assert world.client.starts == [NEW]


def test_a_wait_recorded_for_another_client_is_dropped_quietly(world):
    _check()
    topic = _topic()
    topic["waiting_space"]["client"] = "other"
    save_state({"topics": [topic]})
    _space_pass()
    topic = _topic()
    assert "waiting_space" not in topic
    assert topic["last_error_code"] == "check.waiting_space"  # not "removed from client"


def test_a_wait_for_the_previous_revision_to_stop_is_logged_once(world):
    from tow.log import read_events

    topic = _topic()
    topic["hash"] = OLD
    save_state({"topics": [topic]})
    world.client.torrents[OLD] = {
        "hash": OLD,
        "save_path": str(world.folder),
        "state": "stoppedUP",
        "tags": ["tow"],
        "files": [{"index": 0, "name": "Show/e01.mkv", "size": 1, "progress": 1.0, "priority": 1}],
    }
    _check()
    world.client.torrents[OLD]["state"] = "uploading"  # the owner started the previous one again
    world.disk["free"] = 60 * GIB
    for _ in range(3):
        row = _check()
        assert row["error_record"]["code"] == "check.previous_revision_active"
    assert _topic()["last_error_params"]["hash"] == NEW  # which revision waits
    assert [event["kind"] for event in read_events(limit=50)].count("check_fail") == 1


def test_an_unconfirmed_stopped_add_is_not_started_by_its_recovery_without_room(world):
    # The add went in stopped (it does not fit) but its read-back failed once: the next check
    # finished the add the usual way - and started it, with 10 GiB free for 50 GiB.
    inspect = world.client.inspect_torrent
    reads = {"n": 0}

    def read_back_fails_once(h):
        reads["n"] += 1
        info = inspect(h)
        if info is not None and reads["n"] == 2:  # the add's own read-back sees another folder
            info["save_path"] = "Z:\\elsewhere"
        return info

    world.client.inspect_torrent = read_back_fails_once
    _check()
    assert world.client.adds == [(NEW, False)]
    assert _topic()["last_error_code"] == "check.add_unconfirmed"
    row = _check()  # still 10 GiB free
    topic = _topic()
    assert world.client.torrents[NEW]["state"] == "stoppedDL"
    assert row["status"] == "waiting_space"
    assert topic["waiting_space"]["hash"] == NEW
    assert topic["last_error_code"] == "check.waiting_space"
    world.disk["free"] = 60 * GIB
    _space_pass()
    assert world.client.starts == [NEW]


def test_a_folder_not_seen_from_here_never_waits(world, monkeypatch):
    monkeypatch.setattr("tow.folders.seen_from_here", lambda _path: False)
    row = _check()
    assert world.client.adds == [(NEW, True)]
    assert row["ok"] is True
    assert "waiting_space" not in _topic()


def test_unknown_free_space_never_waits(world, monkeypatch):
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: (_ for _ in ()).throw(OSError("unreachable")))
    _check()
    assert world.client.adds == [(NEW, True)]


def test_a_remote_client_is_not_judged_by_this_computers_disk(world):
    save_secrets({"qbittorrent": {"host": "192.168.1.5"}})
    _check()
    assert world.client.adds == [(NEW, True)]
    assert "waiting_space" not in _topic()


def test_a_dry_run_neither_adds_nor_starts(world):
    preview = check.run_check(apply=False, notify=True, how="test")["results"][0]
    assert world.client.adds == []
    assert preview["status"] == "preview"
    _check()
    world.disk["free"] = 60 * GIB
    state_before = load_state()
    rows = check.run_check(apply=False, notify=True, how="progress", space_only=True)["results"]
    assert rows[0].get("would_start") is True
    check.run_check(apply=False, notify=True, how="test")
    assert world.client.starts == []
    assert load_state() == state_before
    assert len(world.sent) == 1


def test_a_paused_waiting_topic_is_left_alone(world):
    _check()
    topic = _topic()
    topic["paused"] = True
    save_state({"topics": [topic]})
    world.disk["free"] = 60 * GIB
    assert _space_pass() == []
    assert world.client.starts == []


def test_the_shortfall_says_what_to_free():
    short = space.Shortfall(needed=50 * GIB, free=10 * GIB, path="D:\\")
    assert short.missing == 40 * GIB + space.FREE_SPACE_MARGIN
    assert short.error().params == {"missing": 40.5, "needed": 50.0, "free": 10.0, "path": "D:\\"}
    assert space.Shortfall(needed=1, free=space.FREE_SPACE_MARGIN, path="D:\\").error().params["missing"] == 0.1


# --- the add and edit forms' free-space hint ------------------------------------------------------


def test_the_forms_hint_measures_the_folder_as_an_add_does(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from tow.web import app

    shown = space.folder_free(str(tmp_path / "not yet created"))
    assert isinstance(shown["free"], int)
    assert shown["free"] > 0
    assert shown["margin"] == space.FREE_SPACE_MARGIN
    with monkeypatch.context() as remote:
        remote.setattr("tow.folders.seen_from_here", lambda _path: False)  # a remote client's /downloads
        assert space.folder_free("/downloads/tv") == {"free": None, "margin": space.FREE_SPACE_MARGIN}
    assert space.folder_free("") == {"free": None, "margin": space.FREE_SPACE_MARGIN}
    answer = TestClient(app).get("/content/space", params={"path": str(tmp_path)}, headers={"X-TOW-Space": "1"})
    assert answer.status_code == 200
    assert isinstance(answer.json()["free"], int)


def test_only_tows_own_forms_get_the_hint(monkeypatch):
    # A GET is not checked for its origin: an image on any site could make TOW look at a folder
    # of its choosing, on Windows \\host\share of another computer (which gets the sign-in).
    from fastapi.testclient import TestClient

    from tow.web import app, services

    asked: list[str] = []
    monkeypatch.setattr(services, "folder_free", lambda path: asked.append(path) or {"free": 1, "margin": 0})
    client = TestClient(app)
    for headers in ({}, {"X-TOW-Space": "0"}, {"Sec-Fetch-Site": "cross-site", "Accept": "application/json"}):
        answer = client.get("/content/space", params={"path": r"\\203.0.113.7\share"}, headers=headers)
        assert answer.status_code == 403
    assert asked == []
    assert client.get("/content/space", params={"path": "D:\\TV"}, headers={"X-TOW-Space": "1"}).status_code == 200
    assert asked == ["D:\\TV"]


def test_both_forms_show_the_hint_under_the_folder_and_warn_when_it_will_not_fit():
    from pathlib import Path

    src = Path(space.__file__).resolve().parents[1]
    for name in ("index.html", "_topic_edit.html"):
        template = (src / "templates" / name).read_text(encoding="utf-8")
        folder = template[template.index('name="save_path"') :]
        assert "data-space-hint" in folder[:700]  # right under the folder field
    script = (src / "static" / "content.js").read_text(encoding="utf-8")
    assert "/content/space?path=" in script
    assert '"X-TOW-Space": "1"' in script  # the header the route requires
    assert "need + margin > free" in script  # the add's own rule: the chosen bytes and the margin
    assert '"content.js.space_short"' in script
    assert ".space-hint.warn { color: var(--warn); }" in (src / "static" / "app.css").read_text(encoding="utf-8")


# --- a start or a reselect is only told once the client confirms it (mutation survivors) ------


def _recording_selection(client: Client) -> list[bool]:
    calls: list[bool] = []
    configure = client.configure_torrent_selection

    def recorded(content, h, selected, *, ensure_started=False):
        calls.append(ensure_started)
        return configure(content, h, selected, ensure_started=ensure_started)

    client.configure_torrent_selection = recorded  # type: ignore[method-assign]
    return calls


def test_space_start_not_read_back_keeps_waiting(world):
    from tow.log import read_events

    _check()
    world.disk["free"] = 60 * GIB
    world.client.torrents[NEW]["progress"] = 0.5
    world.client.start_owned_torrent = lambda h: world.client.inspect_torrent(h)  # accepted, never carried out
    rows = _space_pass()
    topic = _topic()
    assert rows[0]["ok"] is False
    assert topic["last_error_code"] == "check.start_unconfirmed"
    assert topic["waiting_space"]["hash"] == NEW  # still waiting: the next pass tries again
    assert world.client.torrents[NEW]["state"] == "stoppedDL"
    assert "client_started" not in {event["kind"] for event in read_events(limit=500)}
    world.client.torrents[NEW]["progress"] = 1.0  # a complete torrent may stay stopped after its start
    assert _space_pass()[0]["ok"] is True
    assert "waiting_space" not in _topic()


def test_a_reselect_that_fits_does_not_start_beside_a_live_previous_revision(world):
    topic = _topic()
    topic["hash"] = OLD
    save_state({"topics": [topic]})
    world.client.torrents[OLD] = {
        "hash": OLD,
        "save_path": str(world.folder),
        "state": "stoppedUP",
        "tags": ["tow"],
        "files": [{"index": 0, "name": "Show/e01.mkv", "size": 1, "progress": 1.0, "priority": 1}],
    }
    _check()
    assert _topic()["waiting_space"]["kind"] == "updated"
    world.client.torrents[OLD]["state"] = "uploading"  # the owner started the previous one again
    topic = _topic()
    topic["selection"] = {"mode": "files", "value": "*e02.mkv"}
    topic["selection_dirty"] = True
    save_state({"topics": [topic]})
    world.disk["free"] = 21 * GIB  # the new selection fits now
    calls = _recording_selection(world.client)
    row = _check()
    topic = _topic()
    assert row["ok"] is False
    assert topic["last_error_code"] == "check.previous_revision_active"
    assert True not in calls  # never started beside the running previous revision
    assert world.client.torrents[NEW]["state"] == "stoppedDL"
    assert world.client.torrents[OLD]["state"] == "uploading"  # and the old one is not touched
    assert topic["waiting_space"]["hash"] == NEW


def test_a_rollback_to_the_previous_revision_is_not_an_unfinished_own_add(world, monkeypatch):
    world.disk["free"] = 600 * GIB
    _check()  # NEW added and running
    world.client.torrents[OLD] = {
        "hash": OLD,
        "save_path": str(world.folder),
        "state": "stoppedUP",
        "tags": ["tow"],
        "files": [{"index": f.index, "name": f.path, "size": f.size, "progress": 1.0, "priority": 1} for f in FILES],
    }
    topic = _topic()
    topic["previous_hashes"] = [OLD]
    save_state({"topics": [topic]})
    world.client.torrents[NEW]["state"] = "stoppedDL"  # the new one is not running, so a switch is allowed
    monkeypatch.setattr(
        topic_step,
        "parse_torrent_metadata",
        lambda blob: SimpleNamespace(infohash=OLD, client_hash=OLD, name="Show", is_multi=True, files=FILES),
    )
    calls = _recording_selection(world.client)
    row = _check()
    assert calls == [False]  # the owner's stopped older revision is not started by TOW
    assert row.get("added") is False
    assert world.client.torrents[OLD]["state"] == "stoppedUP"
    assert _topic()["hash"] == OLD


def test_a_hash_another_topic_records_is_not_an_unfinished_own_add(world):
    world.disk["free"] = 600 * GIB
    world.client.add_torrent_selected(b"torrent", str(world.folder), NEW, [0, 1], start=False)
    state = load_state()
    other = {"id": "t2", "title": "Show", "url": "https://tracker.example/2", "save_path": str(world.folder)}
    save_state({"topics": [*state["topics"], {**other, "hash": NEW}]})
    calls = _recording_selection(world.client)
    row = check.run_check(apply=True, notify=True, how="test", ids=["t1"])["results"][0]
    assert calls == [False]  # the other topic's stopped torrent is not started as if TOW's own add
    assert row.get("added") is False
    assert world.client.torrents[NEW]["state"] == "stoppedDL"


def test_a_reselect_of_a_stopped_revision_that_does_not_wait_is_not_blocked_by_the_previous_one(world):
    topic = _topic()
    topic["hash"] = OLD
    save_state({"topics": [topic]})
    world.client.torrents[OLD] = {
        "hash": OLD,
        "save_path": str(world.folder),
        "state": "stoppedUP",
        "tags": ["tow"],
        "files": [{"index": 0, "name": "Show/e01.mkv", "size": 1, "progress": 1.0, "priority": 1}],
    }
    world.disk["free"] = 600 * GIB
    _check()  # NEW added and started: nothing waits
    world.client.torrents[NEW]["state"] = "stoppedDL"  # the owner stopped the new one...
    world.client.torrents[OLD]["state"] = "uploading"  # ...and runs the previous one again
    topic = _topic()
    assert "waiting_space" not in topic
    topic["selection"] = {"mode": "files", "value": "*e02.mkv"}
    topic["selection_dirty"] = True
    save_state({"topics": [topic]})
    calls = _recording_selection(world.client)
    row = _check()
    assert row["ok"] is True  # only the selection changes; nothing starts beside the old one
    assert calls == [False]
    assert [f["priority"] for f in world.client.torrents[NEW]["files"]] == [0, 1]
    assert world.client.torrents[NEW]["state"] == "stoppedDL"
