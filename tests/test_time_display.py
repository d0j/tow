"""The one time rule of every page (rows, panels, History, Recent messages, popovers, results,
Settings): today "20:31"; another day of this year "15 Oct, 20:31" / "15 окт, 20:31"; another year
"15 Oct 2025, 20:31"; a time still to come always dated. 24 h, in TOW's own zone (its computer's,
``tow.clock.local_zone``), never the browser's. Pages print it with ``short_time`` (a filter and a
global): a <time> whose datetime is the ISO time and whose title is the full time with seconds
and the UTC offset. The page scripts have the same rule (``window.towTime``, tests/js/time_short.mjs).

Before, every time was "15.10.2026 20:31:27 CET UTC+01:00" (31 characters), cut off at 768 px.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from markupsafe import Markup

import tow.clock
from tow import i18n
from tow.web.text import short_time

BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 10, 16, 20, 45, tzinfo=BERLIN)  # 18:45 UTC, summer time (+02:00)


@pytest.fixture(autouse=True)
def _berlin(monkeypatch):
    monkeypatch.setattr(tow.clock, "local_zone", lambda: BERLIN)


def _in(monkeypatch, lang: str) -> None:
    monkeypatch.setattr(i18n, "current", lambda: lang)


def _shown(value: object, now: datetime = NOW) -> str:
    """The visible words of ``short_time``."""
    html = str(short_time(value, now=now))
    return html[html.index(">") + 1 : html.rindex("<")] if html.startswith("<time") else html


@pytest.mark.parametrize(
    ("value", "en", "ru"),
    [
        ("2026-10-16T18:31:00+00:00", "20:31", "20:31"),  # today, stored in UTC
        ("2026-10-16T00:00:00+02:00", "00:00", "00:00"),  # today's first minute
        ("2026-10-15T23:59:00+02:00", "15 Oct, 23:59", "15 окт, 23:59"),  # yesterday: dated
        ("2026-10-15T20:31:27+02:00", "15 Oct, 20:31", "15 окт, 20:31"),
        ("2026-05-03T09:05:00+02:00", "3 May, 09:05", "3 мая, 09:05"),
        ("2026-01-01T00:30:00+01:00", "1 Jan, 00:30", "1 янв, 00:30"),
        ("2025-10-15T20:31:00+02:00", "15 Oct 2025, 20:31", "15 окт 2025, 20:31"),  # another year
        ("2026-10-16T22:00:00+02:00", "16 Oct, 22:00", "16 окт, 22:00"),  # later today: scheduled, dated
        ("2026-10-16T20:45:30+02:00", "20:45", "20:45"),  # within the minute: now, not "to come"
        ("2027-01-02T08:00:00+01:00", "2 Jan 2027, 08:00", "2 янв 2027, 08:00"),  # to come, next year
    ],
)
def test_one_rule_for_today_this_year_another_year_and_what_is_to_come(monkeypatch, value, en, ru):
    _in(monkeypatch, "en")
    assert _shown(value) == en
    _in(monkeypatch, "ru")
    assert _shown(value) == ru


def test_a_time_is_a_time_element_with_its_full_time_as_the_tooltip(monkeypatch):
    _in(monkeypatch, "en")
    assert short_time("2026-10-15T20:31:27+02:00", now=NOW) == Markup(
        '<time datetime="2026-10-15T20:31:27+02:00" title="2026-10-15 20:31:27 UTC+02:00" data-short>'
        "15 Oct, 20:31</time>"
    )
    _in(monkeypatch, "ru")
    assert 'title="15.10.2026 20:31:27 UTC+02:00"' in short_time("2026-10-15T20:31:27+02:00", now=NOW)


def test_the_time_is_in_tows_zone_with_its_summer_time(monkeypatch):
    _in(monkeypatch, "en")
    # 23:30 UTC on the 15th is 01:30 on the 16th in Berlin: today.
    assert _shown("2026-10-15T23:30:00+00:00") == "01:30"
    # Winter: +01:00, said in the tooltip.
    winter = str(short_time("2026-03-01T12:00:00+00:00", now=NOW))
    assert ">1 Mar, 13:00<" in winter
    assert 'datetime="2026-03-01T13:00:00+01:00"' in winter
    assert "UTC+01:00" in winter
    # Another zone for TOW: another clock and another day. 14:00 UTC is today in Berlin (16:00),
    # yesterday in Tokyo, where it is already 03:45 on the 17th.
    assert _shown("2026-10-16T14:00:00+00:00") == "16:00"
    tokyo = ZoneInfo("Asia/Tokyo")
    monkeypatch.setattr(tow.clock, "local_zone", lambda: tokyo)
    assert _shown("2026-10-16T14:00:00+00:00") == "16 Oct, 23:00"
    assert _shown("2026-10-16T18:31:00+00:00") == "03:31"
    # UTC itself is "UTC", without an offset.
    monkeypatch.setattr(tow.clock, "local_zone", lambda: UTC)
    assert 'title="2026-10-16 18:31:00 UTC"' in short_time("2026-10-16T18:31:00+00:00", now=NOW)


def test_every_way_a_time_is_stored_is_read(monkeypatch):
    _in(monkeypatch, "en")
    moment = datetime(2026, 10, 15, 20, 31, tzinfo=BERLIN)
    assert _shown(moment) == "15 Oct, 20:31"
    assert _shown(int(moment.timestamp())) == "15 Oct, 20:31"  # a Unix time (a message's "at")
    assert _shown(moment.timestamp()) == "15 Oct, 20:31"
    assert _shown(str(int(moment.timestamp()))) == "15 Oct, 20:31"
    assert _shown("2026-10-15T20:31:00") == "15 Oct, 20:31"  # an old TOW's local time, no zone
    # TOW 1.24 and before wrote the time out in the page language: 11:16 UTC is 13:16 in Berlin.
    assert _shown("07.10.2026 14:16:13 UTC+03:00") == "7 Oct, 13:16"
    assert _shown("2026-10-07 14:16:13 MSK UTC+03:00") == "7 Oct, 13:16"


def test_nothing_shows_nothing_and_a_text_that_is_no_time_shows_as_it_is(monkeypatch):
    _in(monkeypatch, "en")
    assert short_time(None) == Markup("")
    assert short_time("") == Markup("")
    assert short_time(0) == Markup("")
    assert short_time("soon") == Markup("soon")
    assert short_time("<b>x</b>") == Markup("&lt;b&gt;x&lt;/b&gt;")
    assert short_time(True) == Markup("")  # not a Unix time


def test_pages_have_short_time_and_the_scripts_tows_zone(monkeypatch):
    import json
    import re

    from fastapi.testclient import TestClient

    from tow.web import app
    from tow.web.templating import TEMPLATES

    assert TEMPLATES.env.filters["short_time"] is short_time
    assert TEMPLATES.env.globals["short_time"] is short_time
    page = TestClient(app, headers={"Origin": "http://127.0.0.1"}).get("/").text
    texts = json.loads(re.search(r'<script type="application/json" id="tow-i18n">(.*?)</script>', page).group(1))
    assert texts["time.month_short.10"] == "окт"
    assert texts["time.this_year"] == "{day} {month}, {time}"
    assert texts["time.other_year"] == "{day} {month} {year}, {time}"
    # TOW's zone by name (each moment's own offset in the scripts) and its offset now, in minutes.
    now = tow.clock.machine_now()
    assert texts["_tz_now"] == str(int(now.utcoffset().total_seconds() // 60))
    assert texts["_tz_name"] == "Europe/Berlin"
    assert "_tz_half" not in texts


def test_the_header_poll_carries_tows_offset_now():
    """A page open across a summer/winter time change learns TOW's new offset from the poll it
    already makes (/health.json, signed in), for "today" and its midnight."""
    from fastapi.testclient import TestClient

    from tow.web import app
    from tow.web.text import zone_minutes

    body = TestClient(app, headers={"Origin": "http://127.0.0.1"}).get("/health.json").json()
    assert body["tz_now"] == zone_minutes()
    assert isinstance(body["tz_now"], int)


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_every_month_has_a_short_name(lang):
    names = [i18n.translate(f"time.month_short.{n}", lang) for n in range(1, 13)]
    assert all(name and not name.startswith("time.") for name in names)
    assert len(set(names)) == 12


def test_a_script_gets_the_servers_words_for_a_stored_time(monkeypatch):
    """Without the zone's name a page script cannot know an earlier time's summer or winter
    offset: the JSON answers carry what short_time writes (iso, short, full)."""
    from tow.web.text import time_parts

    _in(monkeypatch, "en")
    assert time_parts("2026-03-01T12:00:00+00:00", now=NOW) == {
        "iso": "2026-03-01T13:00:00+01:00",
        "short": "1 Mar, 13:00",
        "full": "2026-03-01 13:00:00 UTC+01:00",
    }
    assert time_parts("soon") is None
    assert time_parts(None) is None


def test_the_zone_has_a_name_only_when_it_is_known(monkeypatch):
    from tow.web.text import zone_name

    assert zone_name() == "Europe/Berlin"
    monkeypatch.setattr(tow.clock, "local_zone", lambda: UTC)  # a zone without a name
    assert zone_name() == ""
    monkeypatch.setattr(tow.clock, "local_zone", lambda: None)  # this computer's zone
    monkeypatch.delenv("TZ", raising=False)
    assert zone_name() == ""
    monkeypatch.setenv("TZ", "Nowhere/Atlantis")
    assert zone_name() == ""
    monkeypatch.setenv("TZ", "../etc")
    assert zone_name() == ""


def test_the_log_and_download_answers_carry_the_servers_words(monkeypatch):
    from fastapi.testclient import TestClient

    from tow.log import log_event
    from tow.store import save_download_history, save_state
    from tow.web import app

    _in(monkeypatch, "en")
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    log_event("topic_pause", topic="t1", title="Show", paused=True, how="manual")
    row = client.get("/log.json").json()["rows"][0]
    assert set(row["time"]) == {"iso", "short", "full"}
    assert row["time"]["iso"] == row["iso"]  # logged in TOW's zone, written back the same
    save_state({"topics": [{"id": "t1", "title": "Show", "url": "https://rutor.info/torrent/1"}]})
    done = "2026-01-10T12:00:00+00:00"
    item = {"label": "S01E01", "status": "completed", "completed_observed_at": done}
    event = {"kind": "episode_completed", "at": done}
    save_download_history({"schema_version": 1, "topics": {"t1": {"items": {"a": item}, "last_event": event}}})
    answer = client.get("/topics/t1/downloads.json").json()
    assert answer["items"][0]["completed_time"]["full"] == "2026-01-10 13:00:00 UTC+01:00"  # winter in Berlin
    assert answer["last_event"]["time"]["iso"] == "2026-01-10T13:00:00+01:00"
