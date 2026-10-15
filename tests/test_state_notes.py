"""Persistent state notes: one component (``.notice``: neutral, amber ``warn``, red ``bad``) with the
colours of the messages, the severity and identity of the issue it says, and the role of a note
(a state shown on a page, not an alert said again on every visit)."""

from __future__ import annotations

import re
import time
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from tow.store import save_secrets, save_state
from tow.web import app, services

STATIC = Path(__file__).parents[1] / "src" / "tow" / "static"
TEMPLATES = Path(__file__).parents[1] / "src" / "tow" / "templates"
CSS = (STATIC / "app.css").read_text(encoding="utf-8")


def _rules(css: str) -> dict[str, str]:
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    found: dict[str, str] = {}
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        found.setdefault(" ".join(match.group(1).split()), match.group(2))
    return found


@pytest.fixture
def client():
    save_secrets({"qbittorrent": {"host": "127.0.0.1", "port": 8080, "username": "admin", "password": "pw"}})
    return TestClient(app)


def _state(*topics, **health) -> None:
    save_state({"topics": list(topics), "mirrors": {}, "health": {"auto_at_ts": int(time.time()), **health}})


def _page(client: TestClient, path: str) -> BeautifulSoup:
    response = client.get(path)
    assert response.status_code == 200
    return BeautifulSoup(response.text, "html.parser")


def test_one_red_for_failures_shared_by_notes_and_messages():
    rules = _rules(CSS)
    assert "--notice-err-" not in CSS  # the second red (#281717 in the dark) is gone
    red = rules[".notice.bad"]
    assert "var(--flash-err-bg)" in red
    assert "var(--flash-err-line)" in red
    assert "var(--flash-err-bg)" in rules[".flash.error"]
    amber = rules[".notice.warn"]
    assert "var(--pill-warn-bg)" in amber
    assert "var(--warn)" in amber
    assert "var(--pill-warn-bg)" in rules[".flash.warn"]
    assert "var(--notice-bg)" in rules[".notice"]  # neutral by default
    assert ".notice.error" not in rules


def test_issue_notes_carry_their_severity_and_identity(client, monkeypatch):
    from tow.config import load_config
    from tow.store import load_state
    from tow.web.views import issue_notes

    _state(auto_at_ts=int(time.time()) - 10 * 24 * 3600)
    monkeypatch.setattr(services, "data_lost", lambda: {"at": 1, "folder": "D:\\TOW\\data"})
    notes = {note["issue"]: note for note in issue_notes(load_state(), load_config(), "settings")}
    assert notes["check_stale"]["severity"] == "warn"
    assert notes["data_lost"]["severity"] == "bad"
    assert notes["data_lost"]["at"] == "backup-night"


def test_settings_issue_notes_are_one_note_in_their_severity(client, monkeypatch):
    _state(auto_at_ts=int(time.time()) - 10 * 24 * 3600)
    monkeypatch.setattr(services, "data_lost", lambda: {"at": 1, "folder": "D:\\TOW\\data"})
    page = _page(client, "/settings")
    [late] = page.select("#acc-service [data-issue-id]")
    assert late["data-issue-id"] == "check_stale"
    assert late["class"] == ["notice", "warn"]
    assert late["role"] == "note"
    [lost] = page.select("#backup-night [data-issue-id]")
    assert lost["data-issue-id"] == "data_lost"
    assert lost["class"] == ["notice", "bad"]  # data_lost is red: it was always amber
    assert lost["role"] == "note"
    assert "D:\\TOW\\data" in lost.get_text()


def test_history_and_sites_notes_are_the_same_component(client):
    from tow.config import load_config, save_config

    cfg = load_config()
    cfg["trackers"] = {
        "fakeone": {"url_regex": r"^https://one\.example/t/(\d+)", "fetch_hosts": ["https://one.example"]}
    }
    save_config(cfg)
    asks = {"last_error_class": "tracker_auth", "last_error": "The site asks to sign in again."}
    _state({"id": "a", "url": "https://one.example/t/1", **asks}, history_rebuilt_at=int(time.time()))

    history = _page(client, "/history")
    [rebuilt] = history.select("main [data-issue-id]")
    assert rebuilt["id"] == "issue-history_rebuilt"  # the link's landing spot stays
    assert rebuilt["class"] == ["notice", "warn"]
    assert rebuilt["role"] == "note"  # History's had no role, the others were live regions

    sites = _page(client, "/sites")
    [login] = sites.select("#site-row-fakeone [data-issue-id]")
    assert login["data-issue-id"] == "site_needs_login:fakeone"
    assert login["class"] == ["notice", "bad"]
    assert login["role"] == "note"


def test_a_note_lights_up_where_a_link_lands():
    rules = _rules(CSS)
    landing = next(selector for selector in rules if selector.startswith(":is(.row-wrap"))
    assert ".notice" in landing


def test_the_master_key_problem_is_one_red_note_with_its_command(client):
    from tow.config import load_config
    from tow.store import load_state
    from tow.web.views import attention_view

    _state(check_ok=False, check_error="secrets_migration_required")
    page = _page(client, "/settings")
    note = page.select_one("#secret-store")
    assert note["class"] == ["notice", "bad", "secret-store"]  # was a blue message strip
    assert note["role"] == "note"  # was an alert, said again on every visit
    assert not page.select(".flash.secret-store")
    assert note.select_one("a[href='/settings/help#export-import']") is not None  # its instructions
    copy = note.select_one("button[data-copy-text]")
    assert copy["data-copy-text"].endswith(" keys status")
    assert note.select_one("#copy-command-status") is not None
    # Needs attention still names it, and lands on this note.
    [entry] = attention_view(load_state(), load_config())["entries"]
    assert (entry["kind"], entry["severity"]) == ("check_blocked", "bad")


@pytest.mark.parametrize("path", ["/", "/sites", "/history", "/settings"])
def test_an_update_is_the_footer_badge_on_every_page_not_a_strip_on_home(client, path):
    _state()
    page = _page(client, path)
    assert page.select_one("[data-release-notice]") is None  # Home's permanent green strip is gone
    assert page.select_one(".release-notice") is None
    badge = page.select_one("footer.app-version [data-release-badge]")
    assert badge is not None
    assert "open=updates" in badge["href"]
    assert not badge.has_attr("aria-live")  # an available update is not announced


def test_the_update_scripts_and_styles_know_no_home_strip():
    assert "release-notice" not in (STATIC / "updates.css").read_text(encoding="utf-8")
    assert "data-release-notice" not in (STATIC / "updates.js").read_text(encoding="utf-8")


def _two_clients(client: TestClient) -> None:
    client.post("/settings/client/add", data={"kind": "transmission"}, headers={"Origin": "http://127.0.0.1"})


def test_a_client_card_says_its_problem_once_with_a_pill_that_agrees(client):
    """D5: a failed Check left two red texts about one fact in the card (the message and a red
    .form-error) beside a grey "Set up" pill. The card keeps one note - the client_down issue's -
    and its pill says the same."""
    _state(qbit_ok=True, ping_failed={"default": 1})
    page = _page(client, "/settings")
    card = page.select_one("#client-default")
    assert card.select(".form-error") == []
    [note] = card.select("[role=note]")
    assert note["data-issue-id"] == "client_down"
    assert note["class"] == ["notice", "bad"]
    assert "новые версии не добавляются" in note.get_text()
    assert [pill.get_text() for pill in card.select(".integration-pills .pill.bad")] == ["Не отвечает"]
    summary = page.select_one("#acc-clients > summary .pill")
    assert summary["class"] == ["pill", "bad"]


def test_a_second_client_down_has_its_own_note_and_pills(client):
    _two_clients(client)
    _state(qbit_ok=True, clients_ok={"default": True}, ping_failed={"transmission": 1})
    page = _page(client, "/settings")
    assert page.select("#client-default [role=note]") == []
    assert page.select_one("#client-default .integration-pills .pill.bad") is None
    [note] = page.select("#client-transmission [role=note]")
    assert note["data-issue-id"] == "client_down:transmission"
    assert page.select_one("#client-transmission .integration-pills .pill.bad").get_text() == "Не отвечает"
    summary = page.select_one("#acc-clients > summary .pill")
    assert summary["class"] == ["pill", "bad"]  # was green: the header's verdict on the main client only
    assert "Transmission" in summary.get_text()


def test_a_client_that_answers_has_no_note(client):
    _state(qbit_ok=True, clients_ok={"default": True})
    page = _page(client, "/settings")
    assert page.select("#client-default [role=note]") == []
    assert page.select_one("#client-default .integration-pills .pill.bad") is None


SETTINGS_SOURCES = ("settings.html", "_settings_protection.html", "_topic_edit.html")


@pytest.mark.parametrize("name", SETTINGS_SOURCES)
def test_lasting_states_are_notes_not_alerts_or_field_errors(name):
    """Sol 7 / audit 3.5: static state blocks used role status (client, messengers, autostart),
    alert (backups, the master key) and note (the topic's last error) for the same kind of content,
    and .form-error - the field validation style - for states. They are notes now; .form-error is
    left to field validation, role alert to what a submit refused."""
    source = (TEMPLATES / name).read_text(encoding="utf-8")
    assert "form-error" not in source
    assert 'role="alert"' not in source
    assert "notice error" not in source
    assert "notice neutral" not in source
    static_status = [
        line.strip()[:80]
        for line in source.splitlines()
        if 'role="status"' in line
        and not any(live in line for live in ("data-", "unsaved-state", "copy-command-status", "sr-only"))
    ]
    assert static_status == []  # a live region only where a script changes the words


def test_backup_failures_are_red_notes_beside_the_lost_data_note(client, monkeypatch):
    """D7: the night copies card showed an amber issue note beside a red .form-error (role alert):
    two styles for two different failures. Both are kept, in one component, each in its colour."""
    _state()
    monkeypatch.setattr(services, "data_lost", lambda: {"at": 1, "folder": "D:\\TOW\\data"})
    monkeypatch.setattr(
        "tow.web.routes_settings.backup_view",
        lambda cfg, request: {**_real_backup_view(cfg, request), "failed": True, "error": "disk full", "error_at": "x"},
    )
    card = _page(client, "/settings").select_one("#backup-night")
    notes = card.select(".notice")
    assert [(n.get("data-issue-id"), n["class"]) for n in notes] == [
        ("data_lost", ["notice", "bad"]),
        (None, ["notice", "bad"]),
    ]
    assert "disk full" in notes[1].get_text()


def _real_backup_view(cfg, request):
    from tow.web.views import backup_view

    return backup_view(cfg, request)
