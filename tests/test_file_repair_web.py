"""Recovery is visible across devices without treating accepted commands as recovered files."""

from __future__ import annotations

import time
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from tow import check_steps, file_presence, file_repair
from tow.clients.factory import default_client_id
from tow.config import load_config
from tow.issues import Snapshot
from tow.issues.kinds.files_missing import collect
from tow.store import load_state, save_download_history, save_state
from tow.web import app, services
from tow.web import sync as web_sync
from tow.web.sync import row_projection
from tow.web.views import check_feedback


def world():
    config = load_config()
    client_id = default_client_id(config)
    topic = {
        "id": "repair-topic",
        "title": "Show A [5 of 24]",
        "url": "https://tracker.example/topic/1",
        "hash": "a" * 40,
        "save_path": "/downloads/show",
        "client_id": client_id,
        "selection": {"mode": "all"},
        "selection_hash": "a" * 40,
        "selection_verified": True,
        "last_ok": True,
        "last_error": "",
    }
    file_presence.observe(topic, {"missing_files": 3, "unverified_files": 0}, client_id=client_id, at="first")
    now = time.time()
    receipt = {
        "version": 1,
        "id": "1" * 32,
        "binding": file_repair.binding(topic, client_id),
        "hash": topic["hash"],
        "client": client_id,
        "path": topic["save_path"],
        "files": "a" * 64,
        "before_progress": "b" * 64,
        "volume": "uuid:test-volume",
        "phase": "checking",
        "recheck_dispatched": True,
        "check_at": now,
        "requested_at": now,
        "updated_at": now,
        "deadline": now + 3600,
        "next_at": now,
        "attempts": 1,
        "resume_authorized": True,
        "count": 3,
    }
    topic.update(repair_request_revision=receipt["id"], repair_pending=receipt)
    save_state({"topics": [topic], "mirrors": {}, "health": {"qbit_ok": True, "check_ok": True}})
    save_download_history(
        {
            "schema_version": 1,
            "topics": {
                topic["id"]: {
                    "expected": {"kind": "episodes", "total": 24},
                    "items": {
                        f"episode:s01e{n:02d}": {
                            "label": f"S01E{n:02d}",
                            "episode_key": f"episode:s01e{n:02d}",
                            "status": "completed" if n > 3 else "missing",
                            "progress": 1.0,
                        }
                        for n in range(1, 6)
                    },
                }
            },
        }
    )
    return topic, config


def test_pending_recovery_shows_work_instead_of_requiring_attention_on_both_devices():
    topic, config = world()
    assert file_presence.count(topic, config) == 3
    assert collect(Snapshot({"topics": [topic]}, config, time.time())) == []
    first, second = TestClient(app), TestClient(app)
    for client in (first, second):
        page = client.get("/")
        panel = client.get("/topics/repair-topic/edit-panel")
        assert page.status_code == panel.status_code == 200
        assert 'data-issue-id="files_missing:repair-topic"' not in page.text
        assert "Проверка файлов" in page.text
        assert "2/24" in page.text
        assert 'action="/topics/repair-topic/cancel-repair"' in panel.text
        assert 'action="/topics/repair-topic/repair-files"' not in panel.text
        assert client.get("/topics/repair-topic/downloads.json").json()["summary"]["is_complete"] is False


def test_expired_or_blocked_recovery_returns_actionable_warning_without_claiming_recovery():
    topic, config = world()
    topic["repair_pending"].update(phase="blocked", blocked="repair.client_stopped")
    save_state({"topics": [topic], "mirrors": {}, "health": {"qbit_ok": True, "check_ok": True}})
    issues = collect(Snapshot({"topics": [topic]}, config, time.time()))
    assert len(issues) == 1
    assert issues[0].fix.ref == "repair_files:repair-topic"
    panel = TestClient(app).get("/topics/repair-topic/edit-panel")
    assert "Раздача остановлена во время восстановления" in panel.text
    assert 'action="/topics/repair-topic/repair-files"' in panel.text
    assert 'action="/topics/repair-topic/cancel-repair"' in panel.text
    assert file_presence.count(topic, config) == 3


def test_cancel_and_pause_withdraw_further_commands_without_changing_the_client(monkeypatch):
    topic, _ = world()
    monkeypatch.setattr(
        services, "client_from_secrets", lambda *args: (_ for _ in ()).throw(AssertionError("no client"))
    )
    client = TestClient(app, base_url="http://127.0.0.1:8787", headers={"Origin": "http://127.0.0.1:8787"})
    response = client.post("/topics/repair-topic/cancel-repair", follow_redirects=False)
    assert response.status_code == 303
    cancelled = load_state()["topics"][0]
    assert "repair_pending" not in cancelled
    assert cancelled["repair_request_revision"] != topic["repair_request_revision"]
    assert "files_missing" in cancelled

    world()
    response = client.post("/topics/repair-topic/pause", follow_redirects=False)
    assert response.status_code == 303
    assert load_state()["topics"][0]["paused"] is True
    assert "repair_pending" not in load_state()["topics"][0]


def test_repair_phase_changes_participate_in_shared_row_revision():
    before = {"id": "repair-topic", "file_repair_phase": "checking"}
    after = {**before, "file_repair_phase": "downloading"}
    assert row_projection(before, site_title="Example", day="2026-01-01") != row_projection(
        after, site_title="Example", day="2026-01-01"
    )


def test_pending_feedback_is_work_in_progress_even_with_a_successful_dispatch():
    feedback = check_feedback({"id": "repair-topic", "ok": True, "status": "repair_pending"}, topic_id="repair-topic")
    assert feedback["kind"] == "info"
    assert "автоматически" in feedback["text"]


def test_concurrent_owner_cancel_survives_progress_merge():
    topic, _ = world()
    checked = deepcopy(topic)
    topic.pop("repair_pending")
    topic["repair_request_revision"] = "2" * 32
    state = {"topics": [topic]}
    check_steps.merge_check_results(state, {"topics": [checked]}, edited={topic["id"]: {"repair_request_revision"}})
    assert "repair_pending" not in state["topics"][0]


def test_recovery_expiry_changes_shared_attention_and_rows_without_another_store_write(monkeypatch):
    topic, _ = world()
    client = TestClient(app)
    before = client.get("/sync.json").json()
    assert before["areas"]["attention"]["count"] == 0
    assert web_sync._repair_deadline(load_state(), time.time()) == topic["repair_pending"]["deadline"]

    later = topic["repair_pending"]["deadline"] + 1
    monkeypatch.setattr(time, "time", lambda: later)
    after = client.get(
        "/sync.json", params={"have": ",".join(f"{key}:{value}" for key, value in before["rev"].items())}
    ).json()
    assert after["rev"]["attention"] != before["rev"]["attention"]
    assert after["rev"]["topics"] != before["rev"]["topics"]
    assert any(entry["kind"] == "files_missing" for entry in after["areas"]["attention"]["entries"])
    assert load_state()["topics"][0]["repair_pending"] == topic["repair_pending"]


@pytest.mark.parametrize(
    ("action", "target"), [("repair-files", "request_file_repair"), ("cancel-repair", "cancel_file_repair")]
)
def test_unconfirmed_owner_actions_show_a_safe_failure_without_claiming_success(monkeypatch, action, target):
    topic, _ = world()

    def unavailable(_topic_id):
        raise OSError("synthetic disk unavailable")

    monkeypatch.setattr(services, target, unavailable)
    client = TestClient(app, base_url="http://127.0.0.1:8787", headers={"Origin": "http://127.0.0.1:8787"})
    response = client.post(f"/topics/repair-topic/{action}", follow_redirects=True)
    assert response.status_code == 200
    assert load_state()["topics"][0]["repair_pending"] == topic["repair_pending"]
    assert any(item["kind"] == "err" for item in client.get("/messages.json").json()["items"])
