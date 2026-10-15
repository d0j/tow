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
