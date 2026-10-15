"""What the Sites page and the site colour say about a site's mirrors (``mirrors.<site>``): a
mirror's health comes from transport answers only, sign-in and the day's limit stay separate
amber evidence, and a topic's own failure never paints the mirror."""

from __future__ import annotations

import re
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from test_protection import LOGIN, SPEC, TORRENT, install

from tow.config import load_config, save_config
from tow.i18n import t
from tow.mirrors import MirrorFetchError, pick_and_get
from tow.store import load_state, save_state
from tow.web import app

HOST = SPEC["fetch_hosts"][0]


@pytest.fixture
def client():
    cfg = load_config()
    cfg["language"] = "en"
    save_config(cfg)
    return TestClient(app, headers={"Origin": "http://127.0.0.1"})


def _mirror(page: str, host: str = HOST) -> str:
    """The mirror's own text in the site's row (its dot, name and pills)."""
    name = host.replace("https://", "")
    found = re.search(r'<span class="mirror"[^>]*>(?:(?!<span class="mirror").)*?' + re.escape(name), page, re.DOTALL)
    assert found, name
    end = page.find("</span>", page.find(name, found.start()))
    return page[found.start() : page.find("</span>", end + 1) + 7]


def _tone(page: str, site: str = "demo") -> str:
    found = re.search(r'<span class="trk (\w+)">' + re.escape(site) + "</span>", page)
    assert found
    return found.group(1)


def _saved(bucket, probes=()):
    state = load_state()
    state["mirrors"] = {"demo": bucket}
    state["doctor"] = {"probes": list(probes)}
    save_state(state)


def test_a_removed_topic_and_tows_own_failure_leave_the_mirror_healthy(monkeypatch, client):
    pages = {"/topic/1": httpx.Response(200, text="<title>Show</title>"), "/topic/2": httpx.Response(404)}
    install(monkeypatch, lambda r: pages.get(r.url.path, httpx.Response(410)))
    pick_and_get("demo", SPEC["fetch_hosts"], "/topic/1")
    with pytest.raises(MirrorFetchError):
        pick_and_get("demo", SPEC["fetch_hosts"], "/topic/2")
    with pytest.raises(MirrorFetchError):
        pick_and_get("demo", SPEC["fetch_hosts"], "/download/3", ok=lambda r: False)  # "not a torrent"
    assert load_state()["mirrors"]["demo"]["observed"][HOST]["failure"] == "ok"
    page = client.get("/sites").text
    assert _tone(page) == "ok"
    assert 'class="pill warn"' not in _mirror(page)


def test_an_older_saved_topic_failure_does_not_paint_the_mirror(client):
    install_config = load_config()
    install_config["trackers"] = {"demo": SPEC}
    save_config(install_config)
    for failure in ("gone", "http_topic", "local", "too_large", "unexpected"):
        _saved({"observed": {HOST: {"failure": failure, "at": time.time(), "status": 404}}})
        page = client.get("/sites").text
        assert _tone(page) == "mut", failure
        assert 'class="pill warn"' not in _mirror(page), failure


@pytest.mark.parametrize(("failure", "label"), [("auth", "sites.mirror_login"), ("quota", "sites.mirror_quota")])
def test_sign_in_and_daily_limit_stay_amber_beside_a_healthy_mirror(monkeypatch, client, failure, label):
    body = LOGIN if failure == "auth" else "Вы скачали сегодня торрент-файл"
    pages = {"/topic/1": httpx.Response(200, text="<title>Show</title>")}
    install(monkeypatch, lambda r: pages.get(r.url.path, httpx.Response(200, text=body)))
    pick_and_get("demo", SPEC["fetch_hosts"], "/topic/1")
    with pytest.raises(MirrorFetchError):
        pick_and_get("demo", SPEC["fetch_hosts"], "/download/1", ok=lambda r: False, download_limit=True)
    pick_and_get("demo", SPEC["fetch_hosts"], "/topic/1")  # a page: no proof the download works
    page = client.get("/sites").text
    assert _tone(page) == "warn"
    assert t(label, "en") in _mirror(page)
    bucket = load_state()["mirrors"]["demo"]
    assert bucket["observed"][HOST]["failure"] == "ok"
    assert bucket["access"][HOST]["failure"] == failure
    pages["/download/1"] = httpx.Response(200, content=TORRENT)
    pick_and_get("demo", SPEC["fetch_hosts"], "/download/1", ok=lambda r: r.content == TORRENT)
    assert HOST not in load_state()["mirrors"]["demo"].get("access", {})
    page = client.get("/sites").text
    assert _tone(page) == "ok"


def test_routine_successes_refresh_their_time_every_five_minutes(monkeypatch, client):
    from datetime import UTC, datetime

    from tow import mirrors

    now = [1_800_000_000.0]
    monkeypatch.setattr("tow.mirrors._now", lambda: now[0])
    install(monkeypatch, lambda r: httpx.Response(200, text="<title>Show</title>"))
    writes = []
    real_save = mirrors.save_state
    monkeypatch.setattr(mirrors, "save_state", lambda state: writes.append(now[0]) or real_save(state))
    for step in range(0, 700, 60):  # a page a minute
        now[0] = 1_800_000_000.0 + step
        pick_and_get("demo", SPEC["fetch_hosts"], "/topic/1")
    assert writes == [1_800_000_000.0, 1_800_000_300.0, 1_800_000_600.0]
    # A failed front-page probe four minutes before the latest refresh is older evidence.
    probe_at = datetime.fromtimestamp(1_800_000_360.0, UTC).isoformat()
    state = load_state()
    state["doctor"] = {"probes": [{"tracker": "demo", "host": HOST, "ok": False, "error": "timed out", "at": probe_at}]}
    save_state(state)
    page = client.get("/sites").text
    assert _tone(page) == "ok"
    assert 'class="dot ok"' in _mirror(page)


@pytest.mark.parametrize(
    ("failure", "status", "key"),
    [
        ("transport", None, "doctor.reason.no_connection"),
        ("http", 503, None),
        ("redirect_policy", None, "doctor.reason.redirect"),
        ("redirect_loop", None, "mirrors.redirect_limit"),
        ("origin_down", 522, None),
        ("rate_limited", 429, "mirrors.rate_limited"),
    ],
)
def test_every_mirror_failure_is_said_in_words(client, failure, status, key):
    cfg = load_config()
    cfg["trackers"] = {"demo": SPEC}
    save_config(cfg)
    _saved({"observed": {HOST: {"failure": failure, "at": time.time(), "status": status}}})
    mirror = _mirror(client.get("/sites").text)
    reason = re.search(r'title="([^"]*)"', mirror).group(1)
    assert failure not in reason
    assert "{" not in reason
    if key:
        assert reason == t(key, "en")
    else:
        assert str(status) in reason


@pytest.mark.parametrize(
    "error", ["auth", "origin_down", "transport", "http 503", "gone", "local", "too_large", "paused"]
)
def test_a_probes_failure_code_is_said_in_words_without_a_raw_placeholder(error):
    from tow.doctor import reason_text

    text = reason_text(error, "ru")
    assert "{" not in text
    assert text != error
    assert "_" not in text


def test_a_sign_in_page_answered_with_200_does_not_say_error_200(monkeypatch):
    install(monkeypatch, lambda r: httpx.Response(200, text=LOGIN))
    with pytest.raises(MirrorFetchError) as error:
        pick_and_get("demo", SPEC["fetch_hosts"], "/download/1", ok=lambda r: False, persist=False)
    assert "200" not in error.value.text("en")
    assert error.value.params["error"].text("en") == "the site asks to sign in"


def test_the_attention_menu_class_has_no_trailing_space_when_nothing_needs_attention(client):
    page = client.get("/sites").text
    assert '<details class="attention-menu" id="current-problems"' in page
