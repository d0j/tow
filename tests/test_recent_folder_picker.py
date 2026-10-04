"""Owner-device folder permissions and ten full-path suggestions; no real client changes."""

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from tow.auth import issue_session
from tow.config import load_config, save_config
from tow.store import load_state, save_state
from tow.web import app


@pytest.fixture
def lan_owner(monkeypatch):
    cfg = load_config()
    cfg.update(bind="0.0.0.0", allow_lan=True)
    save_config(cfg)
    monkeypatch.setenv("TOW_LAN_AUTH_TOKEN", "s" * 32)
    monkeypatch.setattr("tow.title.guess_topic_title", lambda *args, **kw: "Synthetic series")
    monkeypatch.setattr("tow.web.services.run_check", lambda **kw: {"qbit": "ok", "results": []})
    return TestClient(
        app,
        client=("192.168.1.9", 50000),
        headers={"Origin": "http://127.0.0.1"},
        cookies={"tow_session": issue_session("s" * 32)},
    )


@pytest.mark.parametrize("path", [r"E:\Cartoons\Season 2", "/downloads/cartoons/season-2"])
def test_lan_owner_adds_and_edits_new_full_paths(lan_owner, path):
    response = lan_owner.post(
        "/topics/add",
        data={"url": "http://rutor.info/torrent/31/test", "title": "Synthetic series", "save_path": path},
        follow_redirects=False,
    )
    assert response.status_code == 303
    topic = load_state()["topics"][0]
    assert topic["save_path"] == path
    assert load_state()["save_roots"][0] == path
    new_path = path + "/subfolder"
    response = lan_owner.post(f"/topics/{topic['id']}/edit", data={"save_path": new_path}, follow_redirects=False)
    assert response.status_code == 303
    assert load_state()["topics"][0]["save_path"] == new_path
    assert load_state()["save_roots"][:2] == [new_path, path]


@pytest.mark.parametrize(
    "path",
    [r"C:\Windows\System32", "/etc/cron.d", r"\\nas\new-share", "relative", r"E:\Media\..\other", r"\\?\E:\Media"],
)
def test_lan_folder_freedom_keeps_path_safety_before_any_save(lan_owner, path):
    before = load_state()
    response = lan_owner.post(
        "/topics/add",
        data={"url": "http://rutor.info/torrent/32/test", "title": "Synthetic series", "save_path": path},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert load_state() == before


def test_folder_permission_does_not_allow_anonymous_or_cross_origin_add(lan_owner):
    anonymous = TestClient(app, client=("192.168.1.9", 50000), headers={"Origin": "http://127.0.0.1"})
    data = {"url": "http://rutor.info/torrent/33/test", "save_path": r"E:\Cartoons"}
    before = load_state()
    response = anonymous.post("/topics/add", data=data, follow_redirects=False)
    assert response.status_code == 401
    response = lan_owner.post(
        "/topics/add", data=data, headers={"Origin": "https://evil.example"}, follow_redirects=False
    )
    assert response.status_code == 403
    assert load_state() == before


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_folder_suggestions_are_ten_newest_full_paths_and_escaped(lan_owner, lang):
    roots = [rf"E:\media\season-{number}" for number in range(12)]
    roots[0] = r'E:\media\<folder>&"'
    save_state(
        {
            "topics": [
                {
                    "id": "demo",
                    "url": "http://rutor.info/torrent/34/test",
                    "title": "Synthetic series",
                    "save_path": roots[1],
                }
            ],
            "save_roots": roots,
        }
    )
    for url in ("/", "/topics/demo/edit"):
        response = lan_owner.get(url, headers={"Accept-Language": lang})
        assert response.status_code == 200
        document = BeautifulSoup(response.text, "html.parser")
        assert [row["value"] for row in document.select("#save-roots option")] == roots[:10]
        control = document.select_one('.folder-input input[name="save_path"]')
        assert control is not None
        assert control.get("list") == "save-roots"  # native no-script fallback
        button = document.select_one("[data-save-folders]")
        assert button is not None
        assert button["type"] == "button"
        assert button.get("aria-label")
        assert document.select_one("folder") is None
    panel = BeautifulSoup(lan_owner.get("/topics/demo/edit-panel").text, "html.parser")
    assert panel.select_one("[data-save-folders]")  # asynchronously loaded editor uses the same widget
