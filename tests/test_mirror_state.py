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
