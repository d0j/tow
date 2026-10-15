"""D10: a Check pressed with the page's script is registered by its action id before any work and
answered at once with an acknowledgement; its result is looked up by that id (also when the
acknowledgement was lost) and comes back as one message record. Without a script, the native
POST and its 303 work as before."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from helpers import manual_check_consumers

from tow.config import load_config, save_config
from tow.store import save_state
from tow.web import app, check_jobs

ORIGIN = {"Origin": "http://127.0.0.1"}
FETCH = {**ORIGIN, "X-TOW-Fetch": "1"}
TITLE = "Harbour Clock / Часовая гавань"


@pytest.fixture
def client(monkeypatch):
    cfg = load_config()
    cfg["language"] = "en"
    save_config(cfg)
    save_state(
        {
            "topics": [
                {"id": "t1", "title": TITLE, "url": "http://rutor.info/torrent/1/x", "save_path": "M:/a"},
                {"id": "t2", "title": "Second Show", "url": "http://rutor.info/torrent/2/x", "save_path": "M:/a"},
            ]
        }
    )
    return TestClient(app, headers=ORIGIN)


@pytest.fixture
def consumers(monkeypatch):
    return manual_check_consumers(monkeypatch)


def _checks(monkeypatch, rows_by_id):
    calls = []

    def run_check(**kw):
        calls.append(kw)
        ids = kw.get("ids") or list(rows_by_id)
        titles = {"t1": TITLE, "t2": "Second Show"}
        return {
            "results": [
                {"id": tid, "title": titles.get(tid, ""), **rows_by_id[tid]} for tid in ids if tid in rows_by_id
            ]
        }

    monkeypatch.setattr("tow.web.services.run_check", run_check)
    return calls


def test_a_scripted_check_is_acknowledged_at_once_and_registered_before_work(client, consumers, monkeypatch):
    calls = _checks(monkeypatch, {"t1": {"ok": True}})
    response = client.post("/topics/t1/check", data={"action_id": "A1"}, headers=FETCH)
    assert response.status_code == 200
    ack = response.json()
    assert ack["accepted"] is True
    assert ack["action"] == "A1"
    assert ack["status"] in {"queued", "running"}
    assert calls == []  # nothing ran in the request: the queue's worker runs it
    job = ack["job"]
    assert check_jobs.by_action("A1")["id"] == job
    message = ack["message"]
    assert (message["kind"], message["subject"], message["id"]) == ("info", "Harbour Clock", f"check-{job}")
    assert "redirect" not in ack  # the page stays where it is

    waiting = client.get("/check/status?action=A1").json()
    assert (waiting["status"], waiting["job"], waiting["topic_id"]) == ("queued", job, "t1")
    assert waiting["message"]["kind"] == "info"
    consumers[0].run()
    done = client.get("/check/status?action=A1").json()
    assert done["status"] == "done"
    assert done["message"] == {**done["message"], "id": f"check-{job}", "kind": "ok", "subject": "Harbour Clock"}
    assert done["message"]["text"] == "no changes"


def test_the_same_action_sent_again_is_the_same_check(client, consumers, monkeypatch):
    _checks(monkeypatch, {"t1": {"ok": True}})
    first = client.post("/topics/t1/check", data={"action_id": "A1"}, headers=FETCH).json()
    again = client.post("/topics/t1/check", data={"action_id": "A1"}, headers=FETCH).json()
    assert again["job"] == first["job"]
    assert len(check_jobs.JOBS) == 1


def test_two_clicks_on_one_topic_share_its_waiting_check(client, consumers, monkeypatch):
    _checks(monkeypatch, {"t1": {"ok": True}})
    first = client.post("/topics/t1/check", data={"action_id": "A1"}, headers=FETCH).json()
    # The first waits behind an applying check that runs: the second shares it.
    check_jobs.JOBS[first["job"]]["status"] = "queued"
    second = client.post("/topics/t1/check", data={"action_id": "A2"}, headers=FETCH).json()
    assert second["job"] == first["job"]
    assert check_jobs.by_action("A2")["id"] == first["job"]


def test_an_unknown_action_is_an_unconfirmed_result_with_the_history(client):
    body = client.get("/check/status?action=never-sent").json()
    assert body["status"] == "unknown"
    message = body["message"]
    assert message["kind"] == "warn"
    assert message["href"] == "/history"
    assert "unconfirmed" in message["text"]


def test_a_failed_check_names_its_topic_and_offers_the_sign_in(client, consumers, monkeypatch):
    nnm = {
        "title": "NNM-Club",
        "url_regex": r"^https?://nnmclub\.to/forum/viewtopic\.php\?t=(\d+)",
        "fetch_hosts": ["https://nnmclub.to"],
        "login_path": "/forum/login.php",
        "topic_path": "/forum/viewtopic.php?t={id}",
    }
    monkeypatch.setattr("tow.web.services.load_config", lambda: {"trackers": {"nnmclub": nnm}})
    save_state({"topics": [{"id": "t1", "title": TITLE, "url": "https://nnmclub.to/forum/viewtopic.php?t=7"}]})
    _checks(monkeypatch, {"t1": {"ok": False, "error": "sign in", "error_class": "tracker_auth"}})
    ack = client.post("/topics/t1/check", data={"action_id": "A1"}, headers=FETCH).json()
    consumers[0].run()
    message = client.get(f"/check/status?job={ack['job']}").json()["message"]
    assert message["kind"] == "err"  # a sign-in is the owner's to fix: red (AGENTS.md)
    assert message["href"] == "/?credential_topic=t1"


def test_without_a_script_the_native_post_still_answers_with_its_page(client, monkeypatch):
    _checks(monkeypatch, {"t1": {"ok": True}})
    response = client.post("/topics/t1/check", follow_redirects=False)
    assert response.status_code == 303
    assert parse_qs(urlsplit(response.headers["location"]).query)["flash"]


@pytest.mark.parametrize(
    ("rows", "kind"),
    [
        ({"t1": {"ok": True}, "t2": {"ok": True, "changed": True}}, "ok"),
        ({"t1": {"ok": True}, "t2": {"ok": False, "error": "timeout", "error_class": "tracker"}}, "warn"),
        (
            {
                "t1": {"ok": False, "error": "timeout", "error_class": "tracker"},
                "t2": {"ok": False, "error": "refused", "error_class": "qbit"},
            },
            "err",
        ),
    ],
)
def test_check_all_ends_in_one_short_message_by_its_worst_result(client, consumers, monkeypatch, rows, kind):
    _checks(monkeypatch, rows)
    ack = client.post("/check", data={"mode": "apply", "action_id": "ALL"}, headers=FETCH).json()
    assert ack["message"]["subject"] == ""
    consumers[0].run()
    message = client.get("/check/status?action=ALL").json()["message"]
    assert message["kind"] == kind
    assert message["text"] == "check of all topics finished"
    assert message["href"] == f"/?check_result={ack['job']}#check-result"
    assert message["link"] == "View results"


def test_the_following_page_has_no_link_to_a_waiting_result(client, consumers, monkeypatch):
    _checks(monkeypatch, {"t1": {"ok": True}})
    response = client.post("/check", data={"mode": "apply"}, follow_redirects=False)
    job = parse_qs(urlsplit(response.headers["location"]).query)["check_job"][0]
    note = BeautifulSoup(client.get(f"/?check_job={job}").text, "html.parser").select_one("#check-activity")
    assert not note.select("a")
    assert note["data-check-job"] == job
    consumers[0].run()
    note = BeautifulSoup(client.get(f"/?check_job={job}").text, "html.parser").select_one("#check-activity")
    assert note.select_one(f'a[href="/?check_result={job}#check-result"]')  # finished: its results
