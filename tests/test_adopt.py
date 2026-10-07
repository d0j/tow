"""Adopt into TOW: a topic whose torrent is already in its client without TOW's mark (added
by hand, or by Monitorrent before the move). Only on the owner's request; only the mark
changes; it is read back, recorded and logged; the next check manages the torrent."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from helpers import shown
from test_check_pipeline import NEW, OLD, Client, Tracker, _topic, _wire
from test_clients_managed import E01, TORRENT, FakeDeluge, FakeTransmission, H, K, Torrent, _deluge, _transmission

from tow import adopt, check, cli
from tow.errors import TowError
from tow.paths import data_dir
from tow.store import check_run_lock, load_state, save_download_history, save_state
from tow.web import app

ORIGIN = {"Origin": "http://127.0.0.1"}


@pytest.fixture(autouse=True)
def _stores():
    save_download_history({"schema_version": 1, "topics": {}})


def _events() -> list[dict[str, Any]]:
    path = data_dir() / "tow.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _unmarked_topic(found: str = NEW, **extra: Any) -> dict[str, Any]:
    """What a check leaves on a topic whose torrent it found in the client without the mark (and
    the link and client the check saw it for)."""
    topic = _topic(**{"client_id": "main", **extra})
    seen = {"hash": found, "url": topic["url"], "client": topic["client_id"]}
    fields: dict[str, Any] = {
        "hash": None,
        "last_ok": False,
        "last_error": "x",
        "last_error_class": "qbit",
        "last_error_code": "check.not_owned_existing",
        "last_error_params": seen,
    }
    return {**topic, **fields, **extra}


# --- the clients: only the mark changes -------------------------------------------------------


def _by_hand(server: Any, labels: list[str], *, running: bool) -> Torrent:
    torrent = Torrent(TORRENT, "/srv/media/tv", labels, paused=not running)
    torrent.wanted = [index == E01 for index in range(len(torrent.files))]
    server.torrents[K] = torrent
    return torrent


@pytest.mark.parametrize("running", [True, False])
def test_transmission_adoption_adds_the_label_and_nothing_else(running):
    server = FakeTransmission()
    torrent = _by_hand(server, ["mine"], running=running)
    before = (torrent.path, list(torrent.wanted), torrent.running)
    tags = _transmission(server).adopt_torrent(H)
    assert sorted(tags) == ["mine", "tow"]
    assert (torrent.path, torrent.wanted, torrent.running) == before
    assert "tow-pending" not in torrent.labels  # never an unfinished add: a check must not start it


def test_deluge_adoption_sets_its_one_label_and_nothing_else():
    server = FakeDeluge()
    adapter = _deluge(server)
    adapter.ping()
    torrent = _by_hand(server, [], running=True)
    before = (torrent.path, list(torrent.wanted), torrent.running)
    assert adapter.adopt_torrent(H) == ["tow"]
    assert torrent.labels == ["tow"]
    assert (torrent.path, torrent.wanted, torrent.running) == before


def test_deluge_never_replaces_the_owners_label_unless_told():
    """Deluge keeps one label: TOW's would silently take the owner's "movies" away."""
    server = FakeDeluge()
    adapter = _deluge(server)
    adapter.ping()
    torrent = _by_hand(server, ["movies"], running=True)
    with pytest.raises(TowError) as raised:
        adapter.adopt_torrent(H)
    assert raised.value.code == "client.deluge.adopt_has_label"
    assert raised.value.params["label"] == "movies"
    assert torrent.labels == ["movies"]
    assert adapter.adopt_torrent(H, replace_label=True) == ["tow"]
    assert torrent.labels == ["tow"]


def test_qbittorrent_adoption_adds_the_tag_only():
    from tow.clients import qbittorrent

    class Api:
        def __init__(self) -> None:
            self.row = type("Row", (), {"tags": "mine", "category": "tv", "save_path": "/srv/media/tv"})()
            self.calls: list[str] = []

        def torrents_info(self, *, torrent_hashes):
            return [self.row]

        def torrents_add_tags(self, *, tags, torrent_hashes):
            self.calls.append(f"add_tags {tags}")
            self.row.tags = f"{self.row.tags}, {tags}"

    client = qbittorrent.QBittorrentClient.__new__(qbittorrent.QBittorrentClient)
    client._c = Api()
    assert client.adopt_torrent(H) == ["mine", "tow"]
    assert client._c.calls == ["add_tags tow"]  # no category, no move, no start or stop
    assert client._c.row.category == "tv"


def test_a_mark_the_client_does_not_keep_is_reported():
    class Forgetful(FakeTransmission):
        def handler(self, request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content) if request.content else {}
            if body.get("method") == "torrent-set":
                return httpx.Response(200, json={"result": "success", "arguments": {}})
            return super().handler(request)

    server = Forgetful()
    _by_hand(server, [], running=True)
    with pytest.raises(TowError) as raised:
        _transmission(server).adopt_torrent(H)
    assert raised.value.code == "client.managed.adopt_unconfirmed"


# --- the action: asked for, read back, recorded, logged; the next check manages it ------------


class Adoptable(Client):
    def adopt_torrent(self, h):
        self.torrents[h]["tags"] = [*self.torrents[h]["tags"], "tow"]
        return self.torrents[h]["tags"]

    def configure_torrent_selection(self, content, h, selected, *, ensure_started=False):
        self.configured = True
        return super().configure_torrent_selection(content, h, selected, ensure_started=ensure_started)


def test_check_reports_the_unmarked_torrent_with_its_hash(monkeypatch):
    save_state({"topics": [_topic(hash=None)]})
    client = Adoptable()
    client.put(NEW, "/media/tv", tags=[])
    _wire(monkeypatch, {"main": client}, Tracker())
    check.run_check(apply=True, notify=False, how="test")
    saved = load_state()["topics"][0]
    assert adopt.unmarked_hash(saved) == NEW
    assert saved["last_error_params"] == {"hash": NEW, "url": "https://tracker.example/1", "client": "main"}


@pytest.mark.parametrize(
    "change",
    [
        {"url": "https://tracker.example/999"},  # the link was edited after the check
        {"client_id": "other"},  # the topic was moved to another client
    ],
)
def test_a_torrent_seen_for_another_link_or_client_is_not_adopted(monkeypatch, change):
    """The unmarked hash belongs to what the check saw: after an edit "Adopt" would have marked
    the old link's torrent as the new link's revision."""
    save_state({"topics": [{**_unmarked_topic(), **change}]})
    client = Adoptable()
    client.put(NEW, "/media/tv", tags=[])
    _wire(monkeypatch, {"main": client, "other": client}, Tracker())
    saved = load_state()["topics"][0]
    assert adopt.unmarked_hash(saved) == ""
    with pytest.raises(adopt.AdoptError) as raised:
        adopt.adopt_topic("t1", how="manual")
    assert raised.value.code == "adopt.no_torrent"
    assert client.torrents[NEW]["tags"] == []
    assert load_state()["topics"][0].get("hash") is None


def test_a_record_from_before_without_the_link_waits_for_a_check():
    topic = _unmarked_topic()
    topic["last_error_params"] = {"hash": NEW}
    assert adopt.unmarked_hash(topic) == ""


def test_a_topic_edited_while_it_is_adopted_is_not_recorded(monkeypatch):
    save_state({"topics": [_unmarked_topic()]})

    class EditedMeanwhile(Adoptable):
        def adopt_torrent(self, h):
            state = load_state()
            state["topics"][0]["url"] = "https://tracker.example/999"
            save_state(state)
            return super().adopt_torrent(h)

    client = EditedMeanwhile()
    client.put(NEW, "/media/tv", tags=[])
    _wire(monkeypatch, {"main": client}, Tracker())
    with pytest.raises(adopt.AdoptError) as raised:
        adopt.adopt_topic("t1", how="manual")
    assert raised.value.code == "adopt.topic_changed"
    assert load_state()["topics"][0].get("hash") is None


def test_adopting_marks_records_logs_and_the_next_check_is_green(monkeypatch):
    save_state({"topics": [_unmarked_topic()]})
    client = Adoptable()
    client.put(NEW, "/media/tv", tags=["mine"])
    _wire(monkeypatch, {"main": client}, Tracker())
    result = adopt.adopt_topic("t1", how="manual")
    assert result == {"id": "t1", "hash": NEW, "client_id": "main", "already": False}
    assert client.torrents[NEW]["tags"] == ["mine", "tow"]
    saved = load_state()["topics"][0]
    assert (saved["hash"], saved["selection_verified"]) == (NEW, False)
    assert saved["last_error_code"] == "check.not_owned_existing"  # a check, not the action, ends it
    (event,) = [e for e in _events() if e["kind"] == "client_adopted"]
    assert (event["topic"], event["hash"], event["status"]) == ("t1", NEW, "succeeded")

    row = check.run_check(apply=True, notify=False, how="test")["results"][0]
    saved = load_state()["topics"][0]
    assert row["ok"] is True
    assert not saved.get("last_error")
    assert client.adds == []
    assert not getattr(client, "configured", False)  # "all files": its selection stays as it was


def test_an_old_revision_is_kept_as_a_previous_one(monkeypatch):
    save_state({"topics": [_unmarked_topic(hash=OLD)]})
    client = Adoptable()
    client.put(NEW, "/media/tv", tags=[])
    _wire(monkeypatch, {"main": client}, Tracker())
    adopt.adopt_topic("t1", how="manual")
    saved = load_state()["topics"][0]
    assert (saved["hash"], saved["previous_hashes"]) == (NEW, [OLD])


def test_a_torrent_no_longer_in_the_client_is_not_adopted(monkeypatch):
    save_state({"topics": [_unmarked_topic()]})
    _wire(monkeypatch, {"main": Adoptable()}, Tracker())
    with pytest.raises(adopt.AdoptError) as raised:
        adopt.adopt_topic("t1", how="manual")
    assert raised.value.code == "adopt.missing"
    assert load_state()["topics"][0].get("hash") is None
    assert [e["kind"] for e in _events()] == ["client_adopt_failed"]


def test_nothing_is_adopted_while_a_check_runs(monkeypatch):
    from tow.store import CheckBusyError

    save_state({"topics": [_unmarked_topic()]})
    client = Adoptable()
    client.put(NEW, "/media/tv", tags=[])
    _wire(monkeypatch, {"main": client}, Tracker())
    with check_run_lock(), pytest.raises(CheckBusyError):
        adopt.adopt_topic("t1", how="manual")
    assert client.torrents[NEW]["tags"] == []


def test_a_check_never_adopts_on_its_own(monkeypatch):
    save_state({"topics": [_topic(hash=None)]})
    client = Adoptable()
    client.put(NEW, "/media/tv", tags=[])
    _wire(monkeypatch, {"main": client}, Tracker())
    for _ in range(2):
        check.run_check(apply=True, notify=True, how="auto")
    assert client.torrents[NEW]["tags"] == []


# --- the row's button ----------------------------------------------------------------------------


def test_the_row_offers_adoption_only_for_an_unmarked_torrent():
    save_state({"topics": [_unmarked_topic(), _topic(id="t2", hash=NEW, last_ok=True)]})
    browser = TestClient(app, headers=ORIGIN)
    panel = browser.get("/topics/t1/edit-panel").text
    assert "/topics/t1/adopt" in panel
    assert "Взять под управление TOW" in panel
    assert "/topics/t2/adopt" not in browser.get("/topics/t2/edit-panel").text


def test_the_button_adopts_then_checks_the_topic(monkeypatch):
    save_state({"topics": [_unmarked_topic()]})
    adopted, checks = [], []
    monkeypatch.setattr("tow.web.services.adopt_topic", lambda tid, how: adopted.append((tid, how)))
    monkeypatch.setattr(
        "tow.web.services.run_check",
        lambda **kw: checks.append(kw["ids"]) or {"qbit": "ok", "results": [{"id": "t1", "ok": True}]},
    )
    response = TestClient(app, headers=ORIGIN).post("/topics/t1/adopt", follow_redirects=False)
    assert response.status_code == 303
    assert (adopted, checks) == ([("t1", "manual")], [["t1"]])


def test_a_refused_adoption_is_shown(monkeypatch):
    save_state({"topics": [_unmarked_topic()]})

    def refuse(tid, how):
        raise adopt.AdoptError("adopt.missing")

    monkeypatch.setattr("tow.web.services.adopt_topic", refuse)
    monkeypatch.setattr("tow.web.services.run_check", lambda **kw: pytest.fail("no check"))
    response = TestClient(app, headers=ORIGIN).post("/topics/t1/adopt", follow_redirects=False)
    assert "торрента нет в торрент-клиенте" in shown(response.headers["location"])


def test_adoption_needs_the_pages_own_origin():
    save_state({"topics": [_unmarked_topic()]})
    foreign = TestClient(app, headers={"Origin": "http://evil.example"})
    assert foreign.post("/topics/t1/adopt", follow_redirects=False).status_code == 403


# --- tow adopt -------------------------------------------------------------------------------------


OTHER = "C" * 40


def _two_unmarked(monkeypatch) -> Adoptable:
    second = _unmarked_topic(OTHER, id="t2", url="https://tracker.example/2")
    save_state({"topics": [_unmarked_topic(), second]})
    client = Adoptable()
    client.put(NEW, "/media/tv", tags=[])
    client.put(OTHER, "/media/tv", tags=[])
    _wire(monkeypatch, {"main": client}, Tracker())
    monkeypatch.setattr("tow.adopt.client_factory.from_secrets", lambda cfg, secrets, client_id=None: client)
    return client


def test_cli_lists_and_adopts_only_after_a_yes(monkeypatch, capsys):
    client = _two_unmarked(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    assert cli.main(["adopt", "--all-unmarked"]) == 0
    assert client.torrents[NEW]["tags"] == []
    assert "t1" in capsys.readouterr().out
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    assert cli.main(["adopt", "--all-unmarked"]) == 0
    assert (client.torrents[NEW]["tags"], client.torrents[OTHER]["tags"]) == (["tow"], ["tow"])
    assert [topic["hash"] for topic in load_state()["topics"]] == [NEW, OTHER]


def test_cli_adopts_named_topics_with_yes_and_never_without_a_choice(monkeypatch):
    client = _two_unmarked(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda _prompt: pytest.fail("asked"))
    assert cli.main(["adopt"]) == cli.EXIT_USAGE
    assert cli.main(["adopt", "t1", "--yes", "--json"]) == 0
    assert (client.torrents[NEW]["tags"], client.torrents[OTHER]["tags"]) == (["tow"], [])


def test_cli_says_what_it_did_in_words_not_fields(monkeypatch, capsys):
    _two_unmarked(monkeypatch)
    assert cli.main(["adopt", "t1", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "t1:" in out
    assert "ok:" not in out
    assert "adopted:" not in out


def test_cli_json_prints_only_json_and_adopts_only_with_yes(monkeypatch, capsys):
    client = _two_unmarked(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda _prompt: pytest.fail("asked"))
    assert cli.main(["adopt", "--all-unmarked", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [item["id"] for item in data["candidates"]] == ["t1", "t2"]
    assert data["adopted"] == []
    assert client.torrents[NEW]["tags"] == []


def test_cli_reports_a_client_that_does_not_answer(monkeypatch, capsys):
    _two_unmarked(monkeypatch)

    def down(cfg, secrets, client_id=None):
        raise TowError("client.managed.no_address")

    monkeypatch.setattr("tow.adopt.client_factory.from_secrets", down)
    assert cli.main(["adopt", "--all-unmarked", "--json"]) == cli.EXIT_CANNOT_RUN
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False
    assert list(data["unreachable"]) == ["main"]


def test_cli_reports_an_unknown_topic(monkeypatch, capsys):
    client = _two_unmarked(monkeypatch)
    assert cli.main(["adopt", "nope"]) == cli.EXIT_CANNOT_RUN
    assert "nope" in capsys.readouterr().out
    assert cli.main(["adopt", "nope", "t1", "--yes"]) == cli.EXIT_PARTIAL
    assert client.torrents[NEW]["tags"] == ["tow"]


def test_cli_without_an_answer_changes_nothing(monkeypatch):
    client = _two_unmarked(monkeypatch)

    def no_terminal(_prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", no_terminal)
    assert cli.main(["adopt", "--all-unmarked"]) == 0
    assert client.torrents[NEW]["tags"] == []
