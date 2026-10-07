"""Messengers: request formats, retries, the delivery queue, settings and the settings page.

Every test talks to a fake HTTP transport; nothing leaves the machine.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest
from fastapi.testclient import TestClient
from helpers import flash_of

from tow import notifiers
from tow.check import notices as check_notices
from tow.notifiers import base
from tow.notifiers.outbox import chunks
from tow.store import load_secrets, load_state, save_secrets

TG_TOKEN = "12345:" + "a" * 35
DISCORD_URL = "https://discord.com/api/webhooks/123456/fake-webhook-token"
DISCORD = {"notifiers": {"discord": {"webhook_url": DISCORD_URL}}}


@pytest.fixture
def http(monkeypatch):
    """Route every messenger call to ``handler``; returns (set_handler, seen_requests, sleeps)."""
    seen: list[httpx.Request] = []
    sleeps: list[float] = []
    current: dict[str, Callable[[httpx.Request], httpx.Response]] = {"handler": lambda _r: httpx.Response(200)}

    def transport(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return current["handler"](request)

    monkeypatch.setattr(base, "http_client", lambda: httpx.Client(transport=httpx.MockTransport(transport)))
    monkeypatch.setattr(base, "backoff_sleep", sleeps.append)

    def set_handler(handler: Callable[[httpx.Request], httpx.Response]) -> None:
        current["handler"] = handler

    return set_handler, seen, sleeps


def _messenger_modules() -> list[str]:
    """Every messenger file in the package (what discovery must find, whatever is added)."""
    import pkgutil

    from tow.notifiers import registry

    return [
        info.name
        for info in pkgutil.iter_modules(notifiers.__path__)
        if not info.name.startswith("_") and info.name not in registry._SKIP
    ]


def test_every_messenger_is_found_in_display_order():
    found = notifiers.kinds()
    assert sorted(module.__name__.rsplit(".", 1)[-1] for module in found.values()) == sorted(_messenger_modules())
    orders = [module.ORDER for module in found.values()]
    assert orders == sorted(orders)
    assert next(iter(found)) == "telegram"  # the first card the owner sees
    for module in found.values():
        assert module.STEPS
        assert module.FIELDS


def _is_key(value: str) -> bool:
    import re

    return bool(re.fullmatch(r"[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+", value or ""))


def test_every_messenger_text_is_in_every_language():
    """A messenger's wording lives in the language files under notifier.<kind>.*: a wording fix
    touches only a language file, so this test checks keys, never the words."""
    from tow import i18n

    for kind, module in notifiers.kinds().items():
        keys = [*module.STEPS, module.NOTE]
        keys += [text for field in module.FIELDS for text in (field.label, field.error) if text]
        keys += [field.placeholder for field in module.FIELDS if _is_key(field.placeholder)]
        for key in keys:
            assert key.startswith(f"notifier.{kind}."), key
            for code in i18n.codes():
                assert i18n.has(key, code), (code, key)
                assert i18n.translate(key, code).strip(), (code, key)
    for lang in i18n.codes():
        token = i18n._CURRENT.set(lang)  # as a request in this language
        try:
            for card in notifiers.cards({}, {}):
                module = notifiers.get(card["kind"])
                title_key = f"notifier.{card['kind']}.title"
                expected = i18n.translate(title_key, lang) if i18n.has(title_key) else module.TITLE
                assert card["title"] == expected == notifiers.title(module, lang)
                assert card["steps"] == [i18n.translate(step, lang) for step in module.STEPS]
                for shown, field in zip(card["fields"], module.FIELDS, strict=True):
                    assert shown["label"] == i18n.translate(field.label, lang)
                    if _is_key(field.placeholder):
                        assert shown["placeholder"] == i18n.translate(field.placeholder, lang)
        finally:
            i18n._CURRENT.reset(token)


def test_a_broken_messenger_module_does_not_take_the_others_down(monkeypatch, caplog):
    import importlib

    from tow.notifiers import registry

    real = importlib.import_module

    def import_module(name, *args, **kwargs):
        if name == "tow.notifiers.whatsapp":
            raise ImportError("No module named 'some_missing_dependency'")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(registry.importlib, "import_module", import_module)
    registry._discover.cache_clear()
    try:
        assert "whatsapp" not in notifiers.kinds()
        assert {"telegram", "discord", "ntfy"} <= set(notifiers.kinds())
        assert registry.SKIPPED == [("whatsapp", "ImportError: No module named 'some_missing_dependency'")]
        assert "whatsapp" in caplog.text
        assert [card["kind"] for card in notifiers.cards({}, {})] == list(notifiers.kinds())
    finally:
        monkeypatch.undo()
        registry._discover.cache_clear()
    assert "whatsapp" in notifiers.kinds()


# --- request formats ------------------------------------------------------------------------


def test_discord_posts_json_without_pings(http):
    set_handler, seen, _ = http
    set_handler(lambda _r: httpx.Response(204))
    assert notifiers.send_all(DISCORD, "@everyone новая серия") == {"discord": (True, "")}
    (request,) = seen
    assert request.method == "POST"
    assert str(request.url) == DISCORD_URL
    assert json.loads(request.content) == {"content": "@everyone новая серия", "allowed_mentions": {"parse": []}}


def test_telegram_sends_to_every_chat(http):
    set_handler, seen, _ = http
    set_handler(lambda _r: httpx.Response(200, json={"ok": True}))
    secrets = {"telegram": {"token": TG_TOKEN, "chat_ids": ["111", "-222"]}}
    assert notifiers.send_all(secrets, "привет") == {"telegram": (True, "")}
    assert [r.url.path for r in seen] == [f"/bot{TG_TOKEN}/sendMessage"] * 2
    assert [dict(httpx.QueryParams(r.content.decode()))["chat_id"] for r in seen] == ["111", "-222"]


def test_whatsapp_uses_callmebot(http):
    set_handler, seen, _ = http
    set_handler(lambda _r: httpx.Response(200, text="<p>Message queued. You will receive it in a few seconds.</p>"))
    secrets = {"notifiers": {"whatsapp": {"phone": "+79990001122", "apikey": "123456"}}}
    assert notifiers.send_all(secrets, "серия") == {"whatsapp": (True, "")}
    (request,) = seen
    assert request.url.host == "api.callmebot.com"
    assert dict(request.url.params) == {"phone": "+79990001122", "text": "серия", "apikey": "123456"}


def test_ntfy_posts_to_the_topic_on_the_default_server(http):
    _, seen, _ = http
    secrets = {"notifiers": {"ntfy": {"topic": "tow-abcdef123456"}}}
    assert notifiers.send_all(secrets, "серия") == {"ntfy": (True, "")}
    (request,) = seen
    assert str(request.url) == "https://ntfy.sh/tow-abcdef123456"
    assert request.content.decode() == "серия"
    assert request.headers["Title"] == "TOW"


# --- clear reasons, never a secret in them ---------------------------------------------------


@pytest.mark.parametrize(
    ("secrets", "response", "expected"),
    [
        (
            {"telegram": {"token": TG_TOKEN, "chat_ids": ["1"]}},
            httpx.Response(401, json={"ok": False}),
            "токен неверный",
        ),
        (
            {"telegram": {"token": TG_TOKEN, "chat_ids": ["1"]}},
            httpx.Response(403, json={"ok": False}),
            "нажмите «Start»",
        ),
        (DISCORD, httpx.Response(404), "вебхук удалён"),
        (
            {"notifiers": {"whatsapp": {"phone": "+79990001122", "apikey": "987654"}}},
            httpx.Response(200, text="APIKey is invalid"),
            "apikey неверный",
        ),
        ({"notifiers": {"ntfy": {"topic": "tow-abcdef123456"}}}, httpx.Response(403), "требует вход"),
    ],
)
def test_permanent_failures_explain_what_to_do_without_leaking(http, secrets, response, expected):
    set_handler, _, sleeps = http
    set_handler(lambda _r: response)
    ((kind, (ok, reason)),) = notifiers.send_all(secrets, "x").items()
    assert not ok
    assert expected in reason
    for secret in (TG_TOKEN, DISCORD_URL, "fake-webhook-token", "987654", "tow-abcdef123456"):
        assert secret not in reason
    assert sleeps == []  # nothing to retry
    statuses = load_state()["notify_status"]
    assert [row["ok"] for key, row in statuses.items() if key.split(":")[0] == kind] == [False]


def test_rate_limit_is_waited_out(http):
    set_handler, seen, sleeps = http
    answers = iter([httpx.Response(429, headers={"Retry-After": "2"}), httpx.Response(204)])
    set_handler(lambda _r: next(answers))
    assert notifiers.send_all(DISCORD, "x") == {"discord": (True, "")}
    assert len(seen) == 2
    assert sleeps == [2.0]


def test_a_wait_longer_than_the_cap_is_left_to_the_next_delivery(http):
    set_handler, seen, sleeps = http
    answers = iter([httpx.Response(429, json={"retry_after": 600}), httpx.Response(204)])
    set_handler(lambda _r: next(answers))
    ((ok, _reason),) = notifiers.send_all(DISCORD, "x").values()
    assert not ok
    assert (len(seen), sleeps) == (1, [])  # no request inside the wait the service asked for
    assert [item["text"] for item in load_state()["notify_outbox"]["discord"]["items"]] == ["x"]


def test_telegram_wait_under_parameters_is_honoured(http):
    set_handler, seen, sleeps = http
    answers = iter(
        [
            httpx.Response(429, json={"ok": False, "error_code": 429, "parameters": {"retry_after": 30}}),
            httpx.Response(429, json={"ok": False, "error_code": 429, "parameters": {"retry_after": 2}}),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    set_handler(lambda _r: next(answers))
    telegram = {"telegram": {"token": TG_TOKEN, "chat_ids": ["1"]}}
    ((ok, _reason),) = notifiers.send_all(telegram, "x").values()
    assert (not ok, len(seen), sleeps) == (True, 1, [])  # 30 s: not waited out inside this delivery
    ((ok, _reason),) = notifiers.flush(telegram).values()
    assert ok
    assert sleeps == [2.0]


def test_network_error_is_retried_then_succeeds(http):
    set_handler, seen, sleeps = http
    calls = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("down", request=request)
        return httpx.Response(204)

    set_handler(flaky)
    assert notifiers.send_all(DISCORD, "x") == {"discord": (True, "")}
    assert len(seen) == 2
    assert sleeps == [1.0]


def test_a_huge_messenger_answer_is_not_read_into_memory(http):
    set_handler, seen, _ = http
    read: list[int] = []

    def endless() -> object:
        for _ in range(64):  # 64 MiB if it were read to the end
            read.append(1)
            yield b"x" * (1024 * 1024)

    set_handler(lambda _r: httpx.Response(200, content=endless()))
    response = base.request("POST", DISCORD_URL, what="Discord", json={"content": "x"})
    assert (response.status_code, response.content) == (200, b"")  # delivered: the rest is not read
    assert len(seen) == 1
    assert len(read) <= base.MAX_RESPONSE_BYTES // (1024 * 1024) + 1


def test_a_normal_answer_is_read_whole(http):
    set_handler, _, _ = http
    set_handler(lambda _r: httpx.Response(200, json={"ok": True, "result": {"message_id": 1}}))
    response = base.request("POST", DISCORD_URL, what="Discord", json={"content": "x"})
    assert base.json_or_empty(response) == {"ok": True, "result": {"message_id": 1}}


# --- a delivered message is never sent again by TOW itself ----------------------------------


def test_delivered_message_with_an_oversized_answer_leaves_the_queue(http):
    set_handler, seen, _ = http
    big = b"{" + b" " * (base.MAX_RESPONSE_BYTES + 10) + b"}"
    set_handler(lambda _r: httpx.Response(200, content=big))
    assert notifiers.send_all(DISCORD, "серия 1") == {"discord": (True, "")}
    notifiers.flush(DISCORD)
    assert len(seen) == 1
    assert "notify_outbox" not in load_state()


def test_telegram_200_with_an_unreadable_answer_is_delivered(http):
    set_handler, seen, _ = http
    set_handler(lambda _r: httpx.Response(200, content=b"\xff" * 10))
    telegram = {"telegram": {"token": TG_TOKEN, "chat_ids": ["1"]}}
    assert list(notifiers.send_all(telegram, "x").values()) == [(True, "")]
    assert len(seen) == 1


WHATSAPP = {"notifiers": {"whatsapp": {"phone": "+79990001122", "apikey": "123456"}}}


@pytest.mark.parametrize(
    "answer",
    [
        lambda: httpx.Response(200, content=b""),
        lambda: httpx.Response(200, content=b"x" * (base.MAX_RESPONSE_BYTES + 10)),  # not read: empty
        lambda: httpx.Response(200, text="<html><body>Some new CallMeBot page</body></html>"),
    ],
)
def test_whatsapp_200_that_says_nothing_known_is_delivered_once(http, answer):
    """The gateway took the message: "wait and send again" would deliver it twice."""
    set_handler, seen, _ = http
    set_handler(lambda _r: answer())
    assert notifiers.send_all(WHATSAPP, "серия") == {"whatsapp": (True, "")}
    notifiers.flush(WHATSAPP)
    assert len(seen) == 1


def test_whatsapp_asking_to_wait_is_tried_again(http):
    set_handler, _, _ = http
    set_handler(lambda _r: httpx.Response(200, text="Please wait 2 seconds between messages"))
    ((ok, _reason),) = notifiers.send_all(WHATSAPP, "серия").values()
    assert not ok
    assert [item["text"] for item in load_state()["notify_outbox"]["whatsapp"]["items"]] == ["серия"]


def test_telegram_group_moved_to_a_supergroup_names_the_new_id(http):
    set_handler, _, _ = http
    set_handler(
        lambda _r: httpx.Response(
            400,
            json={
                "ok": False,
                "error_code": 400,
                "description": "Bad Request: group chat was upgraded to a supergroup chat",
                "parameters": {"migrate_to_chat_id": -1001234567890},
            },
        )
    )
    telegram = {"telegram": {"token": TG_TOKEN, "chat_ids": ["-4242"]}}
    ((ok, reason),) = notifiers.send_all(telegram, "x").values()
    assert not ok
    assert "-1001234567890" in reason
    assert "-4242" in reason
    # A settings problem: the message waits for the new id instead of being dropped.
    assert [item["text"] for item in load_state()["notify_outbox"]["telegram:-4242"]["items"]] == ["x"]


def test_discord_shows_titles_as_written(http):
    from tow.notifiers.discord import escape_markdown

    set_handler, seen, _ = http
    set_handler(lambda _r: httpx.Response(204))
    title = "*Show* __Part__ ||spoiler|| `x` ~~y~~ [a](b)\n# Big\n- item\n1. first\nhttps://t.example/a_b_c"
    notifiers.send_all(DISCORD, title)
    sent = json.loads(seen[0].content)["content"]
    assert sent == escape_markdown(title)
    assert "\\*Show\\* \\_\\_Part\\_\\_ \\|\\|spoiler\\|\\|" in sent
    assert "\n\\# Big\n\\- item\n1\\. first\nhttps://t.example/a_b_c" in sent  # the link is left as it is
    from tow.notifiers import discord

    assert len(escape_markdown("*" * discord.MAX_LEN)) <= 2000  # Discord's own limit


def test_no_answer_after_sending_is_not_sent_again_at_once(http):
    """The service got the POST but its answer timed out: it may have arrived. TOW does not post
    it again within this delivery; it keeps it, says why, and the next delivery sends it once."""
    set_handler, seen, sleeps = http

    def accepted_then_slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("answer too slow", request=request)

    set_handler(accepted_then_slow)
    ((ok, reason),) = notifiers.send_all(DISCORD, "серия 1").values()
    assert not ok
    assert "возможно, оно дошло" in reason
    assert (len(seen), sleeps) == (1, [])
    assert [item["text"] for item in load_state()["notify_outbox"]["discord"]["items"]] == ["серия 1"]
    set_handler(lambda _r: httpx.Response(204))
    assert notifiers.flush(DISCORD) == {"discord": (True, "")}
    assert len(seen) == 2


def test_one_delivery_ends_well_inside_the_queue_lease(http, monkeypatch):
    """A slow service cannot keep one message's retries going past the lease, after which another
    dispatcher would take the message over and send it as well."""
    from tow.notifiers import outbox

    set_handler, seen, sleeps = http
    clock = [0.0]
    monkeypatch.setattr(base.time, "monotonic", lambda: clock[0])

    def slow_503(_request: httpx.Request) -> httpx.Response:
        clock[0] += 40.0  # each attempt takes 40 s
        return httpx.Response(503)

    set_handler(slow_503)
    with pytest.raises(base.DeliveryError):
        base.request("POST", DISCORD_URL, what="Discord", json={"content": "x"})
    assert (len(seen), sleeps) == (2, [1.0])  # a third attempt would end after the budget
    # The last attempt starts within the budget: connecting, writing and the first byte take at
    # most 10 s each (http_client's timeout), the body ANSWER_SECONDS.
    assert base.REQUEST_BUDGET_SEC + 3 * 10.0 + base.ANSWER_SECONDS < outbox.LEASE_SEC


def test_telegram_parts_are_measured_as_telegram_counts():
    from tow.notifiers import telegram
    from tow.notifiers.outbox import parts_for

    parts = parts_for(telegram, "\U0001f3ac" * 3000)
    assert len(parts) == 2
    assert all(len(part.encode("utf-16-le")) // 2 <= 4096 for part in parts)
    assert "".join(parts) == "\U0001f3ac" * 3000


# --- the queue -----------------------------------------------------------------------------


def test_undelivered_message_waits_and_goes_first_next_time(http):
    set_handler, seen, sleeps = http
    set_handler(lambda _r: httpx.Response(503))
    ((ok, reason),) = notifiers.send_all(DISCORD, "серия 1").values()
    assert not ok
    assert "занят" in reason
    assert len(seen) == 3  # three attempts
    assert sleeps == list(base.RETRY_DELAYS)
    assert [item["text"] for item in load_state()["notify_outbox"]["discord"]["items"]] == ["серия 1"]
    assert notifiers.summary(DISCORD, load_state()) == "уведомления: Discord ✗"

    seen.clear()
    set_handler(lambda _r: httpx.Response(204))
    assert notifiers.send_all(DISCORD, "серия 2") == {"discord": (True, "")}
    (request,) = seen
    assert json.loads(request.content)["content"] == "Не доставлено ранее:\n\nсерия 1\n\nсерия 2"
    assert "notify_outbox" not in load_state()
    assert notifiers.health(DISCORD, load_state()) is True


def test_flush_delivers_only_what_is_queued(http):
    set_handler, seen, _ = http
    assert notifiers.flush_outbox(DISCORD) == {}
    assert seen == []
    set_handler(lambda _r: httpx.Response(500))
    notifiers.send_all(DISCORD, "серия 1")
    seen.clear()
    set_handler(lambda _r: httpx.Response(204))
    assert notifiers.flush_outbox(DISCORD) == {"discord": (True, "")}
    assert json.loads(seen[0].content)["content"] == "Не доставлено ранее:\n\nсерия 1"
    assert "notify_outbox" not in load_state()


def test_queue_is_bounded(http):
    set_handler, _, _ = http
    set_handler(lambda _r: httpx.Response(404))
    for index in range(notifiers.OUTBOX_LIMIT + 5):
        notifiers.send_all(DISCORD, f"m{index}")
    box = load_state()["notify_outbox"]["discord"]
    assert len(box["items"]) == notifiers.OUTBOX_LIMIT
    assert box["items"][-1]["text"] == f"m{notifiers.OUTBOX_LIMIT + 4}"
    assert box["blocked"]  # a deleted webhook holds the queue instead of failing every time
    assert load_state()["notify_dropped"]["discord"] == 5
    assert next(card for card in notifiers.cards(DISCORD, load_state()) if card["kind"] == "discord")["dropped"] == 5
    save_secrets(DISCORD)
    assert "Очередь переполнилась; удалено старых сообщений: 5" in _client().get("/settings").text


def test_one_broken_channel_does_not_stop_the_others(http, monkeypatch):
    set_handler, _, _ = http
    set_handler(lambda _r: httpx.Response(204))
    monkeypatch.setattr(notifiers.kinds()["whatsapp"], "send", lambda *_a: 1 / 0)
    secrets = {
        "notifiers": {
            "whatsapp": {"phone": "+79990001122", "apikey": "123456"},
            "discord": {"webhook_url": DISCORD_URL},
        }
    }
    results = notifiers.send_all(secrets, "x")
    assert results["discord"] == (True, "")
    assert results["whatsapp"] == (False, "WhatsApp: внутренняя ошибка (ZeroDivisionError)")


def test_long_messages_are_split_on_lines():
    text = "\n".join(f"строка {i:04d}" for i in range(400))
    parts = chunks(text, 1000)
    assert len(parts) > 1
    assert all(len(part) <= 1000 for part in parts)
    assert "\n".join(parts) == text
    assert chunks("x" * 2500, 1000) == ["x" * 1000, "x" * 1000, "x" * 500]


def test_disabled_channel_is_skipped(http):
    _, seen, _ = http
    secrets = {"notifiers": {"discord": {"webhook_url": DISCORD_URL, "enabled": False}}}
    assert notifiers.send_all(secrets, "x") == {}
    assert seen == []
    assert notifiers.health(secrets, {}) is None
    assert notifiers.summary(secrets, {}) == "уведомления: не подключены"


# --- settings -------------------------------------------------------------------------------


def test_validation_explains_mistakes():
    values, errors = notifiers.validate("discord", {"webhook_url": "https://example.com/hook"}, None)
    assert errors == [
        "Адрес вебхука: адрес должен начинаться с https://discord.com/api/webhooks/ — скопируйте его кнопкой в Discord"
    ]
    _, errors = notifiers.validate("whatsapp", {"phone": "", "apikey": ""}, None)
    assert errors == ["Ваш номер WhatsApp: заполните поле", "apikey от CallMeBot: заполните поле"]
    values, errors = notifiers.validate("telegram", {"token": TG_TOKEN, "chat_ids": "111, -222;333"}, None)
    assert errors == []
    assert values == {"token": TG_TOKEN, "chat_ids": ["111", "-222", "333"]}


def test_empty_secret_keeps_the_saved_one():
    values, errors = notifiers.validate("telegram", {"token": "", "chat_ids": "111"}, {"token": TG_TOKEN})
    assert errors == []
    assert values["token"] == TG_TOKEN


def test_ntfy_defaults_and_suggestion():
    values, errors = notifiers.validate("ntfy", {"topic": "tow-abcdef123456", "server": ""}, None)
    assert errors == []
    assert values == {"topic": "tow-abcdef123456", "server": "https://ntfy.sh"}
    (card,) = [c for c in notifiers.cards({}, {}) if c["kind"] == "ntfy"]
    topic = card["fields"][0]
    assert topic["kind"] == "text"
    assert topic["suggestion"].startswith("tow-")
    assert len(topic["suggestion"]) == 16


def test_cards_never_carry_secret_values():
    secrets = {"telegram": {"token": TG_TOKEN, "chat_ids": ["111"]}, **DISCORD}
    rendered = json.dumps(notifiers.cards(secrets, {}), ensure_ascii=False)
    assert TG_TOKEN not in rendered
    assert DISCORD_URL not in rendered
    assert '"saved": true' in rendered


def test_store_and_remove_keep_telegram_where_it_always_was():
    secrets: dict = {}
    notifiers.store(secrets, "telegram", {"token": TG_TOKEN, "chat_ids": ["1"]})
    notifiers.store(secrets, "discord", {"webhook_url": DISCORD_URL})
    assert secrets == {"telegram": {"token": TG_TOKEN, "chat_ids": ["1"]}, **DISCORD}
    notifiers.remove(secrets, "telegram")
    notifiers.remove(secrets, "discord")
    assert secrets == {"notifiers": {}}


def test_check_button_needs_saved_settings(http):
    _, seen, _ = http
    assert notifiers.test({}, "discord") == (False, "Discord: сначала заполните и сохраните поля")
    assert seen == []


def test_check_button_does_not_queue(http):
    set_handler, _, _ = http
    set_handler(lambda _r: httpx.Response(404))
    ok, message = notifiers.test(DISCORD, "discord")
    assert not ok
    assert "вебхук удалён" in message
    assert "notify_outbox" not in load_state()


# --- the settings page ----------------------------------------------------------------------


def _client() -> TestClient:
    from tow.web import app

    return TestClient(app, headers={"Origin": "http://127.0.0.1"})


def _flash(response: httpx.Response) -> str:

    return flash_of(response.headers["location"])


def test_settings_page_shows_a_card_per_messenger():
    page = _client().get("/settings").text
    for kind, title in (("telegram", "Telegram"), ("discord", "Discord"), ("whatsapp", "WhatsApp"), ("ntfy", "ntfy")):
        assert f'id="notifier-{kind}"' in page
        assert title in page
    assert "Копировать URL вебхука" in page
    assert "I allow callmebot to send me messages" in page
    assert "пока не поддерживаются" not in page


def test_save_check_and_remove_from_the_page(http):
    set_handler, seen, _ = http
    set_handler(lambda _r: httpx.Response(204))
    c = _client()

    bad = c.post("/settings/notifier/discord", data={"webhook_url": "https://example.com/x"}, follow_redirects=False)
    assert bad.status_code == 303
    assert _flash(bad).startswith("Discord: не сохранено — Адрес вебхука: адрес должен")
    assert "notifiers" not in load_secrets()

    saved = c.post("/settings/notifier/discord", data={"webhook_url": DISCORD_URL}, follow_redirects=False)
    assert _flash(saved) == "Discord: сохранено — нажмите «Проверить»"
    assert saved.headers["location"].endswith("#notifier-discord")
    assert load_secrets()["notifiers"]["discord"] == {"webhook_url": DISCORD_URL}

    page = c.get("/settings").text
    assert DISCORD_URL not in page
    assert 'action="/settings/notifier/discord/test"' in page

    checked = c.post("/settings/notifier/discord/test", follow_redirects=False)
    assert _flash(checked) == "Discord: проверочное сообщение отправлено"
    assert json.loads(seen[-1].content)["content"] == notifiers.check_message()

    removed = c.post("/settings/notifier/discord/remove", follow_redirects=False)
    assert _flash(removed) == "Discord: отключён"
    assert "discord" not in (load_secrets().get("notifiers") or {})

    c.post("/undo", follow_redirects=False)
    assert load_secrets()["notifiers"]["discord"] == {"webhook_url": DISCORD_URL}


def test_saving_again_with_an_empty_secret_keeps_it():
    save_secrets({"telegram": {"token": TG_TOKEN, "chat_ids": ["1"]}})
    response = _client().post(
        "/settings/notifier/telegram", data={"token": "", "chat_ids": "111, 222"}, follow_redirects=False
    )
    assert _flash(response) == "Telegram: сохранено — нажмите «Проверить»"
    assert load_secrets()["telegram"] == {"token": TG_TOKEN, "chat_ids": ["111", "222"]}


def test_unknown_messenger_is_refused():
    response = _client().post("/settings/notifier/icq", data={}, follow_redirects=False)
    assert _flash(response) == "неизвестный мессенджер"


# --- the watchdog retries the queue ---------------------------------------------------------


def test_watchdog_flushes_the_queue(http):
    from tow import watchdog
    from tow.store import save_state

    set_handler, seen, _ = http
    set_handler(lambda _r: httpx.Response(204))
    save_secrets(DISCORD)
    save_state({"notify_outbox": {"discord": ["серия 1"]}})  # the 1.11-1.13 format is still read
    watchdog._flush_queued_messages()
    assert json.loads(seen[0].content)["content"] == "Не доставлено ранее:\n\nсерия 1"
    assert "notify_outbox" not in load_state()

    seen.clear()
    watchdog._flush_queued_messages()  # nothing queued: no traffic
    assert seen == []


def _card_tag(page: str, kind: str) -> str:
    start = page.index(f'id="notifier-{kind}"')
    return page[page.rindex("<", 0, start) : page.index(">", start) + 1]


def test_only_connected_messengers_are_expanded():
    save_secrets(DISCORD)
    page = _client().get("/settings").text
    assert _card_tag(page, "discord").startswith("<details")
    assert " open" in _card_tag(page, "discord")
    for kind in ("telegram", "whatsapp", "ntfy"):
        assert " open" not in _card_tag(page, kind), kind


def test_the_messenger_just_saved_stays_open_even_if_not_connected():
    c = _client()
    bad = c.post("/settings/notifier/whatsapp", data={"phone": "12", "apikey": ""}, follow_redirects=False)
    assert "&card=whatsapp&flash=" in bad.headers["location"]
    assert bad.headers["location"].endswith("#notifier-whatsapp")
    page = c.get("/settings?open=bots&card=whatsapp").text
    assert " open" in _card_tag(page, "whatsapp")
    assert " open" not in _card_tag(page, "ntfy")


def test_settings_show_the_watchdogs_last_message():
    from tow.paths import data_dir

    page = _client().get("/settings").text
    assert "Сторож следит за расписанием" in page
    assert "сам сторож TOW не перезапускает" in page
    assert "Последнее сообщение сторожа" not in page
    (data_dir() / "watchdog.json").write_text(
        json.dumps(
            {"at": 1790000000, "last_problem": {"at": 1790000000, "text": "TOW завис и не отвечал — сторож…\nвторая"}}
        ),
        encoding="utf-8",
    )
    page = _client().get("/settings").text
    assert "Последнее сообщение сторожа" in page
    assert "<li>TOW завис и не отвечал — сторож…</li><li>вторая</li>" in page


def test_header_bell_changes_colour():
    from tow.store import save_state

    def bell() -> str:
        page = _client().get("/settings").text
        start = page.index('class="trk trk-ico')
        return page[start : page.index(">", start)]

    assert 'trk-ico mut"' in bell()  # nothing connected: grey
    save_secrets(DISCORD)
    assert 'trk-ico ok"' in bell()  # connected, no failure yet: green
    save_state({"topics": [], "notify_status": {"discord": {"ok": False, "at": 1, "error": "x"}}})
    assert 'trk-ico bad"' in bell()  # last delivery failed: red
    assert 'title="уведомления: Discord ✗"' in bell()


# --- delivery guarantees (1.14) -------------------------------------------------------------


def test_telegram_recipients_are_independent(http):
    set_handler, seen, _ = http

    def handler(request: httpx.Request) -> httpx.Response:
        chat = dict(httpx.QueryParams(request.content.decode()))["chat_id"]
        if chat == "222":
            return httpx.Response(403, json={"ok": False})
        return httpx.Response(200, json={"ok": True})

    set_handler(handler)
    secrets = {"telegram": {"token": TG_TOKEN, "chat_ids": ["111", "222"]}}
    first = notifiers.send_all(secrets, "серия 1")
    assert first["telegram"][0] is False
    assert "получатель 222 недоступен" in first["telegram"][1]
    seen.clear()
    notifiers.send_all(secrets, "серия 2")
    sent_to_111 = [dict(httpx.QueryParams(r.content.decode())) for r in seen if b"chat_id=111" in r.content]
    assert [row["text"] for row in sent_to_111] == ["серия 2"]  # no repeats for the chat that works
    assert not [r for r in seen if b"chat_id=222" in r.content]  # held until the settings change
    assert notifiers.summary(secrets, load_state()) == "уведомления: Telegram ✗"


def test_a_long_message_resumes_where_it_broke(http, monkeypatch):
    set_handler, seen, sleeps = http
    monkeypatch.setattr(notifiers.kinds()["whatsapp"], "MAX_LEN", 20)
    text = "\n".join(f"строка {n:02d} ......" for n in range(3))  # three parts of up to 20 chars
    answers = iter(
        [
            httpx.Response(200, text="Message queued"),
            httpx.Response(200, text="Please wait 30 seconds"),  # CallMeBot: busy -> transient
            httpx.Response(200, text="Message queued"),
            httpx.Response(200, text="Message queued"),
        ]
    )
    set_handler(lambda _r: next(answers))
    secrets = {"notifiers": {"whatsapp": {"phone": "+79990001122", "apikey": "123456"}}}
    ((ok, _),) = notifiers.send_all(secrets, text).values()
    assert not ok
    assert [dict(r.url.params)["text"] for r in seen] == ["строка 00 ......", "строка 01 ......"]
    assert sleeps == [3.0]  # the gap CallMeBot asks for
    seen.clear()
    assert notifiers.flush_outbox(secrets) == {"whatsapp": (True, "")}
    assert [dict(r.url.params)["text"] for r in seen] == ["строка 01 ......", "строка 02 ......"]
    assert "notify_outbox" not in load_state()


def test_a_queue_another_process_is_sending_is_left_alone(http):
    from tow.store import save_state

    _, seen, _ = http
    save_state(
        {
            "notify_outbox": {"discord": {"items": [{"id": "a", "text": "x"}]}},
            "notify_lease": {"discord": {"pid": -1, "until": 9_999_999_999}},
        }
    )
    results = notifiers.flush_outbox(DISCORD)
    assert results["discord"][0] is False
    assert "другим процессом" in results["discord"][1]
    assert seen == []


def test_fixed_settings_release_a_held_queue(http):
    set_handler, seen, _ = http
    set_handler(lambda _r: httpx.Response(404))
    notifiers.send_all(DISCORD, "серия 1")
    seen.clear()
    assert notifiers.flush_outbox(DISCORD)["discord"][0] is False
    assert seen == []  # held: the webhook is still the deleted one
    set_handler(lambda _r: httpx.Response(204))
    fixed = {"notifiers": {"discord": {"webhook_url": DISCORD_URL + "-new"}}}
    assert notifiers.flush_outbox(fixed) == {"discord": (True, "")}
    assert json.loads(seen[0].content)["content"] == "Не доставлено ранее:\n\nсерия 1"


def test_a_message_is_saved_before_it_is_sent(http, monkeypatch):
    from tow.notifiers import outbox

    def crash(*_a, **_k):
        raise SystemExit("TOW stopped")

    monkeypatch.setattr(outbox, "flush", crash)
    monkeypatch.setattr(notifiers, "flush", crash)
    with pytest.raises(SystemExit):
        notifiers.send_all(DISCORD, "серия 1")
    assert [i["text"] for i in load_state()["notify_outbox"]["discord"]["items"]] == ["серия 1"]


def test_watchdog_hands_over_messages_a_stopped_check_left(http):
    from tow import watchdog
    from tow.store import save_state

    set_handler, seen, _ = http
    set_handler(lambda _r: httpx.Response(204))
    save_secrets(DISCORD)
    save_state({"notify_pending": [{"id": "p1", "text": "серия 3 добавлена", "operation_id": "x", "topic_id": "t"}]})
    watchdog._flush_queued_messages()
    assert [json.loads(r.content)["content"] for r in seen] == ["серия 3 добавлена"]
    state = load_state()
    assert "notify_pending" not in state
    assert "notify_outbox" not in state


@pytest.mark.parametrize(
    ("code", "kind"),
    [(401, "held"), (403, "held"), (404, "held"), (429, "retry"), (500, "retry"), (503, "retry"), (400, "drop")],
)
def test_only_settings_errors_hold_the_queue(code, kind):
    error = base.status_error(code, "x")
    assert ("held" if not error.transient and not error.drop else "retry" if error.transient else "drop") == kind


def test_a_refused_message_is_dropped_and_the_queue_goes_on(http):
    """A 400 for one message (a text the messenger will not take) never blocks the recipient."""
    set_handler, seen, _ = http
    set_handler(lambda r: httpx.Response(400 if "плохое" in json.loads(r.content)["content"] else 204))
    notifiers.send_all(DISCORD, "плохое")  # refused on its own: dropped
    notifiers.send_all(DISCORD, "серия 1")
    contents = [json.loads(r.content)["content"] for r in seen]
    assert contents == ["плохое", "серия 1"]
    assert "notify_outbox" not in load_state()  # nothing held, nothing left

    from tow.log import read_events

    refused = [e for e in read_events(limit=50) if e.get("kind") == "bot_delivery_failed"]
    assert refused[0]["status"] == "dropped"  # with the reason recorded


def test_a_refused_combined_message_is_split_to_find_the_bad_one(http):
    from tow.notifiers import outbox

    set_handler, seen, _ = http
    set_handler(lambda _r: httpx.Response(503))
    notifiers.send_all(DISCORD, "серия 1")
    notifiers.send_all(DISCORD, "плохое")
    notifiers.send_all(DISCORD, "серия 2")  # three waiting, sent together next time
    assert len(load_state()["notify_outbox"]["discord"]["items"]) == 3
    seen.clear()
    set_handler(lambda r: httpx.Response(400 if "плохое" in json.loads(r.content)["content"] else 204))
    assert outbox.MAX_ROUNDS >= 4  # combined, then each of the three, in one pass
    notifiers.flush_outbox(DISCORD)
    contents = [json.loads(r.content)["content"] for r in seen]
    assert "серия 1" in contents[1]
    assert "серия 2" in contents[-1]
    assert sum("плохое" in text for text in contents) == 2  # combined, then alone - then dropped
    assert "notify_outbox" not in load_state()


def test_a_server_error_keeps_the_message_for_later_without_holding_the_queue(http):
    set_handler, _seen, _ = http
    set_handler(lambda _r: httpx.Response(500))
    notifiers.send_all(DISCORD, "серия 1")
    box = load_state()["notify_outbox"]["discord"]
    assert [i["text"] for i in box["items"]] == ["серия 1"]
    assert "blocked" not in box
    set_handler(lambda _r: httpx.Response(204))
    assert notifiers.flush_outbox(DISCORD) == {"discord": (True, "")}


def test_ntfy_parts_are_measured_in_utf8_bytes(http):
    from tow.notifiers import ntfy, outbox

    _set_handler, seen, _ = http
    text = "\n".join(["Серия готова, можно смотреть"] * 300)  # ~16 000 bytes, ~8 400 characters
    parts = outbox.parts_for(ntfy, text)
    assert len(parts) > 1
    assert all(len(part.encode("utf-8")) <= ntfy.MAX_BYTES for part in parts)
    assert "\n".join(parts) == text
    long_line = "я" * 5000  # no line breaks at all: cut by bytes, never inside a letter
    cut = outbox.parts_for(ntfy, long_line)
    assert "".join(cut) == long_line
    assert all(len(part.encode("utf-8")) <= ntfy.MAX_BYTES for part in cut)

    notifiers.send_all({"notifiers": {"ntfy": {"topic": "tow-abcdef123456"}}}, text)
    assert all(len(r.content) <= 4096 for r in seen)


def _pending(*texts):
    return [{"id": f"p{n}", "text": text, "operation_id": f"op{n}", "topic_id": "t"} for n, text in enumerate(texts)]


def test_two_dispatchers_never_send_the_same_message(http):
    """The watchdog and a check flushing at once: every staged message goes out exactly once."""
    import threading

    from tow import delivery
    from tow.store import save_state

    set_handler, seen, _ = http
    set_handler(lambda _r: httpx.Response(204))
    save_secrets(DISCORD)
    texts = ("серия 1", "серия 2", "серия 3")
    save_state({"notify_pending": _pending(*texts)})
    second_done = threading.Event()
    calls: list[str] = []

    def first_send(*, text, operation_id, topic):
        calls.append("first:" + text)
        notifiers.flush_outbox(DISCORD)
        if text == texts[0]:
            # The second dispatcher runs while the first is still in its delivery loop.
            worker = threading.Thread(target=delivery.dispatch, args=(second_send,))
            worker.start()
            worker.join(10)
            second_done.set()
        return True

    def second_send(*, text, operation_id, topic):
        calls.append("second:" + text)
        notifiers.flush_outbox(DISCORD)
        return True

    assert delivery.dispatch(first_send) == 3
    assert second_done.is_set()
    assert calls == ["first:серия 1", "first:серия 2", "first:серия 3"]  # the second found nothing
    delivered = "\n".join(json.loads(r.content)["content"] for r in seen)
    assert [delivered.count(text) for text in texts] == [1, 1, 1]
    assert "notify_pending" not in load_state()


def test_a_dispatcher_stopped_after_the_hand_over_loses_nothing_and_repeats_nothing(http):
    from tow import delivery, watchdog
    from tow.store import save_state

    set_handler, seen, _ = http
    set_handler(lambda _r: httpx.Response(204))
    save_secrets(DISCORD)
    save_state({"notify_pending": _pending("серия 7")})

    def crash(**_kwargs):
        raise SystemExit("TOW stopped right after the hand-over")

    with pytest.raises(SystemExit):
        delivery.dispatch(crash)
    state = load_state()
    assert "notify_pending" not in state  # handed over in the same write...
    assert [i["text"] for i in state["notify_outbox"]["discord"]["items"]] == ["серия 7"]  # ...to the queue
    watchdog._flush_queued_messages()
    watchdog._flush_queued_messages()
    assert [json.loads(r.content)["content"] for r in seen] == ["серия 7"]


def test_a_check_delivery_is_audited_per_message(http, monkeypatch):
    from tow import delivery
    from tow.store import save_state

    set_handler, seen, _ = http
    set_handler(lambda _r: httpx.Response(204))
    save_secrets(DISCORD)
    save_state({"notify_pending": _pending("серия 1", "серия 2")})
    logged = []
    monkeypatch.setattr(
        check_notices, "log_event", lambda kind, **fields: logged.append((kind, fields.get("operation_id")))
    )

    delivery.dispatch(lambda **kwargs: check_notices._audited_send(DISCORD, **kwargs))

    assert len(seen) == 1  # both went out together, once
    assert [entry for entry in logged if entry[0] == "bot_delivery_succeeded"] == [
        ("bot_delivery_succeeded", "op0"),
        ("bot_delivery_succeeded", "op1"),
    ]


def _two_threads_flush(http, monkeypatch):
    """Thread A is inside a slow send when thread B of the same process flushes the same queue.

    A's answer is held until B has either finished or is waiting for the in-process lock, so B
    always runs while A waits for the messenger - on a busy machine too, without a sleep."""
    import threading

    set_handler, seen, _ = http
    inside, release, b_waits_or_done = threading.Event(), threading.Event(), threading.Event()

    def slow(_request):
        inside.set()
        assert release.wait(10)  # a slow messenger: B runs while A waits for the answer
        return httpx.Response(204)

    set_handler(slow)
    from tow.notifiers import outbox
    from tow.store import load_state, persistence_lock, save_state

    class _ObservedLock:
        """The recipient's lock; tells when a caller has to wait for it."""

        def __init__(self, lock):
            self._lock = lock

        def acquire(self, timeout=-1):
            if self._lock.acquire(blocking=False):
                return True
            b_waits_or_done.set()
            return self._lock.acquire(timeout=timeout)

        def release(self):
            self._lock.release()

    local_lock = outbox._local_lock
    monkeypatch.setattr(outbox, "_local_lock", lambda key: _ObservedLock(local_lock(key)))

    with persistence_lock():
        state = load_state()
        outbox.enqueue(state, DISCORD, "серия 1")
        save_state(state)
    results: dict[str, dict] = {}

    def flush_b():
        try:
            results["b"] = notifiers.flush_outbox(DISCORD)
        finally:
            b_waits_or_done.set()

    first = threading.Thread(target=lambda: results.__setitem__("a", notifiers.flush_outbox(DISCORD)))
    first.start()
    assert inside.wait(10)
    second = threading.Thread(target=flush_b)
    second.start()
    assert b_waits_or_done.wait(10)
    release.set()
    first.join(10)
    second.join(10)
    return seen, results


def test_two_threads_of_one_process_send_a_message_once(http, monkeypatch):
    seen, results = _two_threads_flush(http, monkeypatch)

    assert [json.loads(r.content)["content"] for r in seen] == ["серия 1"]
    assert results["a"] == {"discord": (True, "")}
    assert results["b"] == {}  # it waited for A, then found nothing left to send
    assert "notify_outbox" not in load_state()


def test_the_lease_alone_keeps_a_second_thread_out(http, monkeypatch):
    """Without the in-process lock the lease still holds: its token names the claim, not the process."""
    import threading

    from tow.notifiers import outbox

    monkeypatch.setattr(outbox, "_local_lock", lambda _key: threading.Lock())  # every caller its own lock
    seen, results = _two_threads_flush(http, monkeypatch)

    assert len(seen) == 1
    assert results["b"]["discord"][0] is False
    assert "другим процессом" in results["b"]["discord"][1]
    assert results["a"] == {"discord": (True, "")}


def test_a_dispatcher_whose_lease_was_taken_over_writes_nothing(http):
    from tow.notifiers import outbox
    from tow.store import save_state

    save_state({"notify_outbox": {"discord": {"items": [{"id": "a", "text": "x"}]}}})
    first, second = outbox._new_token(), outbox._new_token()
    assert first != second  # two claims of the same thread differ
    claim, _inflight = outbox._claim("discord", DISCORD["notifiers"]["discord"], first)
    assert claim == "send"
    state = load_state()
    state["notify_lease"]["discord"]["until"] = 0  # A stalled past its lease ...
    save_state(state)
    assert outbox._claim("discord", DISCORD["notifiers"]["discord"], second)[0] == "send"  # ... B took over

    assert outbox._progress("discord", first, finished=True) is False
    assert [i["id"] for i in load_state()["notify_outbox"]["discord"]["items"]] == ["a"]  # untouched
    assert outbox._progress("discord", second, finished=True) is True
    assert "notify_outbox" not in load_state()


def test_a_lease_written_by_an_older_tow_still_counts(http):
    from tow.notifiers import outbox

    lease = {"pid": -1, "until": 9_999_999_999}
    assert outbox._lease_held_by_other(lease, outbox._new_token(), 0.0) is True
    assert outbox._lease_held_by_other({**lease, "until": 1}, outbox._new_token(), 2.0) is False


def test_notification_title_limits_untrusted_text_before_regex_matching():
    from tow.notify import short_series_title

    title = "Series" + " " * 100_000 + "[" + "x" * 100_000
    assert short_series_title(title) == "Series"
