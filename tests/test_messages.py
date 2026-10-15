"""One message per action: a record with its kind, its subject (the topic, site or client it is
about) and its text, shown as "{subject} — {result}" exactly once (the unified message strip)."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from helpers import flash_record

from tow.config import load_config, save_config
from tow.store import load_state, save_state
from tow.web import app
from tow.web.views import flash_location, site_title_of

ORIGIN = {"Origin": "http://127.0.0.1"}
RUTOR_URL = "http://rutor.info/torrent/101/show"
TITLE = "Harbour Clock / Часовая гавань [2024]"
SUBJECT = "Harbour Clock"


@pytest.fixture
def client():
    cfg = load_config()
    cfg["language"] = "en"
    save_config(cfg)
    save_state({"topics": [{"id": "t1", "title": TITLE, "url": RUTOR_URL, "save_path": "M:/anime"}]})
    return TestClient(app, headers=ORIGIN)


def _record(response) -> dict:
    assert response.status_code == 303, response.text
    record = flash_record(response.headers["location"])
    assert record is not None
    return record


def _strip(client: TestClient, location: str) -> str:
    page = BeautifulSoup(client.get(location).text, "html.parser")
    strip = page.select_one("#flash")
    assert strip is not None
    return strip.select_one(":scope > span").get_text(" ", strip=True)


def test_a_topic_action_names_its_topic_once(client):
    response = client.post("/topics/t1/pause", follow_redirects=False)
    record = _record(response)
    assert record["subject"] == SUBJECT
    assert record["text"] == "topic paused: scheduled checks skip it"
    assert _strip(client, response.headers["location"]) == f"{SUBJECT} — topic paused: scheduled checks skip it"


def test_a_deleted_topic_is_named_as_it_was_before_the_deletion(client):
    record = _record(client.post("/topics/t1/delete", follow_redirects=False))
    assert load_state()["topics"] == []
    assert record["subject"] == SUBJECT


def test_a_site_action_names_its_site(client):
    response = client.post("/sites/rutor/freeze", follow_redirects=False)
    assert _record(response)["subject"] == site_title_of("rutor")


def test_a_site_probe_says_its_site_once(client, monkeypatch):
    report = {"probes": [{"tracker": "rutor", "ok": True}, {"tracker": "rutor", "ok": False}]}
    monkeypatch.setattr("tow.web.services.doctor_report", lambda **_kw: report)
    record = _record(client.post("/sites/rutor/probe", follow_redirects=False))
    assert record["subject"] == site_title_of("rutor")
    assert site_title_of("rutor") not in record["text"]


def test_a_global_message_has_no_subject(client):
    response = client.post("/check/nothing/cancel", follow_redirects=False)
    record = _record(response)
    assert record["subject"] == ""
    assert _strip(client, response.headers["location"]) == record["text"][:1].upper() + record["text"][1:]


def test_an_unknown_topic_has_no_subject(client):
    assert _record(client.post("/topics/gone/pause", follow_redirects=False))["subject"] == ""


def test_a_failed_check_names_its_topic_once(client, monkeypatch):
    row = {"id": "t1", "title": TITLE, "ok": False, "error": "timeout", "error_class": "tracker"}
    monkeypatch.setattr("tow.web.services.run_check", lambda **_kw: {"results": [row]})
    response = client.post("/topics/t1/check", follow_redirects=False)
    record = _record(response)
    assert record["subject"] == SUBJECT
    assert SUBJECT not in record["text"]


def test_a_queued_check_keeps_the_name_its_topic_had_when_asked_for(client, monkeypatch):
    from helpers import manual_check_consumers

    from tow.store import CheckBusyError
    from tow.web import check_jobs
    from tow.web.views import check_result_flash

    consumers = manual_check_consumers(monkeypatch)
    calls = []

    def check(**_kw):
        calls.append(1)
        if len(calls) == 1:
            raise CheckBusyError("busy")
        return {"results": []}  # deleted while the check waited

    monkeypatch.setattr("tow.web.services.run_check", check)
    response = client.post("/topics/t1/check", follow_redirects=False)
    job = parse_qs(urlparse(response.headers["location"]).query)["check_job"][0]
    save_state({"topics": []})
    consumers[0].run()
    said = check_result_flash(check_jobs.get(job))
    assert said is not None
    assert (said["subject"], said["kind"], said["id"]) == (SUBJECT, "err", f"check-{job}")


def test_the_record_has_a_stable_identity_and_goes_whole_to_a_script(client):
    response = client.post("/topics/t1/pause", headers={"X-TOW-Fetch": "1"})
    body = response.json()
    location = body["redirect"]
    token = parse_qs(urlparse(location).query)["flash"][0]
    assert body["flash"]["subject"] == SUBJECT
    assert body["flash"]["kind"] == "ok"
    assert body["flash"]["id"]
    assert body["flash"]["id"] != token
    page = BeautifulSoup(client.get(location).text, "html.parser")
    assert page.select_one("#flash")["data-message-id"] == body["flash"]["id"]


def test_an_explicit_subject_wins_and_an_empty_one_opts_out(client):
    assert flash_record(flash_location("/", "web.common.saved", "ok", subject="Kodi"))["subject"] == "Kodi"
    assert flash_record(flash_location("/", "web.common.saved", "ok", subject=""))["subject"] == ""


def test_a_subject_already_at_the_start_is_not_said_twice(client):
    location = flash_location("/", f"{SUBJECT} — no changes", "ok", subject=SUBJECT)
    assert _strip(client, location) == f"{SUBJECT} — no changes"
