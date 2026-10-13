"""API outages are shared across sites, never confused with a site's own challenge."""

from __future__ import annotations

import json
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest
from test_protection_providers import ORIGIN, clearance, settings, solution

from tow.paths import data_dir
from tow.protection import chain, providers, store
from tow.protection.spec import AttemptProof, ProviderUnavailableError, validate_settings
from tow.store import master_fernet


@pytest.fixture
def dns(monkeypatch):
    monkeypatch.setattr(chain, "public_addresses", lambda *a: ["93.184.216.34"])


def api(monkeypatch, handler):
    real = httpx.Client

    def client(**kwargs):
        kwargs["transport"].close()
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(**kwargs)

    monkeypatch.setattr(providers.httpx, "Client", client)


@pytest.mark.parametrize(
    ("kind", "command", "media"),
    [
        ("flaresolverr", "request.get", "disableMedia"),
        ("byparr", "request.get", "blockMedia"),
        ("flare_bypasser", "request.get_cookies", None),
        ("trawl", "request.get", None),
    ],
)
def test_independent_adapter_protocols_do_not_send_credentials(monkeypatch, kind, command, media):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=solution())

    api(monkeypatch, handle)
    provider = {"id": "first", "kind": kind, "url": "http://127.0.0.1:8191/v1", "enabled": True}
    value = providers.adapters()[kind](provider, ORIGIN, 15)
    payload = json.loads(calls[0].content)
    assert payload["cmd"] == command
    assert payload["url"] == ORIGIN + "/"
    assert payload["returnOnlyCookies"] is True
    assert payload["maxTimeout"] == 14000
    assert set(payload) == {"cmd", "url", "returnOnlyCookies", "maxTimeout"} | ({media} if media else set())
    assert "cookie" not in calls[0].headers
    assert "authorization" not in calls[0].headers
    assert value.cookies == {"cf_clearance": "synthetic-token"}


def test_flare_bypasser_actual_numeric_status_not_stale_readme_status(monkeypatch):
    api(monkeypatch, lambda request: httpx.Response(200, json=solution(status="ok")))
    with pytest.raises(ValueError, match="not successful"):
        providers.adapters()["flare_bypasser"](settings()["providers"][0], ORIGIN, 15)


def test_nodriver_compatible_response_stays_in_existing_generic_protocol(monkeypatch):
    result = solution()
    result["solution"].pop("response", None)  # fork's returnOnlyCookies envelope
    result["version"] = "synthetic-nodriver-fork"
    api(monkeypatch, lambda request: httpx.Response(200, json=result))
    assert providers.adapters()["flaresolverr"](settings()["providers"][0], ORIGIN, 15).cookies == {
        "cf_clearance": "synthetic-token"
    }


@pytest.mark.parametrize("status", [429, 503])
def test_busy_api_is_distinct_from_site_quota(monkeypatch, status):
    api(monkeypatch, lambda request: httpx.Response(status, headers={"retry-after": "3600"}))
    with pytest.raises(ProviderUnavailableError) as caught:
        providers.cookie_api(settings()["providers"][0], ORIGIN, 15)
    assert caught.value.reason == "busy"
    assert caught.value.retry_after == 3600


@pytest.mark.parametrize("status", [400, 401, 403, 404, 405, 422, 501])
def test_protocol_refusal_is_global_only_to_this_api(monkeypatch, status):
    api(monkeypatch, lambda request: httpx.Response(status))
    with pytest.raises(ProviderUnavailableError) as caught:
        providers.cookie_api(settings()["providers"][0], ORIGIN, 15)
    assert caught.value.reason == "protocol"


def test_api_500_may_be_one_site_and_does_not_trip_global_fuse(monkeypatch):
    api(monkeypatch, lambda request: httpx.Response(500))
    with pytest.raises(ValueError, match="clearance service HTTP failure") as caught:
        providers.cookie_api(settings()["providers"][0], ORIGIN, 15)
    assert not isinstance(caught.value, ProviderUnavailableError)


@pytest.mark.parametrize(
    ("value", "expected"), [(None, 0), ("bad", 0), ("nan", 0), ("inf", 0), ("-5", 0), ("60", 60), ("999999", 86400)]
)
def test_retry_after_is_finite_and_bounded(value, expected):
    assert providers._retry_after(value) == expected


def test_retry_after_http_date():
    future = format_datetime(datetime.now(UTC) + timedelta(seconds=120), usegmt=True)
    assert 118 <= providers._retry_after(future) <= 120


def test_api_down_is_skipped_across_other_origins_before_attempt_limit(dns, monkeypatch):
    calls = []

    def unavailable(provider, origin, timeout):
        calls.append(provider["id"])
        raise httpx.ConnectError("synthetic")

    def good(provider, origin, timeout):
        calls.append(provider["id"])
        return clearance("second")

    monkeypatch.setattr(chain, "adapters", lambda: {"flaresolverr": unavailable, "byparr": good})
    assert list(chain.candidates("first-site", ORIGIN, settings(max_attempts=1))) == []
    assert [c.provider for c in chain.candidates("other-site", "https://other.example", settings(max_attempts=1))] == [
        "second"
    ]
    assert calls == ["first", "second"]


@pytest.mark.parametrize("failure", [ValueError("site challenge"), httpx.ReadTimeout("site slow")])
def test_site_refusal_does_not_disable_this_provider_for_other_origins(dns, monkeypatch, failure):
    calls = []

    def scoped(provider, origin, timeout):
        calls.append(origin)
        if origin == ORIGIN:
            raise failure
        return clearance()

    monkeypatch.setattr(chain, "adapters", lambda: {"flaresolverr": scoped})
    assert list(chain.candidates("site", ORIGIN, settings(max_attempts=1))) == []
    assert len(list(chain.candidates("other-site", "https://other.example", settings(max_attempts=1)))) == 1
    assert calls == [ORIGIN, "https://other.example"]


def test_api_observation_does_not_cache_unverified_clearance(dns, monkeypatch):
    monkeypatch.setattr(chain, "adapters", lambda: {"flaresolverr": lambda *a: clearance()})
    assert len(list(chain.candidates("site", ORIGIN, settings(max_attempts=1)))) == 1
    assert store.service_states(settings()["providers"], now=time.time())["first"]["status"] == "succeeded"
    assert store.read("site", ORIGIN, settings(max_attempts=1), now=time.time()) is None


def test_api_backoff_persists_and_is_not_reset_by_id_or_order():
    configured = settings()
    first = configured["providers"][0]
    store.note_service(first, "unavailable", now=1000)
    renamed = dict(first, id="renamed", enabled=False)
    state = store.service_states([renamed], now=1001)["renamed"]
    assert state["ready"] is False
    assert state["retry_at"] == 1030
    assert store.service_states([first], now=1030)["first"]["ready"] is True
    for _ in range(20):
        store.note_service(first, "unavailable", now=1001)
    assert store.service_states([first], now=1001)["first"]["retry_at"] == 2801
    store.note_service(first, "succeeded", now=1002)
    assert store.service_states([first], now=1002)["first"]["ready"] is True


def test_retry_after_is_respected_without_resetting_another_service():
    first, second = settings()["providers"]
    store.note_service(first, "busy", now=1000, retry_after=3600)
    states = store.service_states([first, second], now=1010)
    assert states["first"]["retry_at"] == 4600
    assert states["second"]["ready"] is True


def test_stale_api_failures_do_not_make_next_days_first_error_a_long_pause():
    first = settings()["providers"][0]
    for _ in range(8):
        store.note_service(first, "unavailable", now=1000)
    store.note_service(first, "unavailable", now=87400)
    assert store.service_states([first], now=87400)["first"]["retry_at"] == 87430


def test_optional_fuse_write_failure_cannot_interrupt_next_provider(dns, monkeypatch):
    def fail(*args):
        raise ProviderUnavailableError("unavailable")

    monkeypatch.setattr(chain, "adapters", lambda: {"flaresolverr": fail, "byparr": lambda *a: clearance("second")})
    monkeypatch.setattr(store, "note_service", lambda *a, **kw: (_ for _ in ()).throw(OSError("synthetic")))
    assert [c.provider for c in chain.candidates("site", ORIGIN, settings())] == ["second"]


def test_old_encrypted_format_is_read_without_rewriting_and_keeps_clearance():
    configured = settings()
    store.reserve("site", ORIGIN, configured, now=time.time())
    store.accept("site", ORIGIN, configured, clearance(), now=time.time())
    path = data_dir() / "protection.enc"
    raw = json.loads(master_fernet().decrypt(path.read_bytes()))
    raw["version"] = 1
    for record in raw["records"].values():
        record.pop("attempt_id", None)
        record.pop("provider_cursor", None)
    path.write_bytes(master_fernet().encrypt(json.dumps(raw).encode()))
    before = path.read_bytes()
    assert store.read("site", ORIGIN, configured, now=time.time()).cookies == {"cf_clearance": "synthetic-token"}
    assert store.service_states(configured["providers"], now=time.time())["first"]["status"] == "unknown"
    assert path.read_bytes() == before
    chain.accept("site", ORIGIN, configured, store.read("site", ORIGIN, configured, now=time.time()))
    assert path.read_bytes() == before


def test_reset_preserves_and_resets_both_clearance_and_service_backoff():
    first = settings()["providers"][0]
    store.note_service(first, "busy", now=time.time(), retry_after=3600)
    assert store.reset() is True
    assert store.service_states([first], now=time.time())["first"]["ready"] is True


@pytest.mark.parametrize("length", [4096, 4097])
def test_documented_clearance_size_bound(length):
    value = clearance(cookies={"cf_clearance": "a" * length})
    if length == 4096:
        assert providers.validate_clearance(value, ORIGIN, now=time.time()) == value
    else:
        with pytest.raises(ValueError, match="invalid clearance field"):
            providers.validate_clearance(value, ORIGIN, now=time.time())


def test_default_port_alias_cannot_create_two_connections_to_one_api():
    with pytest.raises(ValueError, match="duplicate"):
        validate_settings(
            {
                "providers": [
                    {"id": "one", "kind": "trawl", "url": "http://127.0.0.1/v1"},
                    {"id": "two", "kind": "byparr", "url": "http://127.0.0.1:80/v1"},
                ]
            }
        )


def test_bounded_failed_chain_continues_to_later_providers_after_cooldown(dns, monkeypatch):
    configured = settings(max_attempts=1, min_interval_sec=60)
    configured["providers"].append({"id": "third", "kind": "trawl", "url": "http://127.0.0.1:8193/v1", "enabled": True})
    clock = [1000.0]
    calls = []
    monkeypatch.setattr(chain.time, "time", lambda: clock[0])

    def refused(provider, origin, timeout):
        calls.append(provider["id"])
        raise ValueError("site challenge, not an API outage")

    monkeypatch.setattr(chain, "adapters", lambda: dict.fromkeys(["flaresolverr", "byparr", "trawl"], refused))
    for _ in range(4):
        assert list(chain.candidates("site", ORIGIN, configured)) == []
        clock[0] += 61
    assert calls == ["first", "second", "third", "first"]


def test_cursor_is_durable_before_request_and_failed_write_prevents_request(dns, monkeypatch):
    configured = settings(max_attempts=1)

    def verify_cursor(provider, origin, timeout):
        record = store._load()[store._key("site", origin)]
        assert record["provider_cursor"] == provider["id"]
        assert len(record["attempt_id"]) == 32
        return clearance()

    monkeypatch.setattr(chain, "adapters", lambda: {"flaresolverr": verify_cursor})
    assert len(list(chain.candidates("site", ORIGIN, configured))) == 1
    real_save = store._save

    def fail_cursor(records):
        if any("provider_cursor" in value for value in records.values()):
            raise OSError("synthetic cursor failure")
        return real_save(records)

    assert store.reset() is True
    monkeypatch.setattr(store, "_save", fail_cursor)
    monkeypatch.setattr(chain, "adapters", lambda: {"flaresolverr": lambda *a: pytest.fail("reservation not durable")})
    assert list(chain.candidates("site", ORIGIN, configured)) == []


def test_fair_rotation_resets_on_config_edit_but_keeps_service_fuse():
    configured = settings()
    nonce = store.reserve_attempt("site", ORIGIN, configured, now=1000)
    assert store.note_attempt(AttemptProof("site", ORIGIN, nonce), "first") is True
    candidates = configured["providers"]
    assert [p["id"] for p in store.eligible_providers("site", ORIGIN, configured, candidates, now=3000)] == [
        "second",
        "first",
    ]
    changed = settings(timeout_sec=31)
    assert store.eligible_providers("site", ORIGIN, changed, candidates, now=3000) == candidates
    store.note_service(candidates[0], "busy", now=3000, retry_after=60)
    assert store.eligible_providers("site", ORIGIN, changed, candidates, now=3001) == candidates[1:]


def test_verified_cached_access_preserves_configured_priority():
    configured = settings()
    nonce = store.reserve_attempt("site", ORIGIN, configured, now=time.time())
    store.note_attempt(AttemptProof("site", ORIGIN, nonce), "first")
    store.accept("site", ORIGIN, configured, clearance(), now=time.time())
    assert (
        store.eligible_providers("site", ORIGIN, configured, configured["providers"], now=time.time())
        == configured["providers"]
    )


@pytest.mark.parametrize("same_cookie", [True, False])
def test_old_callback_cannot_repopulate_or_discard_new_reservation(dns, monkeypatch, same_cookie):
    configured = settings(max_attempts=1)
    monkeypatch.setattr(chain, "adapters", lambda: {"flaresolverr": lambda *a: clearance()})
    old = next(chain.candidates("site", ORIGIN, configured))
    assert store.reset() is True
    new = next(chain.candidates("site", ORIGIN, configured))
    assert old.attempt_id != new.attempt_id
    chain.accept("site", ORIGIN, configured, old)
    assert chain.cached("site", ORIGIN, configured) is None
    if not same_cookie:
        new = replace(new, cookies={"cf_clearance": "synthetic-new-token"})
    chain.accept("site", ORIGIN, configured, new)
    chain.accept("site", ORIGIN, configured, old)
    assert chain.cached("site", ORIGIN, configured) == new
    chain.discard("site", ORIGIN, expected=old)
    assert chain.cached("site", ORIGIN, configured) == new
    chain.discard("site", ORIGIN, expected=chain.cached("site", ORIGIN, configured))
    assert chain.cached("site", ORIGIN, configured) is None


def test_late_api_outcome_and_cursor_cannot_restore_reset_service_fuse():
    configured = settings()
    provider = configured["providers"][0]
    old = AttemptProof("site", ORIGIN, store.reserve_attempt("site", ORIGIN, configured, now=1000))
    store.note_service(provider, "busy", now=1001, attempt=old)
    assert store.reset() is True
    new = AttemptProof("site", ORIGIN, store.reserve_attempt("site", ORIGIN, configured, now=1002))
    assert store.note_attempt(old, provider["id"]) is False
    store.note_service(provider, "busy", now=1003, attempt=old)
    assert store.service_states([provider], now=1003)["first"]["status"] == "unknown"
    store.note_service(provider, "succeeded", now=1004, attempt=new)
    store.note_service(provider, "busy", now=1005, attempt=old)
    assert store.service_states([provider], now=1005)["first"]["status"] == "succeeded"


def test_attempt_identity_is_private_and_cannot_be_created_by_an_api(monkeypatch):
    api(monkeypatch, lambda request: httpx.Response(200, json=solution(attempt_id="0" * 32)))
    assert providers.cookie_api(settings()["providers"][0], ORIGIN, 15).attempt_id is None
    proof = AttemptProof("site", ORIGIN, "1" * 32)
    assert "1" * 32 not in repr(proof)
    assert "1" * 32 not in repr(replace(clearance(), attempt_id=proof.nonce))


def test_unused_reservation_restores_previous_clearance_and_cursor():
    configured = settings(min_interval_sec=60)
    old = AttemptProof("site", ORIGIN, store.reserve_attempt("site", ORIGIN, configured, now=1000))
    store.note_attempt(old, "second")
    value = replace(clearance(), expires_at=2000, attempt_id=old.nonce)
    store.accept("site", ORIGIN, configured, value, now=1000, require_attempt=True)
    pending = store.prepare_attempt("site", ORIGIN, configured, now=1100)
    assert pending is not None
    assert store.note_attempt(pending.proof, "first") is True
    store.cancel_unused_attempt(pending)
    assert store.read("site", ORIGIN, configured, now=1101) == value
    store.discard("site", ORIGIN, expected=value)
    assert store.eligible_providers("site", ORIGIN, configured, configured["providers"], now=1101)[0]["id"] == "first"


@pytest.mark.parametrize("reset", [False, True])
def test_unused_reservation_cannot_undo_reset_or_newer_attempt(reset):
    configured = settings(min_interval_sec=60)
    pending = store.prepare_attempt("site", ORIGIN, configured, now=1000)
    assert pending is not None
    if reset:
        assert store.reset() is True
        store.cancel_unused_attempt(pending)
        assert not (data_dir() / "protection.enc").exists()
    newer = store.prepare_attempt("site", ORIGIN, configured, now=1100)
    assert newer is not None
    store.cancel_unused_attempt(pending)
    assert store.note_attempt(newer.proof, "second") is True
    assert store.reserve("site", ORIGIN, configured, now=1101) is False


@pytest.mark.parametrize("later", [False, True])
def test_slow_later_preparation_does_not_advance_cursor_without_call(dns, monkeypatch, later):
    configured = settings(max_attempts=3)
    if later:
        configured["providers"].append(
            {"id": "third", "kind": "trawl", "url": "http://127.0.0.1:8193/v1", "enabled": True}
        )
    clock = [100.0]
    calls = []
    monkeypatch.setattr(chain.time, "monotonic", lambda: clock[0])
    original = store.note_attempt

    def slow(attempt, provider_id):
        result = original(attempt, provider_id)
        if provider_id == "second":
            clock[0] += 10 if later else 20
        return result

    def refused(provider, origin, timeout):
        calls.append(provider["id"])
        raise ValueError("synthetic refusal")

    monkeypatch.setattr(store, "note_attempt", slow)
    monkeypatch.setattr(chain, "adapters", lambda: dict.fromkeys(["flaresolverr", "byparr", "trawl"], refused))
    assert list(chain.candidates("site", ORIGIN, configured)) == []
    assert calls == (["first", "third"] if later else ["first"])
    ordered = store.eligible_providers("site", ORIGIN, configured, configured["providers"], now=time.time() + 1900)
    assert ordered[0]["id"] == ("first" if later else "second")


def test_paused_cursor_cannot_starve_last_provider(dns, monkeypatch):
    configured = settings(max_attempts=1, min_interval_sec=60)
    configured["providers"].append({"id": "third", "kind": "trawl", "url": "http://127.0.0.1:8193/v1", "enabled": True})
    clock = [1000.0]
    calls = []
    monkeypatch.setattr(chain.time, "time", lambda: clock[0])

    def refused(provider, origin, timeout):
        calls.append(provider["id"])
        if provider["id"] == "second":
            raise ProviderUnavailableError("busy", retry_after=90)
        raise ValueError("site refused")

    monkeypatch.setattr(chain, "adapters", lambda: dict.fromkeys(["flaresolverr", "byparr", "trawl"], refused))
    for _ in range(6):
        assert list(chain.candidates("site", ORIGIN, configured)) == []
        clock[0] += 61
    assert calls == ["first", "second", "third", "first", "second", "third"]


def test_missing_cursor_module_keeps_the_later_position():
    configured = settings()
    configured["providers"].append({"id": "third", "kind": "trawl", "url": "http://127.0.0.1:8193/v1", "enabled": True})
    proof = AttemptProof("site", ORIGIN, store.reserve_attempt("site", ORIGIN, configured, now=1000))
    store.note_attempt(proof, "second")
    available = [configured["providers"][0], configured["providers"][2]]
    assert [p["id"] for p in store.eligible_providers("site", ORIGIN, configured, available, now=3000)] == [
        "third",
        "first",
    ]


@pytest.mark.parametrize("budget", [3, 5, 30, 90])
def test_solver_budget_keeps_time_for_independent_verification(dns, monkeypatch, budget):
    clock = [100.0]
    times = []
    monkeypatch.setattr(chain.time, "monotonic", lambda: clock[0])

    def solve(provider, origin, timeout):
        times.append(timeout)
        clock[0] += timeout - 0.01
        return clearance()

    monkeypatch.setattr(chain, "adapters", lambda: {"flaresolverr": solve})
    assert len(list(chain.candidates("site", ORIGIN, settings(timeout_sec=90), budget_sec=budget))) == 1
    assert times == [pytest.approx(budget - min(10, budget / 3))]
    assert 100 + budget - clock[0] >= min(10, budget / 3)


def test_late_adapter_cannot_use_reserved_verification_time(dns, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(chain.time, "monotonic", lambda: clock[0])

    def late(*args):
        clock[0] = 125.0  # before the total 30 s deadline, but after this adapter's 20 s share
        return clearance()

    monkeypatch.setattr(chain, "adapters", lambda: {"flaresolverr": late})
    assert list(chain.candidates("site", ORIGIN, settings(max_attempts=1))) == []
    assert store.service_states(settings()["providers"], now=time.time())["first"]["status"] == "unknown"


@pytest.mark.parametrize("delay", [5, 29])
def test_slow_attempt_logging_never_extends_solver_deadline(dns, monkeypatch, delay):
    clock = [100.0]
    times = []
    events = []
    monkeypatch.setattr(chain.time, "monotonic", lambda: clock[0])

    def event(kind, *args):
        events.append(kind)
        if kind == "protection_attempt":
            clock[0] += delay

    def solve(provider, origin, timeout):
        times.append(timeout)
        clock[0] += timeout - 0.01
        return clearance()

    monkeypatch.setattr(chain, "record_event", event)
    monkeypatch.setattr(chain, "adapters", lambda: {"flaresolverr": solve})
    result = list(chain.candidates("site", ORIGIN, settings(max_attempts=1)))
    if delay == 5:
        assert times == [pytest.approx(15)]
        assert len(result) == 1
        assert clock[0] < 120
    else:
        assert result == []
        assert times == []
        assert events == ["protection_attempt", "protection_failed"]


@pytest.mark.parametrize("failure", ["late", "wrong_provider"])
def test_late_and_wrong_provider_results_finish_the_attempt_event(dns, monkeypatch, failure):
    clock = [100.0]
    events = []
    monkeypatch.setattr(chain.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(chain, "record_event", lambda kind, *a: events.append(kind))

    def solve(*args):
        if failure == "late":
            clock[0] = 131.0
        return clearance("second")

    monkeypatch.setattr(chain, "adapters", lambda: {"flaresolverr": solve})
    assert list(chain.candidates("site", ORIGIN, settings(max_attempts=1))) == []
    assert events == ["protection_attempt", "protection_failed"]
