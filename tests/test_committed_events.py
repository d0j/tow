"""A success event is logged only once its change is committed: Recent messages (shared by every
device) and History must never say "edited", "removed" or "undone" for a save that failed or was
refused. Only APIs older than the shared messages are used, so these fail on 1.32.3."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tow import log, store_transaction
from tow.store import StoreWriteError, load_state, save_state
from tow.web import app

BASE = "http://127.0.0.1:8787"
TOPIC = {"id": "t1", "title": "Show A", "url": "http://rutor.info/torrent/1/x", "save_path": "D:/TV"}


def _client() -> TestClient:
    return TestClient(app, base_url=BASE, headers={"Origin": BASE})


def _kinds() -> list[str]:
    return [str(event.get("kind")) for event in log.read_events(limit=200)]


def _failing_save(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(_state: object) -> None:
        raise StoreWriteError(OSError(28, "No space left on device"))

    monkeypatch.setattr("tow.web.services.save_state", refuse)


def test_an_edit_whose_save_failed_is_not_logged_as_edited(monkeypatch):
    save_state({"topics": [dict(TOPIC)]})
    _failing_save(monkeypatch)
    form = {"title": "Renamed", "url": TOPIC["url"], "save_path": TOPIC["save_path"], "check_interval_min": ""}
    answer = _client().post("/topics/t1/edit", data=form, follow_redirects=False)
    assert answer.status_code == 303
    assert load_state()["topics"][0]["title"] == "Show A"
    assert "topic_edit" not in _kinds()


def test_a_saved_edit_is_logged_with_the_folder_it_has():
    save_state({"topics": [dict(TOPIC)]})
    form = {"title": "Renamed", "url": TOPIC["url"], "save_path": TOPIC["save_path"], "check_interval_min": ""}
    assert _client().post("/topics/t1/edit", data=form, follow_redirects=False).status_code == 303
    assert load_state()["topics"][0]["title"] == "Renamed"
    (edited,) = [event for event in log.read_events() if event["kind"] == "topic_edit"]
    assert edited["title"] == "Renamed"
    assert edited["path"] == TOPIC["save_path"]


def test_a_removal_whose_save_failed_is_not_logged_as_removed(monkeypatch):
    save_state({"topics": [dict(TOPIC)]})
    _failing_save(monkeypatch)
    _client().post("/topics/t1/delete", follow_redirects=False)
    assert [topic["id"] for topic in load_state()["topics"]] == ["t1"]
    assert "topic_delete" not in _kinds()


def test_an_undo_that_failed_to_commit_is_not_logged_as_undone(monkeypatch):
    save_state({"topics": [dict(TOPIC)]})
    client = _client()
    assert client.post("/topics/t1/delete", follow_redirects=False).status_code == 303
    stamp = load_state()["undo"]["ts"]

    def broken(*_args: object, **_kwargs: object) -> list[str]:
        raise store_transaction.TransactionError("the disk is full")

    monkeypatch.setattr("tow.undo.engine._write", broken)
    client.post("/undo", data={"stamp": stamp}, follow_redirects=False)
    kinds = _kinds()
    assert "undo_fail" in kinds
    assert "undo" not in kinds
    assert load_state()["topics"] == []


def test_a_committed_undo_is_logged_after_it_and_names_its_kind():
    save_state({"topics": [dict(TOPIC)]})
    client = _client()
    client.post("/topics/t1/delete", follow_redirects=False)
    stamp = load_state()["undo"]["ts"]
    assert client.post("/undo", data={"stamp": stamp}, follow_redirects=False).status_code == 303
    assert [topic["id"] for topic in load_state()["topics"]] == ["t1"]
    undone = next(event for event in log.read_events() if event["kind"] == "undo")
    assert undone["undo"] == "topic"
