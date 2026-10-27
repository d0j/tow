"""Settings → Notifications → Quiet hours and digest (1.35.0): config.yaml's ``quiet_hours`` and
``daily_digest_hour`` in the web UI. The card renders what is saved, saves in place (an empty field
takes its key out) by config.yaml's own rules, merges with another device's save, is undone by Undo
and may be changed from a device signed in from the network, as the messengers are."""

from __future__ import annotations

import re
from datetime import datetime
from html import unescape
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from helpers import flash_kind, flash_of, open_network
from test_edit_conflicts import form_of

from tow.config import config_path, load_config, save_config
from tow.web import app

ACTION = "/settings/quiet-hours"


@pytest.fixture
def two():
    cfg = load_config()
    cfg["language"] = "en"
    save_config(cfg)
    a = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    b = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    return a, b


def card(client: TestClient, location: str = "/settings") -> str:
    page = client.get(location).text
    found = re.search(r'<details class="integration-card quiet-card".*?</details>', page, re.DOTALL)
    assert found is not None
    return found.group(0)


def form(client: TestClient, location: str = "/settings") -> dict[str, str]:
    return form_of(client.get(location).text, "quiet-form")


def post(client: TestClient, fields: dict[str, str]):
    return client.post(ACTION, data=fields, follow_redirects=False)


def save(client: TestClient, start: str, end: str, hour: str):
    return post(client, {**form(client), "quiet_from": start, "quiet_to": end, "daily_digest_hour": hour})


def stored() -> tuple[object, object]:
    cfg = load_config()
    return cfg.get("quiet_hours"), cfg.get("daily_digest_hour")


def value_of(html: str, field_id: str) -> str:
    found = re.search(rf'<input id="{field_id}"[^>]*\bvalue="([^"]*)"', html)
    assert found is not None, field_id
    return found.group(1)


def tag_of(html: str, field_id: str) -> str:
    found = re.search(rf'<input id="{field_id}"[^>]*>', html)
    assert found is not None, field_id
    return found.group(0)


# --- the card ---------------------------------------------------------------------------------


def test_the_card_without_quiet_hours_or_digest_is_folded_and_says_off(two):
    a, _b = two
    html = card(a)
    assert "Quiet hours and digest" in html
    assert re.match(r'<details class="integration-card quiet-card" id="quiet-hours">', html)  # folded
    assert '<span class="pill mut">Off</span>' in html
    assert [value_of(html, name) for name in ("quiet-from", "quiet-to", "digest-hour")] == ["", "", ""]
    for name in ("quiet-from", "quiet-to", "digest-hour"):
        tag = tag_of(html, name)
        assert 'class="w-xs"' in tag
        assert 'type="number"' in tag
        assert 'min="0"' in tag
        assert 'max="23"' in tag
    assert "e.g. 23 to 8" in html  # the example, as common.example says one
    assert "local time of this computer" in html
    # One solid button: the Save.
    assert re.findall(r"<button[^>]*>", html) == ['<button type="submit">']
    # The from/to pair shares one row.
    row = re.search(
        r'<div class="field-row">(.*?)</div>\s*</div>\s*<p class="field-hint" id="quiet-hint">', html, re.DOTALL
    )
    assert row is not None
    assert 'id="quiet-from"' in row.group(1)
    assert 'id="quiet-to"' in row.group(1)


def test_the_card_shows_the_saved_values_and_is_open(two):
    a, _b = two
    cfg = load_config()
    cfg.update(quiet_hours="23-8", daily_digest_hour=9)
    save_config(cfg)
    html = card(a)
    assert re.match(r'<details class="integration-card quiet-card" id="quiet-hours" open>', html)
    assert [value_of(html, name) for name in ("quiet-from", "quiet-to", "digest-hour")] == ["23", "8", "9"]
    assert "Quiet 23–8 · Digest after 9:00" in html


def test_an_end_of_24_shows_as_0_and_an_untouched_save_keeps_it(two):
    a, _b = two
    cfg = load_config()
    cfg["quiet_hours"] = "22-24"
    save_config(cfg)
    assert [value_of(card(a), name) for name in ("quiet-from", "quiet-to")] == ["22", "0"]
    response = post(a, {**form(a), "daily_digest_hour": "7"})
    assert flash_of(response.headers["location"]) == "quiet hours and digest saved"
    assert stored() == ("22-24", 7)  # the quiet hours the owner left alone are not rewritten
    from tow.log import read_events

    event = next(e for e in read_events(limit=20) if e.get("kind") == "settings_quiet_hours")
    assert "quiet_hours" not in event  # History says what the save set, not what it left alone
    assert event["daily_digest_hour"] == 7


@pytest.mark.parametrize(
    ("value", "words", "shown"),
    [
        ("8-8", '"8-8" does not name a part of the day', ("8", "8")),
        ("night", 'quiet_hours must look like "23-8"', ("", "")),
    ],
)
def test_an_invalid_stored_value_shows_as_it_is_with_the_config_error(two, monkeypatch, value, words, shown):
    """config.yaml is validated when it is read, so the page meets such a value only from a
    config it did not read itself; it must still draw, never crash."""
    from tow.web import services

    a, _b = two
    real = services.load_config

    def unvalidated() -> dict:
        return {**real(), "quiet_hours": value}

    monkeypatch.setattr(services, "load_config", unvalidated)
    response = a.get("/settings")
    assert response.status_code == 200
    html = unescape(card(a))
    assert words in html
    assert '<span class="pill bad">Error in config.yaml</span>' in html
    assert (value_of(html, "quiet-from"), value_of(html, "quiet-to")) == shown
    if value == "night":
        assert "(hours, local time). config.yaml holds “night”." in html


# --- saving -----------------------------------------------------------------------------------


def test_save_on_change_and_off_writes_in_place_and_takes_the_keys_out(two):
    from tow.log import format_event, read_events

    a, _b = two
    path = config_path()
    path.write_text(path.read_text(encoding="utf-8") + "# the owner's own note\n", encoding="utf-8")

    response = save(a, "23", "8", "9")
    assert flash_kind(response.headers["location"]) == "ok"
    assert flash_of(response.headers["location"]) == "quiet hours and digest saved"
    assert "#quiet-hours" in response.headers["location"]
    assert "card=quiet" in response.headers["location"]
    assert stored() == ("23-8", 9)
    text = path.read_text(encoding="utf-8")
    assert 'quiet_hours: "23-8"' in text
    assert "daily_digest_hour: 9" in text
    assert "# the owner's own note" in text  # saved in place: the comments stay
    event = next(e for e in read_events(limit=20) if e.get("kind") == "settings_quiet_hours")
    assert (event["quiet_hours"], event["daily_digest_hour"], event["how"]) == ("23-8", 9, "manual")
    line = format_event(event)
    assert line["label"] == "Quiet hours and digest"
    assert "quiet hours 23–8" in line["detail"]
    assert "digest after 9:00" in line["detail"]

    save(a, "22", "7", "9")
    assert stored() == ("22-7", 9)

    response = save(a, "", "", "")
    assert flash_of(response.headers["location"]) == "quiet hours and digest saved"
    assert stored() == (None, None)
    text = path.read_text(encoding="utf-8")
    assert "quiet_hours" not in text
    assert "daily_digest_hour" not in text
    assert "# the owner's own note" in text
    event = next(e for e in read_events(limit=20) if e.get("kind") == "settings_quiet_hours")
    assert "no quiet hours" in format_event(event)["detail"]
    assert "no daily digest" in format_event(event)["detail"]


def test_a_value_written_by_hand_with_spaces_is_replaced_whole(two):
    """The in-place edit cannot take ``"23 - 8"  # night`` apart: the config is written anew instead."""
    a, _b = two
    path = config_path()
    path.write_text(path.read_text(encoding="utf-8") + 'quiet_hours: "23 - 8"  # night\n', encoding="utf-8")
    assert [value_of(card(a), name) for name in ("quiet-from", "quiet-to")] == ["23", "8"]
    save(a, "22", "7", "")
    assert stored() == ("22-7", None)


def test_the_same_values_again_change_nothing(two):
    a, _b = two
    save(a, "23", "8", "")
    response = save(a, "23", "8", "")
    assert flash_of(response.headers["location"]) == "no changes"


def test_the_save_is_in_recent_messages(two):
    from tow import messages_feed

    a, _b = two
    save(a, "23", "8", "9")
    payload = messages_feed.render(messages_feed.feed().refresh().projection, lang="en")
    assert any("Quiet hours and digest saved" in str(item) for item in payload["items"])


@pytest.mark.parametrize(
    ("start", "end", "hour", "words", "field"),
    [
        ("8", "8", "", "the quiet hours must start and end at different hours", "quiet_from"),
        ("24", "8", "", "an hour is a whole number from 0 to 23", "quiet_from"),
        ("ab", "8", "", "an hour is a whole number from 0 to 23", "quiet_from"),
        ("23", "", "", "give both hours, from and to (or leave both empty)", "quiet_from"),
        ("", "8", "", "give both hours, from and to (or leave both empty)", "quiet_to"),
        ("23", "8", "24", "an hour is a whole number from 0 to 23", "daily_digest_hour"),
        ("23", "8", "nine", "an hour is a whole number from 0 to 23", "daily_digest_hour"),
    ],
)
def test_a_refused_save_says_why_in_plain_words_saves_nothing_and_keeps_what_was_typed(
    two, start, end, hour, words, field
):
    a, _b = two
    response = save(a, start, end, hour)
    location = response.headers["location"]
    assert flash_kind(location) == "err"
    assert flash_of(location) == f"not saved: {words}"  # the form's words, never config.yaml's
    assert stored() == (None, None)
    query = parse_qs(urlsplit(location).query)
    assert query["field"] == [field]
    assert query["card"] == ["quiet"]
    html = card(a, location)
    assert re.match(r'<details class="integration-card quiet-card" id="quiet-hours" open>', html)
    assert [value_of(html, name) for name in ("quiet-from", "quiet-to", "digest-hour")] == [start, end, hour]
    marked = {"quiet_from": "quiet-from", "quiet_to": "quiet-to", "daily_digest_hour": "digest-hour"}[field]
    assert 'aria-invalid="true" autofocus' in tag_of(html, marked)
    # Fixed and sent again from that page, it saves (on the base the card was drawn with).
    retry = post(a, {**form(a, location), "quiet_from": "23", "quiet_to": "8", "daily_digest_hour": "9"})
    assert flash_of(retry.headers["location"]) == "quiet hours and digest saved"
    assert stored() == ("23-8", 9)


def test_a_save_without_its_base_is_refused(two, strict_edit_base):
    a, _b = two
    response = post(a, {"quiet_from": "23", "quiet_to": "8", "daily_digest_hour": ""})
    assert flash_kind(response.headers["location"]) == "err"
    assert stored() == (None, None)


# --- two devices, Undo, the network -----------------------------------------------------------


def test_another_devices_digest_survives_a_save_of_the_quiet_hours(two):
    a, b = two
    b_form = form(b)
    save(a, "", "", "9")
    response = post(b, {**b_form, "quiet_from": "23", "quiet_to": "8"})  # B's digest field still empty
    assert "conflict=" not in response.headers["location"]
    assert stored() == ("23-8", 9)


def test_quiet_hours_changed_on_both_sides_are_a_conflict_that_names_both(two):
    a, b = two
    b_form = form(b)
    save(a, "22", "7", "")
    location = post(b, {**b_form, "quiet_from": "21", "quiet_to": "6"}).headers["location"]
    assert "conflict=" in location
    assert stored() == ("22-7", None)
    html = card(b, location)
    assert "Saved now: 22–7" in html
    assert "Yours: 21–6" in html
    assert [value_of(html, name) for name in ("quiet-from", "quiet-to")] == ["21", "6"]  # the draft
    chosen = post(b, {**form(b, location), "resolve_quiet": "mine"})
    assert "conflict=" not in chosen.headers["location"]
    assert stored() == ("21-6", None)


def test_undo_puts_back_what_was_saved_before(two):
    a, _b = two
    save(a, "23", "8", "9")
    undone = a.post("/undo", data={}, follow_redirects=False)
    assert undone.status_code == 303
    assert "card=quiet" in undone.headers["location"]
    assert stored() == (None, None)
    assert "quiet_hours" not in config_path().read_text(encoding="utf-8")

    save(a, "23", "8", "9")
    save(a, "1", "6", "")
    a.post("/undo", data={}, follow_redirects=False)
    assert stored() == ("23-8", 9)


def test_a_device_signed_in_from_the_network_may_change_them(two, monkeypatch):
    from tow.auth import issue_session

    open_network(monkeypatch, token="t" * 32)
    lan = TestClient(
        app,
        client=("192.168.1.7", 50000),
        headers={"Origin": "http://127.0.0.1"},
        cookies={"tow_session": issue_session("t" * 32)},
    )
    assert lan.get("/settings").status_code == 200
    response = save(lan, "23", "8", "9")
    assert flash_of(response.headers["location"]) == "quiet hours and digest saved"
    assert stored() == ("23-8", 9)


def test_the_network_without_signing_in_changes_nothing(two, monkeypatch):
    open_network(monkeypatch, token="t" * 32)
    anonymous = TestClient(app, client=("192.168.1.7", 50000), headers={"Origin": "http://127.0.0.1"})
    response = anonymous.post(
        ACTION, data={"quiet_from": "23", "quiet_to": "8", "daily_digest_hour": "9"}, follow_redirects=False
    )
    assert response.status_code in (303, 401, 403)
    assert stored() == (None, None)


# --- delivery reads what was saved ------------------------------------------------------------


def test_delivery_holds_messages_in_the_saved_quiet_hours_and_sends_the_digest_after_its_hour(two):
    from tow import delivery

    a, _b = two
    save(a, "23", "8", "9")
    cfg = load_config()
    assert delivery.quiet_now(cfg, datetime(2026, 10, 7, 23, 30).astimezone()) is True
    assert delivery.quiet_now(cfg, datetime(2026, 10, 7, 12, 0).astimezone()) is False
    sent: list[str] = []

    def send(*, text: str, operation_id: str, topic: object) -> bool:
        sent.append(text)
        return True

    assert delivery.maybe_digest(cfg=cfg, send=send, now=datetime(2026, 10, 7, 8, 30).astimezone()) is False
    assert delivery.maybe_digest(cfg=cfg, send=send, now=datetime(2026, 10, 7, 9, 30).astimezone()) is True
    assert sent

    save(a, "", "", "")
    assert delivery.quiet_now(load_config(), datetime(2026, 10, 7, 23, 30).astimezone()) is False
