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
    return strip.select_one(".msg-text").get_text(" ", strip=True)


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


def test_a_failed_check_is_shown_and_its_topic_stays_in_needs_attention(client, monkeypatch):
    row = {"id": "t1", "title": TITLE, "ok": False, "error": "timeout", "error_class": "tracker"}
    state = load_state()
    state["topics"][0].update(last_ok=False, last_error="timeout", last_error_class="tracker")
    save_state(state)
    monkeypatch.setattr("tow.web.services.run_check", lambda **_kw: {"results": [row]})
    location = client.post("/topics/t1/check", follow_redirects=False).headers["location"]
    page = BeautifulSoup(client.get(location).text, "html.parser")
    strip = page.select_one("#flash")
    assert strip is not None
    assert strip.get_text(" ", strip=True).count(SUBJECT) == 1
    assert strip.get_text(" ", strip=True).startswith(f"{SUBJECT} — ")
    assert not page.select("[data-problem-result]")
    assert page.select_one("[data-attention-count]").get_text() == "1"


def _queued(client, monkeypatch, check):
    from helpers import manual_check_consumers

    from tow.store import CheckBusyError

    consumers = manual_check_consumers(monkeypatch)
    calls = []

    def run(**kw):
        calls.append(1)
        if len(calls) == 1:
            raise CheckBusyError("busy")
        return check(**kw)

    monkeypatch.setattr("tow.web.services.run_check", run)
    response = client.post("/topics/t1/check", follow_redirects=False)
    job = parse_qs(urlparse(response.headers["location"]).query)["check_job"][0]
    consumers[0].run()
    return job


def _broken(**_kw):
    raise ValueError("synthetic")


@pytest.mark.parametrize(
    ("check", "kind"),
    [
        (lambda **_kw: {"results": [{"id": "t1", "title": TITLE, "ok": True}]}, "ok"),
        (lambda **_kw: {"results": [{"id": "t1", "ok": False, "error": "refused", "error_class": "qbit"}]}, "err"),
        (_broken, "err"),
    ],
)
def test_a_queued_topic_check_ends_in_one_message_and_no_result_card(client, monkeypatch, check, kind):
    job = _queued(client, monkeypatch, check)
    status = client.get(f"/check/status?job={job}").json()
    assert "#check-result" not in status["redirect"]
    page = BeautifulSoup(client.get(status["redirect"]).text, "html.parser")
    assert not page.select("#check-result, .check-results")
    strips = page.select("#flash")
    assert len(strips) == 1
    text = strips[0].get_text(" ", strip=True)
    assert text.startswith(f"{SUBJECT} — ")
    assert text.count(SUBJECT) == 1
    assert ("error" in strips[0]["class"]) is (kind == "err")
    assert strips[0]["data-message-id"] == f"check-{job}"


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


def test_the_page_hands_its_message_and_undo_to_the_script(client):
    import re

    client.post("/topics/t1/delete")
    location = flash_location("/", "synthetic", "warn", subject="Show <A>")
    page = BeautifulSoup(client.get(location).text, "html.parser")
    host = page.select_one("main > #msg-host")
    assert host["data-subject-format"] == "{subject} — {result}"
    assert re.fullmatch(r"[0-9a-f]{16}", host["data-namespace"])
    assert host["data-undo-stamp"] == load_state()["undo"]["ts"]
    assert int(host["data-undo-left"]) > 0
    assert host["data-undo-text"].startswith("You can undo ")
    strip = host.select_one("#flash")
    assert (strip["data-kind"], strip["data-subject"], strip["data-text"]) == ("warn", "Show <A>", "synthetic")
    assert strip.select_one(".msg-ico")["aria-label"] == "Warning"
    assert "data-ttl" not in strip.attrs  # its time is the script's, not the Undo setting's
    menu = page.select_one("header [data-messages]")
    assert menu.has_attr("hidden")  # shown by the script, which keeps the history
    assert menu.select_one('summary use[href="#i-message"]')
    assert page.select_one('script[src^="/static/messages.js"]').has_attr("defer")
    # In sight under the sticky header while shown: a page opened at a card further down shows it.
    from pathlib import Path

    css = (Path(__file__).parents[1] / "src" / "tow" / "static" / "messages.css").read_text(encoding="utf-8")
    assert re.search(r"\.msg-host \{ position: sticky; top: var\(--msg-top", css)


@pytest.mark.parametrize(
    ("href", "kept"), [("/?check_result=j1", True), ("https://evil.example/", False), ("//evil.example", False)]
)
def test_a_message_offers_a_page_of_this_tow_only(client, href, kept):
    location = flash_location("/", "check all finished", "warn", href=href, link="View results")
    said = flash_record(location)
    assert (said.get("href"), said.get("link")) == ((href, "View results") if kept else (None, None))
    strip = BeautifulSoup(client.get(location).text, "html.parser").select_one("#flash")
    link = strip.select_one("a.msg-link")
    assert (link["href"] if link else None) == (href if kept else None)
    assert strip.get("data-href") == (href if kept else None)


def test_an_explicit_subject_wins_and_an_empty_one_opts_out(client):
    assert flash_record(flash_location("/", "web.common.saved", "ok", subject="Kodi"))["subject"] == "Kodi"
    assert flash_record(flash_location("/", "web.common.saved", "ok", subject=""))["subject"] == ""


def test_a_subject_already_at_the_start_is_not_said_twice(client):
    location = flash_location("/", f"{SUBJECT} — no changes", "ok", subject=SUBJECT)
    assert _strip(client, location) == f"{SUBJECT} — no changes"


def test_a_skipped_topic_check_is_a_note_not_a_success():
    # Skipped work is "info" (neutral), as queued and deferred work: it did not check anything.
    from tow.web.views import check_feedback

    said = check_feedback({"id": "t1", "ok": True, "status": "skipped", "skipped": "paused"}, topic_id="t1")
    assert said["kind"] == "info"
    assert check_feedback({"id": "t1", "ok": True}, topic_id="t1")["kind"] == "ok"
