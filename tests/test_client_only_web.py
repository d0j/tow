"""One-time handoffs never create watches or mistake an acknowledgement for proof."""

from __future__ import annotations

import copy
import time
from dataclasses import replace

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from helpers import flash_of, flash_record, multi_file_torrent

from tow import client_only, client_only_store, content, i18n, log, messages_feed
from tow.clients.factory import client_configuration
from tow.config import load_config
from tow.errors import TowError
from tow.selection import normalize_policy
from tow.store import load_download_history, load_state, persistence_lock, save_secrets, save_state
from tow.topic_form import TopicForm
from tow.web import app, client_only_actions, services

URL = "http://rutor.info/torrent/1234567/show-a"
OPERATION = "a" * 32


def torrent():
    return multi_file_torrent(
        b"Show S01", [{b"path": [b"S01E01.mkv"], b"length": 100}, {b"path": [b"S01E02.mkv"], b"length": 200}]
    )


@pytest.fixture
def handoff(monkeypatch):
    client_id = str(client_configuration(load_config(), None)["id"])
    token = content.prepare(torrent(), URL, client_id)["token"]  # explicitly chosen local file
    save_state({"topics": [{"id": "existing-watch", "url": URL, "title": "Original", "save_path": "D:/TV"}]})
    original = copy.deepcopy(load_state())
    original_history = copy.deepcopy(load_download_history())
    calls, known = [], {}

    def result(operation_id, *, request_fingerprint=None):
        saved = known.get(operation_id)
        if saved is None:
            return None
        if request_fingerprint is not None and saved["fingerprint"] != request_fingerprint:
            raise TowError("client_only.request_changed")
        return copy.deepcopy(saved["result"])

    def send(*args, **kwargs):
        calls.append((args, kwargs))
        row = {
            "operation_id": args[4],
            "status": "added",
            "confirmed": True,
            "title": "Show S01",
            "client_id": args[2],
            "finished_at": time.time(),
        }
        known[args[4]] = {"result": row, "fingerprint": kwargs["request_fingerprint"]}
        return row

    monkeypatch.setattr(services, "get_client_only_result", result)
    monkeypatch.setattr(services, "send_to_client", send)
    monkeypatch.setattr(services, "run_check", lambda **_kwargs: pytest.fail("no watched-topic check"))
    monkeypatch.setattr(services, "guess_topic_title", lambda *_a, **_k: pytest.fail("no title fetch"))
    monkeypatch.setattr(
        services, "prepare_content", lambda *_a, **_k: pytest.fail("no site download for a local choice")
    )
    form = {
        "url": URL,
        "title": "Show S01",
        "save_path": "D:/TV",
        "client_id": client_id,
        "content_token": token,
        "client_only": "1",
        "submission_id": OPERATION,
        "tracking_mode": "watch",
        "check_interval_min": "-3",  # irrelevant to a one-time client handoff
    }
    browser = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    return browser, form, calls, known, original, original_history


def test_local_choice_sends_exact_bytes_without_a_download_or_watched_record(handoff):
    browser, form, calls, _known, original, history = handoff
    response = browser.post("/topics/add", data=form, follow_redirects=False)
    assert response.status_code == 303
    assert flash_record(response.headers["location"])["kind"] == "ok"
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[:3] == (torrent(), "D:/TV", form["client_id"])
    assert args[3]["tracking_mode"] == "once"
    assert kwargs["preferred_season"] == 1
    assert load_state() == original
    assert load_download_history() == history
    assert not any(row["kind"] == "topic_add" for row in log.read_events())
    events = [row for row in log.read_events() if row["kind"] == "client_only_result"]
    assert len(events) == 1
    assert events[0]["operation_id"] == OPERATION


def test_the_same_post_replays_after_its_preparation_expired_without_any_read_or_rpc(handoff, monkeypatch):
    browser, form, calls, _known, original, _history = handoff
    browser.post("/topics/add", data=form, follow_redirects=False)
    monkeypatch.setattr(services, "read_content", lambda *_a: pytest.fail("replay must not reopen an expired token"))
    response = browser.post("/topics/add", data=form, follow_redirects=False)
    assert flash_record(response.headers["location"])["kind"] == "ok"
    assert "уже выполнен" in flash_of(response.headers["location"])
    assert len(calls) == 1
    assert load_state() == original
    assert len([row for row in log.read_events() if row["kind"] == "client_only_result"]) == 1
    assert not any(row["kind"] == "action_feedback" for row in log.read_events())


def test_an_unreadable_replay_journal_keeps_the_form_and_never_echoes_its_raw_error(handoff, monkeypatch):
    browser, form, calls, _known, original, _history = handoff

    def unreadable(*_a, **_k):
        raise PermissionError("synthetic-private-file password=hidden-value")

    monkeypatch.setattr(services, "get_client_only_result", unreadable)
    response = browser.post("/topics/add", data=form, follow_redirects=False)
    assert response.status_code == 303
    assert "?add=" in response.headers["location"]
    restored = browser.get(response.headers["location"]).text
    assert "hidden-value" not in restored
    assert "synthetic-private-file" not in restored
    assert calls == []
    assert load_state() == original


@pytest.mark.parametrize(
    ("field", "value"), [("save_path", "D:/Other"), ("selection_value", "*.mkv"), ("content_token", "f" * 32)]
)
def test_reusing_an_operation_with_different_content_is_refused_before_fetch(handoff, monkeypatch, field, value):
    browser, form, calls, _known, original, _history = handoff
    browser.post("/topics/add", data=form, follow_redirects=False)
    monkeypatch.setattr(
        services, "read_content", lambda *_a: pytest.fail("changed request cannot use the old operation")
    )
    response = browser.post("/topics/add", data={**form, field: value}, follow_redirects=False)
    assert response.status_code == 303
    assert "?add=" in response.headers["location"]
    assert len(calls) == 1
    assert load_state() == original


def test_no_preparation_uses_the_cache_first_path_without_permission_to_spend_a_limit(handoff, monkeypatch):
    browser, form, calls, _known, _original, _history = handoff
    prepared = []

    def prepare(*args):
        prepared.append(args)
        return content.prepare(torrent(), URL, form["client_id"], from_site=True)

    monkeypatch.setattr(services, "prepare_content", prepare)
    response = browser.post("/topics/add", data={**form, "content_token": ""}, follow_redirects=False)
    assert response.status_code == 303
    assert flash_record(response.headers["location"])["kind"] == "ok"
    assert prepared == [(URL, form["client_id"], None, False)]
    assert calls[0][0][0] == torrent()


def test_a_limited_site_refusal_preserves_the_form_without_starting_or_saving(handoff, monkeypatch):
    browser, form, calls, _known, original, history = handoff

    def limited(*_args):
        raise TowError("content.limited")

    monkeypatch.setattr(services, "prepare_content", limited)
    response = browser.post("/topics/add", data={**form, "content_token": ""}, follow_redirects=False)
    assert "?add=" in response.headers["location"]
    restored = browser.get(response.headers["location"]).text
    assert BeautifulSoup(restored, "html.parser").select_one('input[name="client_only"]').has_attr("checked")
    assert calls == []
    assert load_state() == original
    assert load_download_history() == history


@pytest.mark.parametrize(("status", "confirmed", "kind"), [("pending", False, "info"), ("existing", True, "ok")])
def test_pending_is_never_announced_as_confirmed(handoff, monkeypatch, status, confirmed, kind):
    browser, form, _calls, _known, original, _history = handoff
    monkeypatch.setattr(
        services,
        "send_to_client",
        lambda *_a, **_k: {
            "operation_id": OPERATION,
            "status": status,
            "confirmed": confirmed,
            "title": "Show",
            "finished_at": time.time(),
        },
    )
    response = browser.post("/topics/add", data=form, follow_redirects=False)
    assert flash_record(response.headers["location"])["kind"] == kind
    assert load_state() == original


def test_an_unconfirmed_positive_result_is_refused_instead_of_fake_success(handoff, monkeypatch):
    browser, form, _calls, _known, original, _history = handoff
    monkeypatch.setattr(services, "send_to_client", lambda *_a, **_k: {"status": "added", "confirmed": False})
    response = browser.post("/topics/add", data=form, follow_redirects=False)
    assert "?add=" in response.headers["location"]
    assert load_state() == original


def test_exact_selected_files_reach_the_client_with_no_watch_mode(handoff):
    browser, form, calls, _known, original, _history = handoff
    response = browser.post(
        "/topics/add", data={**form, "selection_mode": "exact", "selection_indices": "[1]"}, follow_redirects=False
    )
    assert flash_record(response.headers["location"])["kind"] == "ok"
    policy = calls[0][0][3]
    assert policy["tracking_mode"] == "once"
    assert policy["files"] == [{"path": "S01E02.mkv", "size": 200}]
    assert load_state() == original


def test_one_prepared_submission_decrypts_its_metadata_only_once(handoff, monkeypatch):
    browser, form, _calls, _known, _original, _history = handoff
    reads = []
    original = services.read_content
    monkeypatch.setattr(services, "read_content", lambda *args: reads.append(args) or original(*args))
    response = browser.post("/topics/add", data=form, follow_redirects=False)
    assert flash_record(response.headers["location"])["kind"] == "ok"
    assert len(reads) == 1


def test_a_terminal_failure_keeps_the_choice_and_clears_the_next_request_identity(handoff, monkeypatch):
    browser, form, _calls, _known, original, _history = handoff
    monkeypatch.setattr(
        services,
        "send_to_client",
        lambda *_a, **_k: {
            "operation_id": OPERATION,
            "status": "failed",
            "confirmed": False,
            "finished_at": time.time(),
            "error": TowError("check.add_unconfirmed").record(),
        },
    )
    response = browser.post("/topics/add", data=form, follow_redirects=False)
    from urllib.parse import parse_qs, urlsplit

    from starlette.requests import Request

    from tow.web.views import add_draft

    query = urlsplit(response.headers["location"]).query
    request = Request({"type": "http", "method": "GET", "path": "/", "query_string": query.encode(), "headers": []})
    draft = add_draft(request)
    assert draft["client_only"] == "1"
    assert draft["content_token"] == form["content_token"]
    assert not draft.get("submission_id")
    assert parse_qs(query).get("add")
    assert load_state() == original


def _result(**extra):
    return {
        "operation_id": OPERATION,
        "status": "added",
        "confirmed": True,
        "finished_at": time.time(),
        "title": "Show",
        **extra,
    }


def test_terminal_result_is_shared_once_across_restarted_feed_readers():
    assert messages_feed.reconcile_client_only(_result())
    assert not messages_feed.reconcile_client_only(_result())
    messages_feed._FEEDS.clear()
    assert not messages_feed.reconcile_client_only(_result())
    projection = messages_feed.feed().refresh().projection
    en = messages_feed.render(projection, lang="en")["items"]
    ru = messages_feed.render(projection, lang="ru")["items"]
    assert len(en) == len(ru) == 1
    assert en[0]["kind"] == ru[0]["kind"] == "ok"
    assert "not watched" in en[0]["text"]
    assert "без отслеживания" in ru[0]["text"]
    assert "client_only" not in ru[0]["text"]


@pytest.mark.parametrize(
    "result",
    [
        None,
        {},
        _result(status=[]),
        _result(status="pending"),
        _result(confirmed=False),
        _result(finished_at=True),
        _result(finished_at=float("nan")),
        _result(finished_at=1),
    ],
)
def test_unfinished_malformed_and_old_results_are_not_shared_as_success(result):
    assert not messages_feed.reconcile_client_only(result)


def test_failed_terminal_result_is_an_error_in_both_languages():
    assert messages_feed.reconcile_client_only(
        _result(status="failed", confirmed=False, error=TowError("check.add_unconfirmed").record())
    )
    projection = messages_feed.feed().refresh().projection
    for language in ("ru", "en"):
        item = messages_feed.render(projection, lang=language)["items"][0]
        assert item["kind"] == "err"
        assert item["href"] == "/history?group=errors"


def test_cancelled_terminal_result_is_shared_as_information():
    assert messages_feed.reconcile_client_only(
        _result(status="cancelled", confirmed=False, error=TowError("client_only.cancelled").record())
    )
    projection = messages_feed.feed().refresh().projection
    for language in ("ru", "en"):
        item = messages_feed.render(projection, lang=language)["items"][0]
        assert item["kind"] == "info"
        assert "client_only" not in item["text"]
    event = next(row for row in log.read_events() if row["kind"] == "client_only_result")
    assert not event.get("error_code")


def test_handoff_history_is_visible_and_success_is_not_an_error():
    assert messages_feed.reconcile_client_only(_result())
    assert messages_feed.reconcile_client_only(
        _result(
            operation_id="b" * 32, status="failed", confirmed=False, error=TowError("check.add_unconfirmed").record()
        )
    )
    assert {record["status"] for record in log.history_events()} == {"added", "failed"}
    assert {record["status"] for record in log.history_events(group="downloads")} == {"added", "failed"}
    assert [record["status"] for record in log.history_events(group="errors")] == ["failed"]


def test_a_lost_terminal_append_is_retried_without_losing_the_client_result(monkeypatch):
    actual = log.append_event
    monkeypatch.setattr(log, "append_event", lambda *_a, **_k: None)
    assert not messages_feed.reconcile_client_only(_result())
    monkeypatch.setattr(log, "append_event", actual)
    assert messages_feed.reconcile_client_only(_result())


def test_producer_reconciles_stored_terminal_results_even_if_no_operation_just_finished(monkeypatch):
    calls = []
    monkeypatch.setattr(services, "advance_client_only", lambda **kwargs: calls.append(kwargs) or [])
    monkeypatch.setattr(services, "finished_client_only", lambda: [_result()])
    assert client_only_actions.reconcile_results() == 1
    assert client_only_actions.reconcile_results() == 0
    assert calls == [{"limit": 4}, {"limit": 4}]


def test_replay_fingerprint_ignores_cosmetic_name_and_irrelevant_tracking_controls():
    form = TopicForm(url=URL, title="Show", content_token="f" * 32)
    changed = replace(form, title="New name", tracking_mode="watch", check_interval_min="-123")
    assert client_only_actions._fingerprint(form) == client_only_actions._fingerprint(changed)
    assert client_only_actions._fingerprint(replace(form, title="Show S02")) != client_only_actions._fingerprint(form)
    assert i18n.has("web.client_only.pending")


@pytest.fixture
def queued_native_handoff(monkeypatch):
    """A real encrypted receipt; no native request has been dispatched."""
    save_secrets({"qbittorrent": {"host": "http://client.example", "port": 8080, "password": "synthetic"}})
    save_state({"topics": [{"id": "existing-watch", "url": URL, "title": "Original"}]})
    original = copy.deepcopy(load_state())
    client_id = str(client_configuration(load_config(), None)["id"])
    monkeypatch.setattr(client_only, "advance", lambda identity: client_only.get_result(identity))
    monkeypatch.setattr(
        client_only.factory,
        "from_secrets",
        lambda *_a, **_k: pytest.fail("cancellation never talks to a native client"),
    )
    result = client_only.send_to_client(
        torrent(),
        "D:/TV",
        client_id,
        normalize_policy("all", tracking_mode="once"),
        OPERATION,
        request_fingerprint="f" * 64,
    )
    assert result["status"] == "pending"
    return TestClient(app, headers={"Origin": "http://127.0.0.1"}), original


def test_pending_cancel_uses_the_real_receipt_without_requiring_its_form_fingerprint(queued_native_handoff):
    browser, original = queued_native_handoff
    response = browser.post(f"/client-only/{OPERATION}/cancel", follow_redirects=False)
    assert response.status_code == 303
    assert flash_record(response.headers["location"])["kind"] == "info"
    assert client_only.get_result(OPERATION)["status"] == "cancelled"
    with persistence_lock():
        stored = client_only_store.read_entries()[OPERATION]
    assert stored["add_dispatched"] is False
    assert stored["start_dispatched"] is False
    assert load_state() == original


def test_cancel_does_not_change_an_already_terminal_receipt(queued_native_handoff):
    browser, original = queued_native_handoff
    with persistence_lock():
        entries = client_only_store.read_entries()
        entries[OPERATION].update(status="added", phase="done", finished_at=time.time())
        client_only_store.write_entries(entries)
        before = copy.deepcopy(entries[OPERATION])
    response = browser.post(f"/client-only/{OPERATION}/cancel", follow_redirects=False)
    assert flash_record(response.headers["location"])["kind"] == "info"
    assert "уже завершена" in flash_of(response.headers["location"])
    with persistence_lock():
        assert client_only_store.read_entries()[OPERATION] == before
    assert load_state() == original


def test_cancel_of_unknown_operation_is_an_explicit_warning(queued_native_handoff):
    browser, original = queued_native_handoff
    response = browser.post(f"/client-only/{'e' * 32}/cancel", follow_redirects=False)
    assert flash_record(response.headers["location"])["kind"] == "warn"
    assert client_only.get_result(OPERATION)["status"] == "pending"
    assert load_state() == original


def test_cancel_rejects_a_cross_origin_post_and_keeps_the_pending_operation(queued_native_handoff):
    browser, original = queued_native_handoff
    response = browser.post(
        f"/client-only/{OPERATION}/cancel", headers={"Origin": "https://evil.example"}, follow_redirects=False
    )
    assert response.status_code == 403
    assert client_only.get_result(OPERATION)["status"] == "pending"
    assert load_state() == original


def test_pending_status_link_is_shared_and_cancel_survives_the_original_flash(handoff, monkeypatch):
    from tow.web import messages

    browser, form, _calls, _known, original, history = handoff
    send = services.send_to_client

    def pending(*args, **kwargs):
        result = send(*args, **kwargs)
        result.update(status="pending", confirmed=False, phase="checking", selected_file_count=2)
        result.pop("finished_at", None)
        return result

    monkeypatch.setattr(services, "send_to_client", pending)
    response = browser.post("/topics/add", data=form, follow_redirects=False)
    href = flash_record(response.headers["location"])["href"]
    assert href == f"/client-only/{OPERATION}"
    other = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    pending_item = next(item for item in other.get("/messages.json").json()["items"] if item["kind"] == "info")
    assert pending_item["href"] == href
    monkeypatch.setattr(messages.FLASHES, "get", lambda _key: None)
    monkeypatch.setattr(services, "read_content", lambda *_a: pytest.fail("status must not reread content"))
    monkeypatch.setattr(services, "advance_client_only", lambda **_k: pytest.fail("a GET never advances the handoff"))
    page = other.get(href)
    assert page.status_code == 200
    assert f'action="/client-only/{OPERATION}/cancel"' in page.text
    assert "Клиент проверяет имеющиеся файлы" in page.text
    assert "Выбрано файлов: 2" in page.text
    assert "qBittorrent" in page.text
    assert load_state() == original
    assert load_download_history() == history


def test_real_pending_status_get_never_changes_the_encrypted_receipt(queued_native_handoff):
    from tow.paths import run_dir

    browser, original = queued_native_handoff
    journal = run_dir() / "client-only-adds.enc"
    before_bytes = journal.read_bytes()
    with persistence_lock():
        before = copy.deepcopy(client_only_store.read_entries())
    for _ in range(2):
        page = browser.get(f"/client-only/{OPERATION}")
        assert page.status_code == 200
        assert f'action="/client-only/{OPERATION}/cancel"' in page.text
    with persistence_lock():
        assert client_only_store.read_entries() == before
    assert journal.read_bytes() == before_bytes
    assert load_state() == original


@pytest.mark.parametrize("status", ["added", "existing", "failed", "cancelled"])
def test_terminal_status_page_has_no_cancel_control(handoff, monkeypatch, status):
    browser = handoff[0]
    monkeypatch.setattr(
        services,
        "get_client_only_result",
        lambda *_a, **_k: _result(status=status, confirmed=status in ("added", "existing")),
    )
    page = browser.get(f"/client-only/{OPERATION}")
    assert page.status_code == 200
    assert f'action="/client-only/{OPERATION}/cancel"' not in page.text
    assert "Show" in page.text


def test_unknown_status_operation_is_an_explicit_not_found_page(queued_native_handoff):
    browser, original = queued_native_handoff
    page = browser.get(f"/client-only/{'e' * 32}")
    assert page.status_code == 404
    assert "сведения об этой отправке уже недоступны" in page.text
    assert f'action="/client-only/{OPERATION}/cancel"' not in page.text
    assert load_state() == original


def test_unreadable_status_page_is_safe_and_does_not_expose_diagnostics(handoff, monkeypatch):
    browser = handoff[0]

    def unreadable(*_a, **_k):
        raise PermissionError("synthetic-private-file password=hidden-value")

    monkeypatch.setattr(services, "get_client_only_result", unreadable)
    page = browser.get(f"/client-only/{OPERATION}")
    assert page.status_code == 503
    assert "hidden-value" not in page.text
    assert "synthetic-private-file" not in page.text
    assert f'action="/client-only/{OPERATION}/cancel"' not in page.text


@pytest.mark.parametrize(
    "href", ["/client-only/not-an-id", f"/client-only/{OPERATION}/cancel", f"/client-only/{OPERATION}/../settings"]
)
def test_shared_status_links_accept_only_a_complete_operation_id(href):
    messages_feed.record_feedback({"kind": "info", "text": "Pending", "href": href})
    item = messages_feed.render(messages_feed.feed().refresh().projection, lang="en")["items"][0]
    assert item["href"] == "/history"
