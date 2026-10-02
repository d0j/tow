"""A message an action leaves (typed flash): the route says its kind, the server keeps its text."""

import pytest
from fastapi.testclient import TestClient
from helpers import flash_kind, flash_of

from tow.web import app
from tow.web.views import _FLASHES, flash_location


def _page_with(text: str, kind: str) -> str:
    return TestClient(app).get(flash_location("/", text, kind)).text


def test_an_error_is_an_alert_that_stays():
    page = _page_with("не удалось проверить", "err")
    assert '<div class="flash error" id="flash" role="alert">' in page
    assert "Не удалось проверить" in page


def test_a_warning_stays_and_a_success_fades():
    warn = _page_with("сохранено, но не проверено", "warn")
    assert '<div class="flash warn" id="flash" role="status">' in warn
    ok = _page_with("сохранено", "ok")
    assert 'class="flash" id="flash" role="status" data-ttl=' in ok


@pytest.mark.parametrize("text", ["не подходит", "nothing failed here", "not a problem"])
def test_the_words_never_decide_the_colour(text):
    """1.17 coloured a message by its words ("не", "not", ...): a success saying "not" was red."""
    assert 'class="flash" id="flash" role="status"' in _page_with(text, "ok")


def test_the_address_carries_a_token_not_the_text():
    location = flash_location("/settings?open=access#x", "секрет в адресе? нет", "warn")
    assert "секрет" not in location
    assert location.startswith("/settings?open=access&flash=")
    assert location.endswith("#x")
    assert flash_of(location) == "секрет в адресе? нет"
    assert flash_kind(location) == "warn"


@pytest.mark.parametrize("query", ["flash=сохранено", "flash=not-a-token", "flash="])
def test_an_unknown_token_or_an_old_text_link_shows_nothing(query):
    page = TestClient(app).get("/?" + query).text
    assert 'id="flash"' not in page


def test_the_store_is_bounded_and_expires(monkeypatch):
    from tow.web import views

    first = flash_location("/", "первое", "ok")
    for index in range(_FLASHES.limit):
        flash_location("/", f"сообщение {index}", "ok")
    assert flash_of(first) == ""  # the oldest went first
    later = flash_location("/", "позднее", "ok")
    clock = views.time.monotonic() + _FLASHES.ttl + 1
    monkeypatch.setattr(views.time, "monotonic", lambda: clock)
    assert flash_of(later) == ""


def test_a_restart_forgets_messages_and_drafts_and_the_page_still_opens(monkeypatch):
    """Messages and refused-add drafts live in the web process's memory only (by design): after a
    restart the link they left opens the page without them, never an error."""
    from tow.web.views import _ADD_DRAFTS

    monkeypatch.setattr("tow.web.services.run_check", lambda **_kw: {"results": []})
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    message = flash_location("/", "сохранено", "ok")
    refused = client.post("/topics/add", data={"url": "https://unknown.example/1"}, follow_redirects=False)
    assert refused.headers["location"].startswith("/?add=")
    assert "unknown.example" in client.get(refused.headers["location"]).text  # the draft, before

    _FLASHES.clear()  # what a restart of the web server does to them
    _ADD_DRAFTS.clear()

    for location in (message, refused.headers["location"]):
        page = client.get(location)
        assert page.status_code == 200
        assert 'id="flash"' not in page.text
        assert "unknown.example" not in page.text


def test_routes_say_the_kind_of_their_message():
    from tow.store import save_state

    save_state(
        {"topics": [{"id": "t1", "title": "Show", "url": "http://rutor.info/torrent/1/x", "save_path": "M:\\a"}]}
    )
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    paused = client.post("/topics/t1/pause", follow_redirects=False).headers["location"]
    missing = client.post("/topics/nope/pause", follow_redirects=False).headers["location"]
    gone = client.post("/topics/nope/delete", follow_redirects=False).headers["location"]
    assert (flash_kind(paused), flash_kind(missing), flash_kind(gone)) == ("ok", "err", "warn")


def test_no_route_writes_a_message_into_an_address():
    """Every message goes through the server-side store (tow.web.views.flash_location)."""
    from pathlib import Path

    src = Path(__file__).parents[1] / "src" / "tow"
    for path in [*(src / "web").glob("*.py"), src / "static" / "app.js"]:
        if path.name != "views.py":
            assert "flash=" not in path.read_text(encoding="utf-8"), path.name


def test_a_catalog_key_is_rendered_in_the_pages_language():
    location = flash_location("/", "web.common.saved", "ok")
    assert flash_of(location) == "сохранено"
