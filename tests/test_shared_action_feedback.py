"""Final action results survive navigation and are visible to independent devices."""

from __future__ import annotations

import asyncio
import contextvars
import json
import threading

import pytest
from fastapi.testclient import TestClient
from helpers import manual_check_consumers

from tow import log, messages_feed
from tow.web import app, check_jobs, services

BASE = "http://127.0.0.1:8787"


def _items():
    return messages_feed.render(messages_feed.feed().refresh().projection, lang="en")["items"]


def test_unlogged_feedback_is_shared_scrubbed_and_has_the_strips_identity():
    message = {
        "id": "feedback-1",
        "kind": "err",
        "subject": "Show A",
        "text": "Cannot save D:\\TV\\private.mkv using https://user:pw@host.example/private?token=secret",
        "href": "/settings?open=clients&token=secret#client-q1",
    }
    assert messages_feed.record_feedback(message)
    (entry,) = _items()
    assert entry["kind"] == "err"
    assert entry["subject"] == "Show A"
    assert entry["source_id"] == "feedback-1"
    assert entry["href"] == "/settings?open=clients#client-q1"
    words = json.dumps(entry)
    for private in ("private.mkv", "D:", "user:pw", "token", "secret"):
        assert private not in words
    assert "host.example" in words
    a, b = TestClient(app, base_url=BASE), TestClient(app, base_url=BASE)
    assert a.get("/messages.json").json()["items"] == b.get("/messages.json").json()["items"]


@pytest.mark.parametrize("existing", ["topic_edit", "settings_interval", "client_added"])
def test_a_semantic_result_is_not_duplicated_by_the_flash(existing):
    with log.observe_events() as observed:
        event_id = log.append_event(existing, topic="a", title="Show A")
    assert messages_feed.record_feedback({"kind": "ok", "text": "Saved successfully"}, observed) == event_id
    assert len(_items()) == 1


def test_an_unlogged_warning_is_not_hidden_by_a_successful_save():
    with log.observe_events() as observed:
        log.append_event("settings_notifier", integration_id="telegram")
    assert messages_feed.record_feedback({"kind": "warn", "text": "Saved; the server cannot be reached"}, observed)
    assert [entry["kind"] for entry in _items()] == ["warn", "ok"]


def test_observation_is_isolated_and_follows_a_worker_thread():
    async def operation(kind):
        with log.observe_events() as observed:
            await asyncio.to_thread(log.append_event, kind)
            await asyncio.sleep(0)
        return [entry["kind"] for entry in observed]

    async def concurrent():
        return await asyncio.gather(operation("topic_add"), operation("topic_delete"))

    assert asyncio.run(concurrent()) == [["topic_add"], ["topic_delete"]]


@pytest.mark.parametrize(
    "href", ["//host.example", "/%2fhost.example", "/settings\\bad", "/settings\nX", "/settings?" + "a=b&" * 20]
)
def test_malformed_or_external_feedback_links_do_not_break_the_feed(href):
    assert messages_feed.record_feedback({"kind": "warn", "text": "Action refused safely", "href": href})
    assert _items()[0]["href"] == "/history"


def test_two_messengers_recover_independently_and_name_their_connection():
    log.append_event("bot_delivery_failed", integration_id="telegram", status="queued")
    log.append_event("bot_delivery_failed", integration_id="discord", status="queued")
    log.append_event("bot_delivery_succeeded", integration_id="telegram")
    entries = _items()
    assert [(entry["subject"], entry["kind"]) for entry in entries] == [
        ("Telegram", "ok"),
        ("Discord", "warn"),
        ("Telegram", "warn"),
    ]
    assert [entry["href"] for entry in entries] == [
        "/settings?open=bots&card=telegram#notifier-telegram",
        "/settings?open=bots&card=discord#notifier-discord",
        "/settings?open=bots&card=telegram#notifier-telegram",
    ]


def test_a_client_integration_id_does_not_replace_the_topics_title():
    log.append_event("client_added", integration_id="q1", client_id="q1", topic="a", title="Show A")
    assert _items()[0]["subject"] == "Show A"


@pytest.mark.parametrize("ok", [True, False])
def test_messenger_test_results_are_shared_and_typed(ok):
    log.append_event("notifier_test", integration_id="telegram", ok=ok)
    (entry,) = _items()
    assert entry["subject"] == "Telegram"
    assert entry["kind"] == ("ok" if ok else "err")
    assert ("test delivered" if ok else "test failed") in entry["text"]


def test_completed_applying_job_publishes_its_result_once_without_a_poll(monkeypatch):
    consumers = manual_check_consumers(monkeypatch)
    monkeypatch.setattr(services, "run_check", lambda **kw: {"results": [{"id": "a", "title": "Show A", "ok": True}]})
    client = TestClient(app, base_url=BASE, headers={"Origin": BASE})
    job = client.post("/check", data={"mode": "apply", "action_id": "action-a"}).json()["job"]
    consumers[0].run()
    (entry,) = _items()
    assert entry["source_id"] == "check-" + job
    assert entry["kind"] == "ok"
    assert "check_result=" + job in entry["href"]
    before = log.log_path().read_bytes()
    client.get("/check/status", params={"job": job})
    client.get("/check/status", params={"job": job})
    assert log.log_path().read_bytes() == before


def test_a_preview_does_not_publish_a_shared_result(monkeypatch):
    consumers = manual_check_consumers(monkeypatch)
    monkeypatch.setattr(services, "run_check", lambda **kw: {"results": []})
    client = TestClient(app, base_url=BASE, headers={"Origin": BASE})
    job = client.post("/check", data={"mode": "dry", "action_id": "preview-a"}).json()["job"]
    consumers[0].run()
    assert check_jobs.get(job)["status"] == "done"
    assert not log.log_path().exists()


def test_publication_failure_does_not_strand_the_next_job(monkeypatch):
    consumers = manual_check_consumers(monkeypatch)
    calls = []

    def run(**kw):
        calls.append(kw.get("ids"))
        return {"results": [{"id": "a", "title": "Show A", "ok": True}]}

    monkeypatch.setattr(services, "run_check", run)
    monkeypatch.setattr(services, "record_message_feedback", lambda *args, **kw: (_ for _ in ()).throw(OSError("held")))
    client = TestClient(app, base_url=BASE, headers={"Origin": BASE})
    one = client.post("/check", data={"mode": "apply", "action_id": "action-a"}).json()["job"]
    two = client.post("/topics/a/check", data={"action_id": "action-b"}).json()["job"]
    consumers[0].run()
    assert len(calls) == 2
    assert check_jobs.get(one)["status"] == "done"
    assert check_jobs.get(two)["status"] == "done"


def test_a_refused_form_is_shared_once_and_gets_cannot_publish_it_again():
    from tow.web.messages import flash_at

    client = TestClient(app, base_url=BASE, headers={"Origin": BASE})
    answer = client.post(
        "/settings/interval", data={"interval_min": "invalid", "flash_ttl_min": "5"}, follow_redirects=False
    )
    assert answer.status_code == 303
    (entry,) = _items()
    assert entry["kind"] == "err"
    assert entry["source_id"]
    assert flash_at(answer.headers["location"])["id"] == entry["id"]
    client.get(answer.headers["location"])
    assert len(_items()) == 1


def test_recovery_messages_do_not_hide_the_forms_refusal(monkeypatch):
    from tow.web import middleware

    original = middleware._recover_before_dispatch

    def recover(cleanup):
        original(cleanup)
        log.append_event("backup_restore_recovered")

    monkeypatch.setattr(middleware, "_recover_before_dispatch", recover)
    client = TestClient(app, base_url=BASE, headers={"Origin": BASE})
    client.post("/settings/interval", data={"interval_min": "invalid", "flash_ttl_min": "5"})
    entries = _items()
    assert any(entry["category"] == "actions" and entry["kind"] == "err" for entry in entries)


def test_cancelled_follow_up_failure_keeps_its_error_severity(monkeypatch):
    consumers = manual_check_consumers(monkeypatch)

    class FollowUp:
        def __call__(self, out, error):
            return None

        def dropped(self):
            return {"text": "Could not restore the previous sign-in", "kind": "err"}

    job, _ = check_jobs.submit(topic_id="a", apply=True, lang="en", perform=lambda job: None, follow_up=FollowUp())
    assert consumers
    assert check_jobs.cancel(job["id"])
    (entry,) = _items()
    assert entry["kind"] == "err"
    assert "Could not restore" in entry["text"]


def test_failed_publication_does_not_change_a_completed_forms_response(monkeypatch):
    monkeypatch.setattr(services, "record_message_feedback", lambda *args, **kw: (_ for _ in ()).throw(OSError("held")))
    client = TestClient(app, base_url=BASE, headers={"Origin": BASE})
    response = client.post(
        "/settings/interval", data={"interval_min": "invalid", "flash_ttl_min": "5"}, follow_redirects=False
    )
    assert response.status_code == 303


def test_a_damaged_log_link_and_its_private_query_cannot_leak_to_a_browser():
    log.append_event(
        "action_feedback",
        feedback_kind="warn",
        feedback_text="Action refused safely",
        feedback_href="/settings?" + "a=b&" * 20,
    )
    assert _items()[0]["href"] == "/history"


def test_a_queue_worker_never_inherits_a_request_or_its_event_observer(monkeypatch):
    from tow.web import _context

    original = threading.Thread

    def inheriting_thread(*args, **kwargs):
        kwargs.setdefault("context", contextvars.copy_context())
        return original(*args, **kwargs)

    # Python can be configured to inherit context in new threads. The queue must choose its
    # own clean context regardless of that process-wide option.
    monkeypatch.setattr(threading, "Thread", inheriting_thread)
    received = []

    def work():
        received.append(_context.subject())
        log.append_event("topic_add", topic="a")

    with _context.seeded({}):
        _context.use_subject("Another request")
        with log.observe_events() as observed:
            worker = check_jobs._start("isolated-check", work)
            worker.join(timeout=5)
            assert not worker.is_alive()
    assert received == [""]
    assert observed == []
