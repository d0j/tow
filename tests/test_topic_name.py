"""A topic's own name, once the owner set it in the edit panel, is shown instead of the site's title."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tow.delivery import compose
from tow.notify import PendingNotification
from tow.records import shown_title
from tow.store import load_state, save_state
from tow.web import app

URL = "http://rutor.info/torrent/1234567/show"
SITE_TITLE = "Сериал А / Show A [S01E01-05 из 10] (2026)"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr("tow.web.services.run_check", lambda **kw: {"qbit": "ok", "results": []})
    save_state(
        {
            "topics": [
                {
                    "id": "t1",
                    "title": "Show A",  # the name it was added with
                    "tracker_title": SITE_TITLE,  # the site's current title, from the last check
                    "url": URL,
                    "save_path": r"D:\TV",
                    "client_id": "default",
                }
            ],
            "mirrors": {},
        }
    )
    return TestClient(app, headers={"Origin": "http://127.0.0.1"})


def _edit(client: TestClient, name: str) -> None:
    response = client.post(
        "/topics/t1/edit",
        data={"title": name, "url": URL, "save_path": r"D:\TV", "client_id": "default"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def _row_name(client: TestClient) -> str:
    page = client.get("/").text
    start = page.index('id="row-t1"')
    return page[start : start + 2000]


def test_a_renamed_topic_shows_its_own_name_on_home_and_in_the_edit_panel(client):
    assert "Сериал А" in _row_name(client)  # not renamed: the site's title

    _edit(client, "My show")

    topic = load_state()["topics"][0]
    assert topic["title"] == "My show"
    assert topic["title_set"] is True
    row = _row_name(client)
    assert 'data-name="my show"' in row  # sort by name
    assert ">My show</span>" in row
    assert 'value="My show"' in client.get("/topics/t1/edit-panel").text


def test_an_unchanged_name_marks_nothing_and_a_cleared_one_returns_to_the_site_title(client):
    _edit(client, SITE_TITLE)  # the panel shows the site's title; saved as it was
    topic = load_state()["topics"][0]
    assert topic["title"] == "Show A"  # History keeps the name it had
    assert "title_set" not in topic

    _edit(client, "My show")
    _edit(client, "")
    topic = load_state()["topics"][0]
    assert topic["title"] == SITE_TITLE
    assert "title_set" not in topic
    assert "Сериал А" in _row_name(client)


def test_a_name_equal_to_the_site_title_is_not_a_custom_name(client):
    _edit(client, "My show")
    _edit(client, SITE_TITLE)
    assert "title_set" not in load_state()["topics"][0]


def test_a_message_names_the_renamed_topic_and_still_reads_episodes_from_the_site():
    topic = {"id": "t1", "title": "My show", "title_set": True, "tracker_title": SITE_TITLE, "url": URL}
    ((text, _, _),) = compose([PendingNotification("added", "op", topic)])
    assert text.startswith("My show — ")
    assert "S01E01–05 из 10" in text
    assert "Сериал А" not in text


@pytest.mark.parametrize(
    ("topic", "shown"),
    [
        ({"title": "Show A", "tracker_title": SITE_TITLE}, SITE_TITLE),  # topics added before: unchanged
        ({"title": "Show A"}, "Show A"),
        ({"title": "My show", "title_set": True, "tracker_title": SITE_TITLE}, "My show"),
        ({"title": " ", "title_set": True, "tracker_title": SITE_TITLE}, SITE_TITLE),
    ],
)
def test_shown_title(topic, shown):
    assert shown_title(topic) == shown
