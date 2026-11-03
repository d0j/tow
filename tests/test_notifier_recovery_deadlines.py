"""Queued delivery observes a server's deadline and bounded external permission recovery."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from email.utils import format_datetime

import httpx
import pytest

from tow import notifiers
from tow.notifiers import base, outbox
from tow.store import load_state, save_state

DISCORD = {"notifiers": {"discord": {"webhook_url": "https://discord.com/api/webhooks/1/synthetic"}}}


@pytest.fixture
def transport(monkeypatch):
    clock = {"now": 1_793_664_000.0}
    state = {"response": httpx.Response(204)}
    seen = []

    def answer(request):
        seen.append(request)
        return state["response"]

    monkeypatch.setattr(outbox.time, "time", lambda: clock["now"])
    monkeypatch.setattr(base, "http_client", lambda: httpx.Client(transport=httpx.MockTransport(answer)))
    monkeypatch.setattr(base, "backoff_sleep", lambda _seconds: None)
    return clock, state, seen


@pytest.mark.parametrize("after", [30, 600, 3600, 1e100])
def test_server_wait_survives_new_events_reloaded_state_and_another_dispatcher(transport, after):
    clock, state, seen = transport
    state["response"] = httpx.Response(429, headers={"Retry-After": str(after)})
    assert notifiers.send_all(DISCORD, "first")["discord"][0] is False
    assert load_state()["notify_outbox"]["discord"]["retry_at"] == clock["now"] + after
    state["response"] = httpx.Response(204)
    notifiers.send_all(DISCORD, "second")
    notifiers.flush_outbox(DISCORD)
    assert len(seen) == 1
    assert outbox._claim("discord", DISCORD["notifiers"]["discord"], "another") == ("blocked", None)
    assert notifiers.health(DISCORD, load_state()) is False
    clock["now"] += after
    assert notifiers.flush_outbox(DISCORD) == {"discord": (True, "")}
    assert len(seen) == 2
    assert "first" in json.loads(seen[-1].content)["content"]
    assert "second" in json.loads(seen[-1].content)["content"]
    assert not load_state().get("notify_outbox")
    assert notifiers.health(DISCORD, load_state()) is True


def test_telegram_parameters_wait_is_per_recipient_and_survives_a_reload(transport):
    clock, state, seen = transport
    telegram = {"telegram": {"token": "123:" + "a" * 35, "chat_ids": ["1"]}}
    state["response"] = httpx.Response(429, json={"parameters": {"retry_after": 600}})
    notifiers.send_all(telegram, "queued")
    state["response"] = httpx.Response(200, json={"ok": True})
    notifiers.flush_outbox(telegram)
    assert len(seen) == 1
    clock["now"] += 600
    assert notifiers.flush_outbox(telegram)["telegram"][0] is True
    assert len(seen) == 2


def test_changed_connection_is_not_held_by_the_old_connections_rate_limit(transport):
    _clock, state, seen = transport
    state["response"] = httpx.Response(429, json={"retry_after": 3600})
    notifiers.send_all(DISCORD, "queued")
    state["response"] = httpx.Response(204)
    changed = {"notifiers": {"discord": {"webhook_url": "https://discord.com/api/webhooks/2/synthetic"}}}
    assert notifiers.flush_outbox(changed)["discord"][0] is True
    assert len(seen) == 2


def test_restored_external_permissions_resume_the_existing_queue_after_six_hours(transport):
    clock, state, seen = transport
    state["response"] = httpx.Response(403)
    notifiers.send_all(DISCORD, "queued")
    assert notifiers.health(DISCORD, load_state()) is False
    state["response"] = httpx.Response(204)
    notifiers.flush_outbox(DISCORD)
    assert len(seen) == 1
    clock["now"] += outbox.REPROBE_SEC
    assert notifiers.flush_outbox(DISCORD)["discord"][0] is True
    assert len(seen) == 2
    assert notifiers.health(DISCORD, load_state()) is True
    assert not load_state().get("notify_outbox")


@pytest.mark.parametrize("advance", [outbox.REPROBE_SEC, outbox.REPROBE_TTL_SEC + 1])
def test_permission_recovery_has_a_durable_budget_and_ttl(transport, advance):
    clock, state, seen = transport
    state["response"] = httpx.Response(403)
    notifiers.send_all(DISCORD, "queued")
    for _index in range(outbox.REPROBE_LIMIT + 3):
        clock["now"] += advance
        notifiers.flush_outbox(DISCORD)
    assert len(seen) == (1 + outbox.REPROBE_LIMIT if advance == outbox.REPROBE_SEC else 1)
    assert notifiers.health(DISCORD, load_state()) is False
    assert len(load_state()["notify_outbox"]["discord"]["items"]) == 1


@pytest.mark.parametrize("code", [401, 404])
def test_bad_credentials_and_missing_webhooks_do_not_generate_automatic_probes(transport, code):
    clock, state, seen = transport
    state["response"] = httpx.Response(code)
    notifiers.send_all(DISCORD, "queued")
    state["response"] = httpx.Response(204)
    clock["now"] += 7 * 86400
    notifiers.flush_outbox(DISCORD)
    assert len(seen) == 1
    assert notifiers.health(DISCORD, load_state()) is False


def test_http_date_wait_is_preserved_between_deliveries(transport):
    clock, state, seen = transport
    deadline = clock["now"] + 3600
    state["response"] = httpx.Response(
        429,
        headers={"Retry-After": format_datetime(datetime.fromtimestamp(deadline, UTC), usegmt=True)},
    )
    notifiers.send_all(DISCORD, "queued")
    assert load_state()["notify_outbox"]["discord"]["retry_at"] == deadline
    state["response"] = httpx.Response(204)
    notifiers.flush_outbox(DISCORD)
    assert len(seen) == 1


def test_manual_test_preserves_the_server_wait_without_a_preexisting_event_queue(transport):
    clock, state, seen = transport
    state["response"] = httpx.Response(429, json={"retry_after": 3600})
    assert notifiers.test(DISCORD, "discord")[0] is False
    assert not load_state()["notify_outbox"]["discord"]["items"]
    state["response"] = httpx.Response(204)
    assert notifiers.test(DISCORD, "discord")[0] is False
    notifiers.send_all(DISCORD, "real event")
    assert len(seen) == 1
    clock["now"] += 3600
    assert notifiers.test(DISCORD, "discord")[0] is True
    assert len(load_state()["notify_outbox"]["discord"]["items"]) == 1
    notifiers.flush_outbox(DISCORD)
    assert len(seen) == 3
    assert not load_state().get("notify_outbox")


def test_a_successful_test_never_acknowledges_the_queued_inflight_event(transport):
    _clock, state, seen = transport
    state["response"] = httpx.Response(503)
    notifiers.send_all(DISCORD, "real event")
    before = load_state()["notify_outbox"]["discord"]
    state["response"] = httpx.Response(204)
    assert notifiers.test(DISCORD, "discord")[0] is True
    after = load_state()["notify_outbox"]["discord"]
    assert after["items"] == before["items"]
    assert after["inflight"] == before["inflight"]
    notifiers.flush_outbox(DISCORD)
    assert "real event" in json.loads(seen[-1].content)["content"]


def test_a_parallel_test_and_queued_delivery_cannot_send_under_anothers_test_lease(monkeypatch):
    nested = []
    seen = []

    def answer(request):
        seen.append(request)
        if len(seen) == 1:
            nested.append(notifiers.test(DISCORD, "discord"))
            nested.append(notifiers.send_all(DISCORD, "queued during test")["discord"])
        return httpx.Response(204)

    monkeypatch.setattr(base, "http_client", lambda: httpx.Client(transport=httpx.MockTransport(answer)))
    assert notifiers.test(DISCORD, "discord")[0] is True
    assert [row[0] for row in nested] == [False, False]
    assert len(seen) == 1
    assert not load_state().get("notify_lease")
    notifiers.flush_outbox(DISCORD)
    assert len(seen) == 2


def test_cancelled_manual_test_releases_its_own_reservation(monkeypatch):
    monkeypatch.setattr(notifiers.get("discord"), "send", lambda *_args: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        notifiers.test(DISCORD, "discord")
    assert not load_state().get("notify_lease")
    assert not load_state().get("notify_outbox")


def test_internal_test_error_is_reported_and_does_not_hold_the_recipient(monkeypatch):
    module = notifiers.get("discord")
    monkeypatch.setattr(module, "send", lambda *_args: (_ for _ in ()).throw(RuntimeError("synthetic")))
    assert notifiers.test(DISCORD, "discord")[0] is False
    assert not load_state().get("notify_lease")
    monkeypatch.setattr(module, "send", lambda *_args: None)
    assert notifiers.test(DISCORD, "discord")[0] is True


def test_a_test_losing_its_lease_does_not_write_over_the_new_dispatcher(monkeypatch):
    def takeover(*_args):
        state = load_state()
        state["notify_lease"]["discord"]["owner"] = "another dispatcher"
        save_state(state)

    monkeypatch.setattr(notifiers.get("discord"), "send", takeover)
    assert notifiers.test(DISCORD, "discord")[0] is False
    state = load_state()
    assert state["notify_lease"]["discord"]["owner"] == "another dispatcher"
    assert not state.get("notify_status")


def test_an_old_connection_test_result_cannot_mark_changed_settings_healthy(monkeypatch):
    module = notifiers.get("discord")
    monkeypatch.setattr(module, "send", lambda *_args: None)
    assert notifiers.test(DISCORD, "discord")[0] is True
    changed = {"notifiers": {"discord": {"webhook_url": "https://discord.com/api/webhooks/2/synthetic"}}}
    card = next(card for card in notifiers.cards(changed, load_state()) if card["kind"] == "discord")
    assert card["ok"] is None


def test_a_refused_test_message_cannot_permanently_block_real_queued_events(transport):
    _clock, state, seen = transport
    state["response"] = httpx.Response(503)
    notifiers.send_all(DISCORD, "real event")
    state["response"] = httpx.Response(400)
    assert notifiers.test(DISCORD, "discord")[0] is False
    assert not load_state()["notify_outbox"]["discord"].get("blocked")
    state["response"] = httpx.Response(204)
    assert notifiers.flush_outbox(DISCORD)["discord"][0] is True
    assert "real event" in json.loads(seen[-1].content)["content"]


def test_an_explicit_owner_test_can_confirm_permissions_before_the_automatic_probe(transport):
    _clock, state, seen = transport
    state["response"] = httpx.Response(403)
    notifiers.send_all(DISCORD, "real event")
    state["response"] = httpx.Response(204)
    assert notifiers.test(DISCORD, "discord")[0] is True
    assert len(load_state()["notify_outbox"]["discord"]["items"]) == 1
    assert notifiers.flush_outbox(DISCORD)["discord"][0] is True
    assert len(seen) == 3


@pytest.mark.parametrize("code", [400, 403, 503])
def test_an_old_delivery_failure_is_not_a_failure_of_changed_settings(transport, code):
    _clock, state, _seen = transport
    state["response"] = httpx.Response(code)
    notifiers.send_all(DISCORD, "real event")
    changed = {"notifiers": {"discord": {"webhook_url": "https://discord.com/api/webhooks/2/synthetic"}}}
    card = next(card for card in notifiers.cards(changed, load_state()) if card["kind"] == "discord")
    assert card["ok"] is None
