"""What a mirror's answers leave in ``state.json`` (``mirrors.<site>``) when several requests
overlap: counts and deadlines change from the saved values, and an older answer never undoes
a newer one."""

from __future__ import annotations

import httpx
import pytest
from test_protection import SPEC, install

from tow.mirrors import MirrorFetchError, pick_and_get
from tow.store import load_state

HOST = SPEC["fetch_hosts"][0]


def _clock(monkeypatch, start=1000.0):
    """A clock that moves one second at every reading: each request starts later than the last."""
    now = [start]

    def tick():
        now[0] += 1
        return now[0]

    monkeypatch.setattr("tow.mirrors._now", tick)
    return now


def test_two_overlapping_failures_both_count(monkeypatch):
    # Each request read fail=0 before it went out; writing back its own snapshot kept one of them.
    _clock(monkeypatch)
    nested = []

    def handler(request):
        if not nested:
            nested.append(1)
            with pytest.raises(MirrorFetchError):
                pick_and_get("demo", SPEC["fetch_hosts"], "/topic/2", fail_threshold=9)
        return httpx.Response(500, text="down")

    install(monkeypatch, handler)
    with pytest.raises(MirrorFetchError):
        pick_and_get("demo", SPEC["fetch_hosts"], "/topic/1", fail_threshold=9)
    assert load_state()["mirrors"]["demo"]["fail"][HOST] == 2


def test_an_earlier_success_answering_late_does_not_reset_a_later_failure(monkeypatch):
    _clock(monkeypatch)
    nested = []

    def handler(request):
        if request.url.path == "/topic/1":
            nested.append(1)
            with pytest.raises(MirrorFetchError):  # started later, answered first
                pick_and_get("demo", SPEC["fetch_hosts"], "/topic/2", fail_threshold=1, cooldown_sec=600)
            return httpx.Response(200, text="page")
        return httpx.Response(500, text="down")

    install(monkeypatch, handler)
    pick_and_get("demo", SPEC["fetch_hosts"], "/topic/1", fail_threshold=1, cooldown_sec=600)
    bucket = load_state()["mirrors"]["demo"]
    assert nested == [1]
    assert bucket["fail"][HOST] == 1
    assert bucket["cool"][HOST] > 1000
    assert bucket["observed"][HOST]["failure"] == "http"


def test_a_later_failure_after_an_earlier_success_counts(monkeypatch):
    _clock(monkeypatch)

    def handler(request):
        if request.url.path == "/topic/1":
            pick_and_get("demo", SPEC["fetch_hosts"], "/topic/2")  # started later, succeeded first
            return httpx.Response(500, text="down")
        return httpx.Response(200, text="page")

    install(monkeypatch, handler)
    with pytest.raises(MirrorFetchError):
        pick_and_get("demo", SPEC["fetch_hosts"], "/topic/1", fail_threshold=9)
    bucket = load_state()["mirrors"]["demo"]
    assert bucket["fail"][HOST] == 1
    assert bucket["observed"][HOST]["failure"] == "ok"  # the later request's evidence stands


def test_expired_pauses_are_dropped_when_the_site_is_next_written(monkeypatch):
    from tow.store import save_state

    hosts = ["https://site.example", "https://free.example"]
    install(monkeypatch, lambda r: httpx.Response(200, text="page"), hosts=hosts)
    monkeypatch.setattr("tow.mirrors._now", lambda: 5000)
    state = load_state()
    state["mirrors"] = {
        "demo": {
            "cool": {"https://free.example": 4000.0},
            "rate_cool": {"https://free.example": 4500.0},
            "fail": {"https://free.example": 3},
        }
    }
    save_state(state)
    pick_and_get("demo", hosts, "/topic/1")
    bucket = load_state()["mirrors"]["demo"]
    assert bucket["rate_cool"] == {}
    assert bucket["cool"] == {}
    assert bucket["fail"] == {"https://free.example": 3, "https://site.example": 0}


def test_a_late_answer_cannot_recreate_a_deleted_site(monkeypatch):
    install(monkeypatch, lambda r: httpx.Response(500, text="down"))
    monkeypatch.setattr("tow.config.load_config", lambda: {"trackers": {}})
    with pytest.raises(MirrorFetchError):
        pick_and_get("demo", SPEC["fetch_hosts"], "/topic/1")
    assert "demo" not in load_state().get("mirrors", {})


DAY = 86400


def _saved(**bucket):
    from tow.store import save_state

    state = load_state()
    state["mirrors"] = {"demo": bucket}
    save_state(state)


def test_a_deadline_a_clock_jump_left_far_ahead_is_clamped_once(monkeypatch):
    # The clock went back ten days after a pause was written: it held the mirror for ten days.
    now = [1000.0]
    monkeypatch.setattr("tow.mirrors._now", lambda: now[0])
    calls, _ = install(monkeypatch, lambda r: httpx.Response(200, text="page"))
    _saved(cool={HOST: 1000.0 + 10 * DAY}, rate_cool={HOST: 1000.0 + 10 * DAY}, fail={HOST: 3})
    with pytest.raises(MirrorFetchError):
        pick_and_get("demo", SPEC["fetch_hosts"], "/topic/1", ignore_cool=True)
    bucket = load_state()["mirrors"]["demo"]
    assert bucket["rate_cool"][HOST] == 1000.0 + DAY
    assert bucket["cool"][HOST] == 1000.0 + DAY
    now[0] = 5000.0  # read again later: the corrected deadline stays where it was put
    with pytest.raises(MirrorFetchError):
        pick_and_get("demo", SPEC["fetch_hosts"], "/topic/1", ignore_cool=True)
    assert load_state()["mirrors"]["demo"]["rate_cool"][HOST] == 1000.0 + DAY
    now[0] = 1001.0 + DAY
    pick_and_get("demo", SPEC["fetch_hosts"], "/topic/1")
    assert len(calls) == 1


def test_a_cooldown_longer_than_the_sites_own_is_clamped_and_a_real_day_long_limit_is_kept(monkeypatch):
    monkeypatch.setattr("tow.mirrors._now", lambda: 1000.0)
    install(monkeypatch, lambda r: httpx.Response(200, text="page"), hosts=[HOST, "https://free.example"])
    _saved(
        cool={HOST: 1000.0 + 5 * DAY, "https://free.example": 1000.0 + DAY},
        rate_cool={"https://free.example": 1000.0 + DAY},
    )
    with pytest.raises(MirrorFetchError):
        pick_and_get("demo", [HOST, "https://free.example"], "/topic/1", cooldown_sec=600)
    bucket = load_state()["mirrors"]["demo"]
    assert bucket["cool"][HOST] == 1600.0  # the configured cooldown, e.g. after it was shortened
    assert bucket["cool"]["https://free.example"] == 1000.0 + DAY  # a server's 24-hour Retry-After
    assert bucket["rate_cool"]["https://free.example"] == 1000.0 + DAY


def test_a_preview_clamps_in_its_own_view_and_writes_nothing(monkeypatch):
    from tow.paths import state_path

    monkeypatch.setattr("tow.mirrors._now", lambda: 1000.0)
    calls, _ = install(monkeypatch, lambda r: httpx.Response(200, text="page"))
    _saved(cool={HOST: 1000.0 + 10 * DAY}, fail={HOST: 3})
    before = state_path().read_bytes()
    with pytest.raises(MirrorFetchError):
        pick_and_get("demo", SPEC["fetch_hosts"], "/topic/1", persist=False)
    assert state_path().read_bytes() == before
    assert calls == []


@pytest.mark.parametrize("login", [False, True])
def test_a_dated_rate_limit_is_counted_from_the_servers_clock(monkeypatch, login):
    # This computer's clock is two hours behind the server's: "retry at server time + 10 min"
    # paused the mirror for two hours and ten minutes.
    from test_http_limits import _http_date

    from tow.mirrors import note_rate_limit

    monkeypatch.setattr("tow.mirrors._now", lambda: 1_000_000.0 - 7200)
    headers = {"retry-after": _http_date(1_000_600), "date": _http_date(1_000_000)}
    install(monkeypatch, lambda r: httpx.Response(429, headers=headers))
    if login:
        assert note_rate_limit("demo", HOST, httpx.Response(429, headers=headers), persist=True)
    else:
        with pytest.raises(MirrorFetchError):
            pick_and_get("demo", SPEC["fetch_hosts"], "/topic/1")
    assert load_state()["mirrors"]["demo"]["rate_cool"][HOST] == 1_000_000.0 - 7200 + 600
