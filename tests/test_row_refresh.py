"""A topic's Home row, drawn again for checks.js after its check has ended: the same markup as
Home's, escaped, read-only, and asked like any other page (a device on the network signs in)."""

from __future__ import annotations

from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from tow.config import load_config, save_config
from tow.store import load_state, save_state
from tow.web import app

TOPIC = {
    "id": "t1",
    "title": "<b>Harbour</b> Clock / Часовая гавань",
    "url": "http://rutor.info/torrent/1/x",
    "save_path": "M:/a",
    "last_ok": False,
    "last_error": "timeout",
    "last_error_class": "tracker",
}


def _seed():
    save_state({"topics": [TOPIC, {**TOPIC, "id": "t2", "title": "Second", "url": "http://rutor.info/torrent/2/x"}]})


def test_the_row_is_drawn_as_home_draws_it():
    _seed()
    client = TestClient(app)
    home = BeautifulSoup(client.get("/").text, "html.parser").select_one("#row-t1")
    response = client.get("/topics/t1/row")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    row = BeautifulSoup(response.text, "html.parser").select_one("#row-t1")
    assert str(row) == str(home)
    assert len(BeautifulSoup(response.text, "html.parser").select(".row-wrap")) == 1
    assert "<b>Harbour</b>" not in response.text  # the owner's title is text, never markup
    assert "&lt;b&gt;Harbour&lt;/b&gt;" in response.text


def test_the_row_says_what_the_check_found():
    _seed()
    client = TestClient(app)
    assert "row-error" in client.get("/topics/t1/row").text
    state = load_state()
    state["topics"][0].update(last_ok=True, last_error=None, last_error_class=None, last_changed=True)
    save_state(state)
    row = BeautifulSoup(client.get("/topics/t1/row").text, "html.parser")
    assert "row-error" not in str(row)
    assert "new" in row.select_one(".dot.lg")["class"]


def test_an_unknown_topic_has_no_row():
    _seed()
    response = TestClient(app).get("/topics/nope/row")
    assert response.status_code == 404
    assert "row-wrap" not in response.text


def test_asking_for_a_row_writes_nothing():
    _seed()
    before = load_state()
    TestClient(app).get("/topics/t1/row")
    assert load_state() == before


def test_a_device_on_the_network_signs_in_for_a_row_as_for_any_page(monkeypatch):
    _seed()
    cfg = load_config()
    cfg.update(allow_lan=True, bind="0.0.0.0")
    save_config(cfg)
    monkeypatch.setenv("TOW_LAN_AUTH_TOKEN", "t" * 32)
    lan = TestClient(app, client=("192.168.1.7", 50000))
    page = lan.get("/topics/t1/row", headers={"Accept": "text/html"}, follow_redirects=False)
    assert page.status_code == 303
    assert page.headers["location"].startswith("/login")
    fetched = lan.get("/topics/t1/row", headers={"Accept": "application/json"}, follow_redirects=False)
    assert fetched.status_code == 401
