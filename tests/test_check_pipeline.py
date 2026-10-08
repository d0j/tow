"""The check pipeline end to end with a fake site and fake torrent clients (synthetic data):
what it adds, when it reports, and what it leaves to the next check."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

from tow import check
from tow.check import apply as check_apply
from tow.check import reconcile as check_reconcile
from tow.check import run as check_run
from tow.check import topic as topic_step
from tow.clients import factory as client_factory
from tow.notify import NotificationBatch
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
    monkeypatch.setattr(check_run, "load_config", lambda: cfg)
    monkeypatch.setattr(check_run, "load_trackers", lambda cfg: {"fake": tracker})
    monkeypatch.setattr(topic_step, "match_tracker", lambda trackers, url: tracker)
    monkeypatch.setattr(
        topic_step,
        "parse_torrent_metadata",
        lambda blob: SimpleNamespace(
            infohash=NEW, client_hash=NEW, name="Show", is_multi=True, files=(TorrentFile(0, "Show/e01.mkv", 1),)
        ),
    )
    monkeypatch.setattr(client_factory, "default_client_id", lambda cfg: "main")
    monkeypatch.setattr(
        client_factory, "from_secrets", lambda cfg, secrets, client_id=None: clients[client_id or "main"]
    )
    monkeypatch.setattr(check_apply, "free_space_problem", lambda *a, **k: None)
    monkeypatch.setattr(check_reconcile, "reconcile_topic", lambda *a, **k: {"events": [], "summary": {}})


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


# --- the free-space check measures only this computer's disks -----------------------------------


@pytest.mark.parametrize(("host", "remote"), [("192.168.1.5", True), ("127.0.0.1", False), ("", False)])
def test_free_space_is_judged_only_for_a_client_on_this_computer(monkeypatch, stores, host, remote):
    """A remote client's /downloads whose top folder also exists here was measured on this
    computer's disk (and could refuse the add with "not enough space")."""
    from tow.errors import TowError
    from tow.store import save_secrets

    save_secrets({"qbittorrent": {"host": host}})
    save_state({"topics": [_topic(hash=OLD)]})
    client = Client()
    _wire(monkeypatch, {"main": client}, Tracker())
    full = TowError("check.low_disk", needed=1.0, free=0.1, path="/media")
    monkeypatch.setattr(check_apply, "free_space_problem", lambda *a, **k: full)
    row = check.run_check(apply=True, notify=False, how="test")["results"][0]
    assert row["ok"] is remote
    assert client.adds == ([(NEW, "/media/tv")] if remote else [])


@pytest.mark.parametrize(
    ("host", "here"),
    [
        ("127.0.0.1", True),
        ("http://localhost:8080", True),
        ("[::1]", True),
        ("", True),
        ("192.168.1.5", False),
        ("https://nas.example:9091/transmission/rpc", False),
    ],
)
def test_which_client_runs_on_this_computer(host, here):
    from tow.clients.factory import on_this_computer

    assert on_this_computer({}, {"qbittorrent": {"host": host}}) is here


# --- a client still loading its torrents after a start -----------------------------------------


class StartingClient(Client):
    """Answers, but lists no torrent yet (qBittorrent loads them after its Web UI is up)."""

    loading = True

    def has_any_torrent(self) -> bool:
        return bool(self.torrents) and not self.loading

    def has_hash(self, h):
        return False if self.loading else super().has_hash(h)

    def inspect_torrent(self, h):
        return None if self.loading else super().inspect_torrent(h)


def test_a_client_that_lists_nothing_yet_is_not_read_as_removed_or_added_to(monkeypatch, stores):
    client = StartingClient()
    client.put(OLD, "/media/tv", tags=["tow"])
    save_state({"topics": [_topic(hash=OLD, last_ok=True)]})
    _wire(monkeypatch, {"main": client}, Tracker())
    check.run_check(apply=True, notify=False, how="test")
    saved = load_state()["topics"][0]
    assert client.adds == []
    assert saved["last_error_code"] == "check.client_unreachable"
    assert saved["last_error_params"]["reason"]["$msg"]["code"] == "check.client_empty"
    client.loading = False  # loaded: the next check works as always
    check.run_check(apply=True, notify=False, how="test")
    assert client.adds == [(NEW, "/media/tv")]


def test_a_finished_one_time_topic_does_not_hold_an_emptied_client(monkeypatch, stores):
    """A finished "once" topic is skipped without a new result, so it keeps last_ok and its hash
    forever: an emptied (or new) client was "still starting" on every check and nothing was added."""
    client = StartingClient()
    client.loading = False
    save_state(
        {
            "topics": [
                {**_topic(hash=OLD, last_ok=True, once_done=True, tracking_mode="once"), "id": "done"},
                {**_topic(hash=None), "id": "t2", "url": "https://tracker.example/2"},
            ]
        }
    )
    _wire(monkeypatch, {"main": client}, Tracker())
    check.run_check(apply=True, notify=False, how="test")
    assert client.adds == [(NEW, "/media/tv")]


def test_a_topic_whose_timer_is_not_due_does_not_hold_an_emptied_client(monkeypatch, stores):
    client = StartingClient()
    client.loading = False
    save_state(
        {
            "topics": [
                {**_topic(hash=OLD, last_ok=True, check_interval_min=1440), "id": "later"},
                {**_topic(hash=None), "id": "t2", "url": "https://tracker.example/2"},
            ]
        }
    )
    _wire(monkeypatch, {"main": client}, Tracker())
    check.run_check(apply=True, notify=False, how="auto", scheduled_scope="global")
    assert client.adds == [(NEW, "/media/tv")]


def test_a_client_that_stays_empty_is_believed_after_two_checks(monkeypatch, stores):
    client = StartingClient()
    client.loading = False  # emptied by the owner: nothing will ever be listed
    save_state({"topics": [_topic(hash=OLD, last_ok=True)]})
    _wire(monkeypatch, {"main": client}, Tracker())
    for _ in range(2):
        check.run_check(apply=True, notify=False, how="test")
        saved = load_state()["topics"][0]
        assert saved["last_error_params"]["reason"]["$msg"]["code"] == "check.client_empty"
        assert client.adds == []
    assert load_state()["health"]["clients_empty"] == {"main": 2}
    check.run_check(apply=True, notify=False, how="test")
    assert client.adds == [(NEW, "/media/tv")]
    check.run_check(apply=True, notify=False, how="test")  # lists the torrent now: the count is gone
    assert "clients_empty" not in load_state()["health"]


@pytest.mark.parametrize("between", ["one_topic", "no_topic"])
def test_a_run_that_does_not_ask_a_client_keeps_its_empty_count(monkeypatch, stores, between):
    """A check in between that did not ask the second client (the owner checked a topic of the
    main one, or there was nothing to check) dropped its count: the still-loading client was
    then believed empty after one refusal instead of two and its torrent was added again."""
    main = StartingClient("main")
    main.loading = False
    main.put(OLD, "/media/tv", tags=["tow"])
    other = StartingClient("b")
    other.put(OLD, "/media/tv", tags=["tow"])  # still loading: lists nothing yet
    save_state(
        {
            "topics": [
                _topic(hash=OLD, last_ok=True),
                {**_topic(hash=OLD, last_ok=True, client_id="b"), "id": "t2", "url": "https://tracker.example/2"},
            ]
        }
    )
    _wire(monkeypatch, {"main": main, "b": other}, Tracker())
    check.run_check(apply=True, notify=False, how="test")
    assert load_state()["health"]["clients_empty"] == {"b": 1}
    check.run_check(apply=True, notify=False, how="test", ids=["t1"] if between == "one_topic" else [])
    assert load_state()["health"]["clients_empty"] == {"b": 1}
    check.run_check(apply=True, notify=False, how="test")
    t2 = next(topic for topic in load_state()["topics"] if topic["id"] == "t2")
    assert other.adds == []
    assert t2["last_error_params"]["reason"]["$msg"]["code"] == "check.client_empty"
    assert load_state()["health"]["clients_empty"] == {"b": 2}


def test_an_empty_client_without_earlier_torrents_is_used(monkeypatch, stores):
    client = StartingClient()
    client.loading = False
    save_state({"topics": [_topic(hash=None)]})
    _wire(monkeypatch, {"main": client}, Tracker())
    check.run_check(apply=True, notify=False, how="test")
    assert client.adds == [(NEW, "/media/tv")]


# --- the header clock's reason does not depend on the language ---------------------------------


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_check_failure_reason_comes_from_the_error_not_its_words(lang):
    from tow import i18n
    from tow.errors import TowError
    from tow.store import MissingMasterKeyError, SecretStoreError

    i18n.use(lang)
    assert check_run._check_failure_code(MissingMasterKeyError("store.no_master_key")) == "secrets_migration_required"
    assert check_run._check_failure_code(SecretStoreError("legacy plaintext secrets")) == "secrets_migration_required"
    assert check_run._check_failure_code(TowError("check.daily_limit")) == "quota"
    assert check_run._check_failure_code(RuntimeError("the master key of the client")) == "error"


# --- owner edits made while the check runs -------------------------------------------------------


def _edit_saved_topic(**changes: Any) -> Any:
    from tow.store import persistence_lock

    def edit() -> None:
        with persistence_lock():
            state = load_state()
            state["topics"][0].update(changes)
            save_state(state)

    return edit


def test_folder_moved_during_the_check_is_left_to_the_next_check(monkeypatch, stores):
    """The owner moves the topic to /media/new while the check fetches: the new revision went
    to /media/old while the saved topic said /media/new."""
    save_state({"topics": [_topic(hash=OLD, save_path="/media/old")]})
    client = Client()
    client.put(OLD, "/media/old", tags=["tow"])
    moved = _edit_saved_topic(save_path="/media/new", move_pending={"from": "/media/old", "to": "/media/new"})
    _wire(monkeypatch, {"main": client}, Tracker(during_fetch=moved))
    row = check.run_check(apply=True, notify=False, how="test")["results"][0]
    assert client.adds == []
    assert row["status"] == "skipped"
    assert row["skipped"].startswith("во время проверки изменились папка")
    saved = load_state()["topics"][0]
    assert (saved["hash"], saved["save_path"]) == (OLD, "/media/new")
    # The next check adds the new revision where the topic now says.
    _wire(monkeypatch, {"main": client}, Tracker())
    check.run_check(apply=True, notify=False, how="test")
    assert client.adds == [(NEW, "/media/new")]


def test_client_switched_during_the_first_check_is_left_to_the_next_check(monkeypatch, stores):
    save_state({"topics": [_topic(hash=None)]})
    main, second = Client("main"), Client("second")
    _wire(monkeypatch, {"main": main, "second": second}, Tracker(during_fetch=_edit_saved_topic(client_id="second")))
    check.run_check(apply=True, notify=False, how="test")
    saved = load_state()["topics"][0]
    assert main.adds == []
    assert (saved["client_id"], saved.get("hash")) == ("second", None)
    _wire(monkeypatch, {"main": main, "second": second}, Tracker())
    check.run_check(apply=True, notify=False, how="test")
    assert (main.adds, second.adds) == ([], [(NEW, "/media/tv")])
    assert load_state()["topics"][0]["hash"] == NEW


def test_selection_changed_during_the_check_is_left_to_the_next_check(monkeypatch, stores):
    save_state({"topics": [_topic(hash=None)]})
    client = Client()
    changed = _edit_saved_topic(selection={"mode": "files", "value": "*.mkv"}, selection_dirty=True)
    _wire(monkeypatch, {"main": client}, Tracker(during_fetch=changed))
    check.run_check(apply=True, notify=False, how="test")
    assert client.adds == []
    assert load_state()["topics"][0]["selection"] == {"mode": "files", "value": "*.mkv"}


# --- when the owner hears of an error ----------------------------------------------------------


def _notified(monkeypatch) -> list[tuple[Any, Any]]:
    seen: list[tuple[Any, Any]] = []
    real = NotificationBatch.queue

    def spy(self, topic, **kw):
        seen.append((topic.get("id"), kw.get("kind")))
        return real(self, topic, **kw)

    monkeypatch.setattr(NotificationBatch, "queue", spy)
    monkeypatch.setattr(check_run, "flush_notifications", lambda *a, **k: None)
    return seen


class Failing(Tracker):
    """Fails on the runs listed in ``failing`` (1-based) with ``error``."""

    def __init__(self, failing: set[int], error: Exception) -> None:
        super().__init__()
        self.failing, self.error, self.n = failing, error, 0

    def fetch_torrent(self, *a, **k):
        self.n += 1
        if self.n in self.failing:
            raise self.error
        return b"torrent"


def _healthy_topic(client: Client) -> None:
    client.put(NEW, "/media/tv", tags=["tow"])
    save_state({"topics": [_topic(hash=NEW, last_ok=True)]})


def _site_down() -> Exception:
    from tow.errors import TowError

    return TowError("mirrors.all_failed", tracker="fake", error="x")


def test_a_flapping_site_is_not_reported_every_other_check(monkeypatch, stores):
    client = Client()
    _healthy_topic(client)
    notified = _notified(monkeypatch)
    _wire(monkeypatch, {"main": client}, Failing({1, 3, 5}, _site_down()))
    for _ in range(6):
        check.run_check(apply=True, notify=True, how="auto")
    assert notified == []
    assert not load_state()["topics"][0].get("last_error")  # the last check was fine
    assert "error_streak" not in load_state()["topics"][0]


def test_a_lasting_site_problem_is_reported_once_after_three_checks(monkeypatch, stores):
    client = Client()
    _healthy_topic(client)
    notified = _notified(monkeypatch)
    _wire(monkeypatch, {"main": client}, Failing({1, 2, 3, 4}, _site_down()))
    kinds = []
    for _ in range(5):
        check.run_check(apply=True, notify=True, how="auto")
        kinds.append([kind for _tid, kind in notified])
        notified.clear()
    assert kinds == [[], [], ["error"], [], ["recovered"]]
    assert load_state()["topics"][0]["last_error_class"] == ""


def test_a_red_error_is_reported_at_once_and_once(monkeypatch, stores):
    from tow.errors import TowError

    client = Client()
    _healthy_topic(client)
    notified = _notified(monkeypatch)
    _wire(monkeypatch, {"main": client}, Failing({1, 2}, TowError("tracker.not_torrent")))
    check.run_check(apply=True, notify=True, how="auto")
    check.run_check(apply=True, notify=True, how="auto")
    assert [kind for _tid, kind in notified] == ["error"]


def test_a_check_without_messages_keeps_the_recovery_for_the_next_one(monkeypatch, stores):
    """A check that sends nothing (the CLI's apply without notify) after a reported failure:
    the owner who heard "error" still hears "working again" from the next check that sends."""
    client = Client()
    client.put(NEW, "/media/tv", tags=["tow"])
    failed = _topic(hash=NEW, last_error="boom", last_error_class="tracker", error_notified=True, last_ok=False)
    save_state({"topics": [failed]})
    notified = _notified(monkeypatch)
    _wire(monkeypatch, {"main": client}, Tracker())
    check.run_check(apply=True, notify=False, how="manual")
    assert notified == []
    assert load_state()["topics"][0]["error_notified"] is True
    check.run_check(apply=True, notify=True, how="auto")
    assert [kind for _tid, kind in notified] == ["recovered"]
    assert "error_notified" not in load_state()["topics"][0]
