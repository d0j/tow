"""A topic's Home row has one writer: ``POST /topics/rows`` (``static/sync.js``), bound to the topics
revision the page has. The old revision-unaware ``GET /topics/<id>/row`` is gone; that the rows the
sync draws are Home's own markup is pinned in ``test_web_sync.py``."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tow.store import save_state
from tow.web import app

TOPIC = {
    "id": "t1",
    "title": "Show A",
    "url": "http://rutor.info/torrent/1/x",
    "save_path": "M:/a",
    "last_ok": True,
}


def test_an_existing_topic_has_no_row_route_of_its_own():
    # The topic exists, so a 404 here is the missing route, not the missing topic.
    save_state({"topics": [TOPIC]})
    client = TestClient(app)
    assert 'id="row-t1"' in client.get("/").text
    response = client.get("/topics/t1/row")
    assert response.status_code == 404
    assert "row-wrap" not in response.text
