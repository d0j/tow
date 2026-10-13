"""A rate-limited mirror is paused immediately, without blocking healthy mirrors."""

from datetime import UTC, datetime
from email.utils import format_datetime

import httpx
import pytest
from test_protection import SPEC, install

from tow import http as thttp
from tow.mirrors import MirrorFetchError, has_available_host, pick_and_get
from tow.paths import state_path
from tow.protection import chain
from tow.store import load_state


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, 0),
        ("", 0),
        ("nan", 0),
        ("inf", 0),
        ("-5", 0),
        ("1.5", 0),
        (" 60 ", 60),
        ("9999999", 86400),
        ("9" * 129, 0),
    ],
)
def test_retry_after_is_bounded_and_accepts_only_protocol_values(value, expected):
    assert thttp.retry_after_seconds(value, now=1000) == expected


def test_retry_after_date_uses_injected_clock_and_expired_dates_are_zero():
    value = format_datetime(datetime.fromtimestamp(1600, UTC), usegmt=True)
    assert thttp.retry_after_seconds(value, now=1000) == 600
    assert thttp.retry_after_seconds(value, now=2000) == 0
    assert thttp.retry_after_seconds(value, now=float("nan")) == 0


@pytest.mark.parametrize("ignore_cool", [False, True])
def test_first_rate_limit_immediately_pauses_the_host_and_manual_check_cannot_override(monkeypatch, ignore_cool):
    clock = [1000]
    monkeypatch.setattr("tow.mirrors._now", lambda: clock[0])
    calls, _ = install(monkeypatch, lambda r: httpx.Response(429, headers={"retry-after": "600"}))
    monkeypatch.setattr(chain, "candidates", lambda *a, **kw: pytest.fail("not a challenge"))
    with pytest.raises(MirrorFetchError) as first:
        pick_and_get("demo", SPEC["fetch_hosts"], "/topic/1")
    assert first.value.failure == "rate_limited"
    bucket = load_state()["mirrors"]["demo"]
    assert bucket["cool"][SPEC["fetch_hosts"][0]] == 1600
    assert not has_available_host("demo", SPEC["fetch_hosts"], ignore_cool=ignore_cool)
    with pytest.raises(MirrorFetchError) as second:
        pick_and_get("demo", SPEC["fetch_hosts"], "/topic/2", ignore_cool=ignore_cool)
    assert second.value.code == "mirrors.all_paused"
    assert len(calls) == 1
    clock[0] = 1600
    assert has_available_host("demo", SPEC["fetch_hosts"], ignore_cool=ignore_cool)


def test_rate_limited_mirror_does_not_prevent_a_healthy_fallback(monkeypatch):
    calls, _ = install(
        monkeypatch,
        lambda r: (
            httpx.Response(429, headers={"retry-after": "600"})
            if r.url.host == "site.example"
            else httpx.Response(200, text="real page")
        ),
        hosts=["https://site.example", "https://free.example"],
    )
    response, host = pick_and_get("demo", ["https://site.example", "https://free.example"], "/topic/1")
    assert response.text == "real page"
    assert host == "https://free.example"
    assert len(calls) == 2
    bucket = load_state()["mirrors"]["demo"]
    assert bucket["cool"].get("https://site.example")
    assert not bucket["cool"].get("https://free.example")


@pytest.mark.parametrize("hint", [None, "bad", "0", "nan", "-1", "1.5"])
def test_no_valid_hint_uses_a_bounded_pause_without_sleep(monkeypatch, hint):
    monkeypatch.setattr("tow.mirrors._now", lambda: 1000)
    install(monkeypatch, lambda r: httpx.Response(429, headers={"retry-after": hint} if hint else {}))
    with pytest.raises(MirrorFetchError):
        pick_and_get("demo", SPEC["fetch_hosts"], "/topic/1")
    assert load_state()["mirrors"]["demo"]["cool"][SPEC["fetch_hosts"][0]] == 1060


def test_rate_limited_preview_never_persists_a_pause(monkeypatch):
    install(monkeypatch, lambda r: httpx.Response(429, headers={"retry-after": "600"}))
    before = state_path().read_bytes() if state_path().exists() else None
    with pytest.raises(MirrorFetchError):
        pick_and_get("demo", SPEC["fetch_hosts"], "/topic/1", persist=False)
    assert (state_path().read_bytes() if state_path().exists() else None) == before


@pytest.mark.parametrize("stage", ["probe", "resource"])
def test_rate_limit_during_recovery_also_pauses_the_origin(monkeypatch, stage):
    from test_protection import CF, clearance, settings

    monkeypatch.setattr("tow.mirrors._now", lambda: 1000)

    def handle(request):
        if "cf_clearance=" not in request.headers.get("cookie", ""):
            return httpx.Response(403, text=CF)
        if stage == "probe" or request.url.path != "/":
            return httpx.Response(429, headers={"retry-after": "600"})
        return httpx.Response(200, text="public page")

    calls, _ = install(monkeypatch, handle)
    monkeypatch.setattr(chain, "candidates", lambda *a, **kw: iter([clearance()]))
    with pytest.raises(MirrorFetchError):
        pick_and_get("demo", SPEC["fetch_hosts"], "/topic/1", protection=settings())
    assert not has_available_host("demo", SPEC["fetch_hosts"], ignore_cool=True)
    assert load_state()["mirrors"]["demo"]["cool"][SPEC["fetch_hosts"][0]] == 1600
    count = len(calls)
    with pytest.raises(MirrorFetchError):
        pick_and_get("demo", SPEC["fetch_hosts"], "/topic/2", ignore_cool=True)
    assert len(calls) == count


@pytest.mark.parametrize("persist", [True, False])
def test_direct_login_limit_uses_the_same_pause_and_preview_cannot_write(monkeypatch, persist):
    from tow.mirrors import note_rate_limit

    monkeypatch.setattr("tow.mirrors._now", lambda: 1000)
    install(monkeypatch, lambda request: httpx.Response(200, text="unused"))
    before = state_path().read_bytes() if state_path().exists() else None
    response = httpx.Response(429, headers={"retry-after": "600"})
    assert note_rate_limit("demo", SPEC["fetch_hosts"][0], response, persist=persist)
    if persist:
        assert not has_available_host("demo", SPEC["fetch_hosts"], ignore_cool=True)
    else:
        assert (state_path().read_bytes() if state_path().exists() else None) == before


def test_healthy_observation_cannot_erase_a_hard_rate_deadline(monkeypatch):
    from tow.mirrors import _bucket, _mark_host_ok, note_rate_limit
    from tow.store import save_state

    monkeypatch.setattr("tow.mirrors._now", lambda: 1000)
    install(monkeypatch, lambda request: httpx.Response(200, text="unused"))
    stale = _bucket(load_state(), "demo")
    assert note_rate_limit(
        "demo", SPEC["fetch_hosts"][0], httpx.Response(429, headers={"retry-after": "600"}), persist=True
    )
    _mark_host_ok("demo", stale, SPEC["fetch_hosts"][0], persist=True)
    disk = load_state()
    disk["mirrors"]["demo"]["observed"][SPEC["fetch_hosts"][0]] = {"failure": "ok", "at": 1001, "status": 200}
    save_state(disk)
    assert not has_available_host("demo", SPEC["fetch_hosts"], ignore_cool=True)
    assert load_state()["mirrors"]["demo"]["rate_cool"][SPEC["fetch_hosts"][0]] == 1600


def test_dedicated_login_origin_gets_its_own_rate_pause(monkeypatch):
    from tow.config import load_config, save_config
    from tow.mirrors import note_rate_limit

    monkeypatch.setattr("tow.mirrors._now", lambda: 1000)
    install(monkeypatch, lambda request: httpx.Response(200, text="unused"))
    cfg = load_config()
    cfg["trackers"]["demo"]["login_hosts"] = ["https://auth.example"]
    save_config(cfg)
    assert note_rate_limit(
        "demo", "https://auth.example", httpx.Response(429, headers={"retry-after": "600"}), persist=True
    )
    assert not has_available_host("demo", ["https://auth.example"], ignore_cool=True)
    assert has_available_host("demo", SPEC["fetch_hosts"], ignore_cool=True)
