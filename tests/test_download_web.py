import json

from fastapi.testclient import TestClient

from tow.store import save_download_history, save_state
from tow.web import app


def test_home_and_download_details_show_progress(tmp_path):
    save_state(
        {
            "topics": [
                {
                    "id": "topic-1",
                    "title": "Show [01x01-01 из 1]",
                    "url": "http://rutor.info/torrent/1/show",
                    "save_path": str(tmp_path),
                    "hash": "ABC123",
                    "client_id": "qbit-main",
                }
            ],
            "mirrors": {},
        }
    )
    save_download_history(
        {
            "schema_version": 1,
            "topics": {
                "topic-1": {
                    "expected": {"kind": "episodes", "total": 1, "confidence": "high"},
                    "summary": {"completed": 1, "expected": 1, "is_complete": True},
                    "last_completed": {
                        "label": "S01E01",
                        "completed_observed_at": "2026-09-12T22:58:40+03:00",
                    },
                    "last_event": {
                        "kind": "torrent_completed",
                        "label": "S01E01",
                        "at": "2026-09-12T22:58:40+03:00",
                    },
                    "items": {
                        "episode:s01e01": {
                            "label": "S01E01",
                            "status": "completed",
                            "completed_observed_at": "2026-09-12T22:58:40+03:00",
                        }
                    },
                }
            },
        }
    )
    c = TestClient(app)
    home = c.get("/")
    assert home.status_code == 200
    assert "1/1" in home.text
    assert "download-details" in home.text
    assert "торрент завершён" not in home.text
    assert "S01E01" in home.text

    details = c.get("/topics/topic-1/downloads.json")
    assert details.status_code == 200
    payload = details.json()
    assert payload["summary"]["is_complete"] is True
    assert payload["last_event"]["kind"] == "torrent_completed"
    assert payload["last_event"]["display"] == "S01E01"
    assert payload["items"][0]["label"] == "S01E01"
    assert "22:58:40" in json.dumps(payload, ensure_ascii=False)


def test_topic_without_torrent_shows_expected_progress_from_title(tmp_path):
    save_state(
        {
            "topics": [
                {
                    "id": "waiting-topic",
                    "title": "Show [2026, TV, 12 из 14]",
                    "url": "https://nnmclub.to/forum/viewtopic.php?t=1",
                    "save_path": str(tmp_path),
                    "hash": None,
                    "last_ok": False,
                    "last_error": "nnmclub: no download link on page",
                }
            ],
            "mirrors": {},
        }
    )
    save_download_history({"schema_version": 1, "topics": {}})
    client = TestClient(app)

    home = client.get("/")
    details = client.get("/topics/waiting-topic/downloads.json")

    assert home.status_code == 200
    assert "0/14" in home.text
    assert details.status_code == 200
    assert details.json()["summary"] == {
        "completed": 0,
        "expected": 14,
        "completion_known": False,
        "is_complete": False,
        "completion_inconsistent": False,
    }


def test_bleach_style_title_shows_zero_of_episode_total_before_history_update(tmp_path):
    save_state(
        {
            "topics": [
                {
                    "id": "bleach-style",
                    "title": "Bleach (сезон 1 из 16, серии 1-20 из 20)",
                    "url": "https://nnmclub.to/forum/viewtopic.php?t=2345679",
                    "save_path": str(tmp_path),
                    "hash": "ABC123",
                    "selection": {"mode": "all"},
                    "selection_verified": True,
                    "selected_episode_keys": [f"episode:s01e{episode:02d}" for episode in range(1, 21)],
                }
            ],
            "mirrors": {},
        }
    )
    save_download_history({"schema_version": 1, "topics": {}})
    client = TestClient(app)

    home = client.get("/")
    details = client.get("/topics/bleach-style/downloads.json")

    assert home.status_code == 200
    assert "0/20" in home.text
    assert details.json()["summary"]["expected"] == 20
    assert details.json()["summary"]["is_complete"] is False
