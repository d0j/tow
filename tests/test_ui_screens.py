"""The compact screens of 1.32 (Home, Sites, History, Setup, Login, Diagnostics): what each form
sends and what each page shows, read from the rendered pages and the routes they post to."""

from __future__ import annotations

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from tow.store import load_state
from tow.web import app

ORIGIN = {"Origin": "http://127.0.0.1"}


@pytest.fixture
def client():
    return TestClient(app, headers=ORIGIN)


def _add(client, monkeypatch, **fields):
    monkeypatch.setattr("tow.web.services.guess_topic_title", lambda _url: "")  # no site is asked
    monkeypatch.setattr("tow.web.services.run_check", lambda **kw: {"qbit": "ok", "results": []})
    data = {"url": "http://rutor.info/torrent/101/show", "title": "Show", "save_path": r"M:\anime", **fields}
    return client.post("/topics/add", data=data, follow_redirects=False)


@pytest.mark.parametrize(
    ("fields", "mode"),
    [
        ({"tracking_mode": "watch", "tracking_unchecked": "once"}, "watch"),  # the box ticked
        ({"tracking_unchecked": "once"}, "once"),  # the box cleared: a checkbox sends nothing
        ({"tracking_mode": "once"}, "once"),  # the payload of 1.31 and of scripts
        ({}, "watch"),  # neither: the default stays "follow"
    ],
)
def test_follow_is_a_checkbox_that_keeps_the_watch_and_once_payload(client, monkeypatch, fields, mode):
    """Follow the topic was a yes/no select; a cleared checkbox sends no value at all, so the
    form names what a cleared box means and the route keeps reading watch/once."""
    assert _add(client, monkeypatch, **fields).status_code == 303
    assert load_state()["topics"][0]["tracking_mode"] == mode


def test_the_add_form_offers_follow_as_one_checked_box(client):
    page = BeautifulSoup(client.get("/").text, "html.parser")
    box = page.select_one("#topic-tracking-mode")
    assert (box.name, box["type"], box["name"], box["value"]) == ("input", "checkbox", "tracking_mode", "watch")
    assert box.has_attr("checked")
    assert page.select_one('#new input[type="hidden"][name="tracking_unchecked"]')["value"] == "once"
    assert page.select_one('label[for="topic-tracking-mode"]') is not None


@pytest.mark.parametrize(
    ("error", "line"),
    [
        ("qBittorrent: connection refused", "qBittorrent: connection refused"),
        ("Transmission: connection refused", "Transmission: connection refused"),
        ("connection refused", "Торрент-клиент: connection refused"),  # no name: the class says whose
    ],
)
def test_a_client_error_names_its_client_once(error, line):
    """The row said "Торрент-клиент: qBittorrent: connection refused": the class and the client
    named the same thing twice. A detail that starts with its client's name keeps only that, as
    written ("qBittorrent", never "QBittorrent")."""
    from tow.web.views import _error_line

    assert _error_line(error, "qbit") == line


def test_the_sign_in_dialog_names_the_site_and_says_where_the_password_goes(client):
    """The dialog said "Sign-in needed" over the topic's name, and "the password never goes into
    TOW's data" beside "Save and retry". It now names the site, says the password is stored
    encrypted and offers the shared eye for what is typed."""
    from tow.store import save_state

    url = "https://rutracker.org/forum/viewtopic.php?t=1234567"
    save_state({"topics": [{"id": "rt", "title": "Сериал А", "url": url, "last_error_class": "tracker_auth"}]})
    dialog = BeautifulSoup(client.get("/?credential_topic=rt").text, "html.parser").select_one("dialog")
    assert dialog.select_one("h2").get_text() == "Вход на RuTracker"
    assert dialog.select_one("p.sub").get_text() == "Сериал А"
    assert "Введите логин и пароль" not in dialog.get_text()
    password = dialog.select_one('input[name="password"]')
    assert (password["type"], password["autocomplete"]) == ("password", "current-password")
    eye = password.find_parent(class_="secret").select_one("button.reveal")
    assert (eye["type"], eye["data-reveal"], eye["aria-controls"]) == ("button", password["id"], password["id"])
    assert eye["aria-pressed"] == "false"
    note = dialog.select_one(f"#{password['aria-describedby']}").get_text()
    assert note == "Хранится зашифрованным, не показывается и не пишется в журнал."


def test_a_cleared_follow_box_is_saved_as_once_on_edit(client, monkeypatch):
    assert _add(client, monkeypatch).status_code == 303
    topic = load_state()["topics"][0]
    panel = BeautifulSoup(client.get(f"/topics/{topic['id']}/edit-panel").text, "html.parser")
    box = panel.select_one(f"#edit-tracking-mode-{topic['id']}")
    assert (box["type"], box["value"]) == ("checkbox", "watch")
    assert box.has_attr("checked")
    edit = {"title": "Show", "url": topic["url"], "save_path": topic["save_path"], "tracking_unchecked": "once"}
    assert client.post(f"/topics/{topic['id']}/edit", data=edit, follow_redirects=False).status_code == 303
    assert load_state()["topics"][0]["tracking_mode"] == "once"
    panel = BeautifulSoup(client.get(f"/topics/{topic['id']}/edit-panel").text, "html.parser")
    assert not panel.select_one(f"#edit-tracking-mode-{topic['id']}").has_attr("checked")
