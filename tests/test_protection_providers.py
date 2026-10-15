"""Optional providers are synthetic, bounded, non-authenticating and independently disabled."""

from __future__ import annotations

import json
import time

import httpcore
import httpx
import pytest

from tow.paths import data_dir
from tow.protection import chain, providers, store
from tow.protection.spec import AccessFailure, Clearance, RecoveryBudget, service_url, target_origin, validate_settings

ORIGIN = "https://tracker.example"


def settings(**changes):
    raw = {
        "enabled": True,
        "providers": [
            {"id": "first", "kind": "flaresolverr", "url": "http://127.0.0.1:8191", "enabled": True},
            {"id": "second", "kind": "byparr", "url": "http://127.0.0.1:8192", "enabled": True},
        ],
    }
    raw.update(changes)
    return validate_settings(raw)


def clearance(provider="first", **changes):
    raw = {
        "cookies": {"cf_clearance": "synthetic-token"},
        "user_agent": "Synthetic/1.0",
        "expires_at": time.time() + 300,
        "provider": provider,
    }
    raw.update(changes)
    return Clearance(**raw)


def solution(**changes):
    raw = {
        "url": ORIGIN + "/landing",
        "status": 200,
        "userAgent": "Synthetic/1.0",
        "cookies": [
            {
                "name": "cf_clearance",
                "value": "synthetic-token",
                "domain": "tracker.example",
                "path": "/",
                "secure": True,
                "expires": time.time() + 300,
            },
            {"name": "site_session", "value": "discard-this", "domain": "tracker.example"},
        ],
    }
    raw.update(changes)
    return {"status": "ok", "solution": raw}


@pytest.fixture
def safe_dns(monkeypatch):
    calls = []
    monkeypatch.setattr(chain, "public_addresses", lambda host, port: calls.append((host, port)) or ["93.184.216.34"])
    return calls


@pytest.mark.parametrize(
    "url",
    [
        "https://public.example/v1",
        "http://8.8.8.8/v1",
        "http://169.254.169.254/v1",
        "http://0.0.0.0/v1",
        "http://127.0.0.1/v1?x=1",
        "http://127.0.0.1/v1#x",
        "http://user:pass@127.0.0.1/v1",
        "http://127.0.0.1/v1/",
        "http://127.0.0.1:0",
        "http://127.0.0.1:65536",
        "http://127.0.0.1\\@public.example",
        "http://192.0.2.1",
        "http://100.64.0.1",
        "ftp://127.0.0.1",
        "http://[fe80::1%25eth0]/v1",
    ],
)
def test_service_refuses_unsafe_address(url):
    with pytest.raises(ValueError, match="invalid clearance service"):
        service_url(url)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://localhost:8191", "http://127.0.0.1:8191/v1"),
        ("http://192.168.1.2/", "http://192.168.1.2/v1"),
        ("http://[::1]:8191/v1", "http://[::1]:8191/v1"),
        ("https://[fd00::1]/v1", "https://[fd00::1]/v1"),
    ],
)
def test_service_canonical_address(url, expected):
    assert service_url(url) == expected


@pytest.mark.parametrize(
    "raw",
    [
        True,
        [],
        {"unknown": True},
        {"enabled": "true"},
        {"timeout_sec": True},
        {"timeout_sec": 4},
        {"min_interval_sec": 59},
        {"max_attempts": 5},
        {"providers": "bad"},
        {"providers": [{"id": "x", "kind": "byparr", "url": "http://127.0.0.1", "password": "x"}]},
    ],
)
def test_configuration_fail_closed(raw):
    with pytest.raises(ValueError, match="invalid"):
        validate_settings(raw)


def test_defaults_are_disabled_and_provider_requires_explicit_enable():
    assert validate_settings(None) == {
        "enabled": False,
        "timeout_sec": 30,
        "min_interval_sec": 1800,
        "max_attempts": 2,
        "providers": [],
    }
    configured = validate_settings({"providers": [{"id": "local", "kind": "byparr", "url": "http://localhost:8191"}]})
    assert configured["providers"][0]["enabled"] is False


def test_duplicate_id_or_endpoint_refused():
    for field in ("id", "url"):
        raw = settings()
        raw["providers"][1][field] = raw["providers"][0][field]
        with pytest.raises(ValueError, match=r"invalid|duplicate"):
            validate_settings(raw)


@pytest.mark.parametrize(
    "origin", [ORIGIN + "/topic", ORIGIN + "?secret=x", "https://user:pass@tracker.example", ORIGIN + "#x"]
)
def test_target_origin_refuses_topics_and_credentials(origin):
    with pytest.raises(ValueError, match="invalid clearance target"):
        target_origin(origin)


def test_plugins_discovered_independently():
    assert set(providers.adapters()) == {"flaresolverr", "byparr", "flare_bypasser", "trawl"}


def test_disabled_chain_no_dns_no_storage(safe_dns, monkeypatch):
    monkeypatch.setattr(store, "read", lambda *a, **kw: pytest.fail("store accessed"))
    assert list(chain.candidates("sample", ORIGIN, settings(enabled=False))) == []
    assert safe_dns == []


def test_failed_first_falls_back_and_reservation_throttles(safe_dns, monkeypatch):
    calls = []

    def fail(provider, origin, timeout):
        calls.append(provider["id"])
        raise httpx.ConnectError("synthetic")

    def succeed(provider, origin, timeout):
        calls.append(provider["id"])
        assert 0 < timeout <= 30
        return clearance("second")

    monkeypatch.setattr(chain, "adapters", lambda: {"flaresolverr": fail, "byparr": succeed})
    assert [c.provider for c in chain.candidates("sample", ORIGIN, settings())] == ["second"]
    assert calls == ["first", "second"]
    assert list(chain.candidates("sample", ORIGIN, settings())) == []
    assert calls == ["first", "second"]


def test_missing_plugin_does_not_block_next(safe_dns, monkeypatch):
    monkeypatch.setattr(chain, "adapters", lambda: {"byparr": lambda *args: clearance("second")})
    assert [c.provider for c in chain.candidates("sample", ORIGIN, settings())] == ["second"]


@pytest.mark.parametrize(("max_attempts", "expected"), [(1, []), (2, ["first"]), (4, ["first"])])
def test_missing_cookie_last_chance_is_within_shared_budget(safe_dns, monkeypatch, max_attempts, expected):
    calls = []

    def solve(provider, origin, timeout):
        calls.append(provider["id"])
        raw = solution(cookies=[] if len(calls) == 1 else solution()["solution"]["cookies"])
        return providers._solution(raw, origin, provider["id"])

    monkeypatch.setattr(chain, "adapters", lambda: {"flaresolverr": solve})
    configured = settings(max_attempts=max_attempts)
    budget = RecoveryBudget(max_attempts)
    assert [c.provider for c in chain.candidates("sample", ORIGIN, configured, call_budget=budget)] == expected
    assert calls == ["first"] * min(2, max_attempts)
    assert budget.remaining == max_attempts - len(calls)
    assert list(chain.candidates("sample", ORIGIN, configured)) == []  # no new lease or calls


def test_missing_cookie_gives_other_connections_priority(safe_dns, monkeypatch):
    calls = []

    def solve(provider, origin, timeout):
        calls.append(provider["id"])
        if provider["id"] == "first" and calls.count("first") == 1:
            raise AccessFailure("missing_cookie", "validation", "synthetic")
        return clearance(provider["id"])

    monkeypatch.setattr(chain, "adapters", lambda: {"flaresolverr": solve, "byparr": solve})
    assert [c.provider for c in chain.candidates("sample", ORIGIN, settings(max_attempts=3))] == ["second", "first"]
    assert calls == ["first", "second", "first"]


def test_repeated_missing_cookie_stops_after_one_last_chance(safe_dns, monkeypatch):
    calls = []

    def solve(provider, origin, timeout):
        calls.append(provider["id"])
        raise AccessFailure("missing_cookie", "validation", "synthetic")

    monkeypatch.setattr(chain, "adapters", lambda: {"flaresolverr": solve})
    assert list(chain.candidates("sample", ORIGIN, settings(max_attempts=4))) == []
    assert calls == ["first", "first"]


@pytest.mark.parametrize(("elapsed", "count"), [(0, 2), (19, 2), (20, 1), (30, 1)])
def test_last_chance_keeps_verification_margin(safe_dns, monkeypatch, elapsed, count):
    now = [0.0]
    calls = []
    monkeypatch.setattr(chain.time, "monotonic", lambda: now[0])

    def solve(provider, origin, timeout):
        calls.append(timeout)
        now[0] += elapsed
        raise AccessFailure("missing_cookie", "validation", "synthetic")

    monkeypatch.setattr(chain, "adapters", lambda: {"flaresolverr": solve})
    assert list(chain.candidates("sample", ORIGIN, settings(timeout_sec=30, max_attempts=4))) == []
    assert len(calls) == count
    assert all(1 <= value <= 20 for value in calls)


@pytest.mark.parametrize("reason", ["refused", "origin", "status", "scope", "invalid_response", "limit", "timeout"])
def test_other_provider_refusals_do_not_repeat_same_connection(safe_dns, monkeypatch, reason):
    calls = []

    def solve(provider, origin, timeout):
        calls.append(provider["id"])
        raise AccessFailure(reason, "validation", "synthetic")

    monkeypatch.setattr(chain, "adapters", lambda: {"flaresolverr": solve})
    assert list(chain.candidates("sample", ORIGIN, settings(max_attempts=4))) == []
    assert calls == ["first"]


def test_missing_cookie_outside_validation_does_not_retry(safe_dns, monkeypatch):
    calls = []

    def solve(provider, origin, timeout):
        calls.append(provider["id"])
        raise AccessFailure("missing_cookie", "service", "synthetic")

    monkeypatch.setattr(chain, "adapters", lambda: {"flaresolverr": solve})
    assert list(chain.candidates("sample", ORIGIN, settings(max_attempts=4))) == []
    assert calls == ["first"]


def test_ambiguous_cookie_does_not_authorize_last_chance(safe_dns, monkeypatch):
    calls = []

    def solve(provider, origin, timeout):
        calls.append(provider["id"])
        raw = solution()
        raw["solution"]["cookies"].append(dict(raw["solution"]["cookies"][0]))
        return providers._solution(raw, origin, provider["id"])

    monkeypatch.setattr(chain, "adapters", lambda: {"flaresolverr": solve})
    assert list(chain.candidates("sample", ORIGIN, settings(max_attempts=4))) == []
    assert calls == ["first"]


def test_last_chance_uses_anonymous_public_api_and_does_not_cache(safe_dns, monkeypatch):
    seen = []
    real_client = httpx.Client

    def handler(request):
        seen.append(request)
        return (
            httpx.Response(200, json=solution(cookies=[])) if len(seen) == 1 else httpx.Response(200, json=solution())
        )

    def client(**kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        kwargs["transport"].close()
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(**kwargs)

    monkeypatch.setattr(providers.httpx, "Client", client)
    monkeypatch.setattr(chain, "adapters", lambda: {"flaresolverr": providers.cookie_api})
    values = list(chain.candidates("kinozal", ORIGIN, settings()))
    assert len(values) == 1
    assert values[0].cookies == {"cf_clearance": "synthetic-token"}
    assert len(seen) == 2
    for request in seen:
        payload = json.loads(request.content)
        assert payload["url"] == ORIGIN + "/login.php"
        assert payload["returnOnlyCookies"] is True
        assert "cookie" not in request.headers
        assert "authorization" not in request.headers
        assert set(payload) == {"cmd", "url", "maxTimeout", "returnOnlyCookies", "disableMedia"}
    assert store.read("kinozal", ORIGIN, settings(), now=time.time()) is None


def test_last_chance_never_takes_a_call_reserved_for_a_later_mirror(safe_dns, monkeypatch):
    # The contract before 1.30.11 let the first mirror's last chance spend the second mirror's
    # only call; a call is now reserved for every later mirror that can still be recovered.
    calls = []

    def solve(provider, origin, timeout):
        calls.append(origin)
        raise AccessFailure("missing_cookie", "validation", "synthetic")

    monkeypatch.setattr(chain, "adapters", lambda: {"flaresolverr": solve})
    budget = RecoveryBudget(2)
    first = budget.for_origin(later=1)
    assert first.remaining == 1
    assert list(chain.candidates("sample", ORIGIN, settings(), call_budget=first)) == []
    assert list(chain.candidates("sample", "https://other.example", settings(), call_budget=budget.for_origin(0))) == []
    assert calls == [ORIGIN, "https://other.example"]
    assert budget.remaining == 0


def test_shared_call_limit_is_never_exceeded_by_origin_shares():
    budget = RecoveryBudget(3)
    alone = budget.for_origin(later=0)
    assert alone.remaining == 3
    crowded = budget.for_origin(later=5)
    assert crowded.remaining == 1  # the current mirror always keeps one call
    assert crowded.consume()
    assert not crowded.consume()
    assert budget.remaining == 2
    assert RecoveryBudget(0).for_origin(later=0).remaining == 0
    share = budget.for_origin(later=0)
    budget.remaining = 0  # spent elsewhere: a share cannot outlive the shared limit
    assert not share.consume()


def _challenged_mirrors(monkeypatch, cfg, hosts):
    from test_protection import CF, install

    install(monkeypatch, lambda request: httpx.Response(403, text=CF), hosts=hosts, protection=cfg)
    monkeypatch.setattr(chain, "public_addresses", lambda *args: ["93.184.216.34"])
    calls = []

    def solve(provider, origin, timeout):
        calls.append(origin)
        raise AccessFailure("missing_cookie", "validation", "synthetic")

    monkeypatch.setattr(chain, "adapters", lambda: {"flaresolverr": solve})
    return calls


def test_each_challenged_mirror_gets_a_call_before_any_last_chance(monkeypatch):
    from tow import mirrors

    hosts = ["https://a.example", "https://b.example"]
    cfg = settings(max_attempts=2)
    calls = _challenged_mirrors(monkeypatch, cfg, hosts)
    with pytest.raises(mirrors.MirrorFetchError):
        mirrors.pick_and_get("demo", hosts, "/topic/1", protection=cfg)
    assert calls == hosts


def test_one_challenged_mirror_still_gets_its_last_chance(monkeypatch):
    from tow import mirrors

    hosts = ["https://a.example"]
    cfg = settings(max_attempts=2)
    calls = _challenged_mirrors(monkeypatch, cfg, hosts)
    with pytest.raises(mirrors.MirrorFetchError):
        mirrors.pick_and_get("demo", hosts, "/topic/1", protection=cfg)
    assert calls == hosts * 2


@pytest.mark.parametrize("why", ["rate_limited", "recently_tried"])
def test_a_mirror_that_cannot_be_recovered_reserves_no_call(monkeypatch, why):
    from tow import mirrors

    hosts = ["https://a.example", "https://b.example"]
    cfg = settings(max_attempts=2)
    calls = _challenged_mirrors(monkeypatch, cfg, hosts)
    if why == "rate_limited":
        # Challenged in this run, then paused by the site's own deadline: recovery skips it.
        monkeypatch.setattr(mirrors, "_host_paused", lambda bucket, host, **kw: host == hosts[1] and kw["ignore_cool"])
    else:
        assert store.reserve_attempt("demo", hosts[1], cfg, now=time.time()) is not None
    with pytest.raises(mirrors.MirrorFetchError):
        mirrors.pick_and_get("demo", hosts, "/topic/1", protection=cfg)
    assert calls == [hosts[0], hosts[0]]


def test_only_verified_clearance_retained_and_encrypted(safe_dns, monkeypatch):
    calls = []

    def solve(*args):
        calls.append(1)
        return clearance()

    monkeypatch.setattr(chain, "adapters", lambda: {"flaresolverr": solve})
    value = next(chain.candidates("sample", ORIGIN, settings()))
    assert store.read("sample", ORIGIN, settings(), now=time.time()) is None
    chain.accept("sample", ORIGIN, settings(), value)
    assert b"synthetic-token" not in (data_dir() / "protection.enc").read_bytes()
    assert list(chain.candidates("sample", ORIGIN, settings())) == [value]
    assert calls == [1]
    chain.discard("sample", ORIGIN)
    assert list(chain.candidates("sample", ORIGIN, settings())) == []
    assert calls == [1]  # discard does not erase cooldown


def test_accept_without_reservation_never_creates_cache():
    chain.accept("sample", ORIGIN, settings(), clearance())
    assert not (data_dir() / "protection.enc").exists()


def test_configuration_changes_bind_cache_but_not_bypass_cooldown():
    configured = settings()
    assert store.reserve("sample", ORIGIN, configured, now=time.time())
    store.accept("sample", ORIGIN, configured, clearance(), now=time.time())
    changed = settings(timeout_sec=40)
    assert store.read("sample", ORIGIN, changed, now=time.time()) is None
    assert store.reserve("sample", ORIGIN, changed, now=time.time()) is False


def test_origin_and_site_are_isolated():
    assert store.reserve("sample", ORIGIN, settings(), now=time.time())
    store.accept("sample", ORIGIN, settings(), clearance(), now=time.time())
    assert store.read("other", ORIGIN, settings(), now=time.time()) is None
    assert store.read("sample", "https://other.example", settings(), now=time.time()) is None


def test_corrupt_storage_preserved_and_no_external_calls(safe_dns, monkeypatch):
    path = data_dir() / "protection.enc"
    path.write_bytes(b"corrupt-encrypted-record")
    monkeypatch.setattr(chain, "adapters", lambda: pytest.fail("provider requested"))
    assert list(chain.candidates("sample", ORIGIN, settings())) == []
    chain.accept("sample", ORIGIN, settings(), clearance())
    chain.discard("sample", ORIGIN)
    assert path.read_bytes() == b"corrupt-encrypted-record"


def test_unwritable_reservation_prevents_external_calls(safe_dns, monkeypatch):
    monkeypatch.setattr(store, "atomic_write_bytes", lambda *args: (_ for _ in ()).throw(PermissionError()))
    monkeypatch.setattr(chain, "adapters", lambda: {"byparr": lambda *args: pytest.fail("provider requested")})
    assert list(chain.candidates("sample", ORIGIN, settings())) == []


def test_private_target_refused_even_if_tracker_allows_private(monkeypatch):
    monkeypatch.setattr(chain, "public_addresses", lambda *a: (_ for _ in ()).throw(httpx.ConnectError("private")))
    assert list(chain.candidates("sample", "http://192.168.1.2", settings(), public_only=False)) == []
    assert not (data_dir() / "protection.enc").exists()


@pytest.mark.parametrize(
    "changes",
    [
        {"url": "https://other.example/"},
        {"url": "https://user:pass@tracker.example/"},
        {"status": 403},
        {"userAgent": "bad\r\nHeader"},
        {"cookies": []},
    ],
)
def test_solution_refuses_unverified_or_unsafe_data(changes):
    with pytest.raises((ValueError, TypeError)):
        providers._solution(solution(**changes), ORIGIN, "first")


@pytest.mark.parametrize(
    "changes",
    [
        {"domain": ".example"},
        {"path": "/topic"},
        {"value": "bad;cookie"},
        {"expires": float("nan")},
        {"expires": time.time() - 10},
        {"name": "session"},
    ],
)
def test_solution_cookie_scope_and_expiry(changes):
    raw = solution()
    raw["solution"]["cookies"][0].update(changes)
    with pytest.raises((ValueError, TypeError)):
        providers._solution(raw, ORIGIN, "first")


def test_cookie_only_protocol_never_sends_user_credentials(monkeypatch):
    seen = []
    real_client = httpx.Client

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=solution())

    def client(**kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        kwargs["transport"].close()
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(**kwargs)

    monkeypatch.setattr(providers.httpx, "Client", client)
    value = providers.cookie_api(settings()["providers"][0], ORIGIN, 15)
    payload = json.loads(seen[0].content)
    assert payload == {
        "cmd": "request.get",
        "url": ORIGIN + "/",
        "maxTimeout": 14000,
        "returnOnlyCookies": True,
        "disableMedia": True,
    }
    assert "cookie" not in seen[0].headers
    assert "authorization" not in seen[0].headers
    assert value.cookies == {"cf_clearance": "synthetic-token"}


def test_byparr_uses_its_documented_media_flag(monkeypatch):
    from tow.protection.solvers import byparr

    seen = []
    real_client = httpx.Client

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=solution())

    def client(**kwargs):
        kwargs["transport"].close()
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(**kwargs)

    monkeypatch.setattr(providers.httpx, "Client", client)
    assert byparr.solve(settings()["providers"][1], ORIGIN, 15).provider == "second"
    assert seen[0]["blockMedia"] is True
    assert "disableMedia" not in seen[0]


@pytest.mark.parametrize(("timeout", "browser_ms"), [(1, 1000), (1.5, 1500), (2, 1000), (30, 29000)])
def test_browser_timeout_margin_never_uses_ambiguous_subsecond_units(monkeypatch, timeout, browser_ms):
    seen = []
    real_client = httpx.Client
    # This checks protocol units, not elapsed time under parallel runner load.
    monkeypatch.setattr(providers.time, "monotonic", lambda: 1000.0)

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=solution())

    def client(**kwargs):
        kwargs["transport"].close()
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(**kwargs)

    monkeypatch.setattr(providers.httpx, "Client", client)
    providers.cookie_api(settings()["providers"][0], ORIGIN, timeout)
    assert seen[0]["maxTimeout"] == browser_ms


@pytest.mark.parametrize(
    ("status", "body"),
    [(302, b""), (500, b""), (200, b"x" * (1024 * 1024 + 1)), (200, b"not-json")],
    ids=["redirect", "server-error", "oversized", "bad-json"],
)
def test_http_redirect_errors_oversize_and_bad_json(monkeypatch, status, body):
    real_client = httpx.Client

    def client(**kwargs):
        kwargs["transport"].close()
        kwargs["transport"] = httpx.MockTransport(lambda request: httpx.Response(status, content=body))
        return real_client(**kwargs)

    monkeypatch.setattr(providers.httpx, "Client", client)
    with pytest.raises(ValueError, match=r"HTTP failure|response limit|Expecting value"):
        providers.cookie_api(settings()["providers"][0], ORIGIN, 15)


def test_expired_cache_is_not_used():
    now = time.time()
    store.reserve("sample", ORIGIN, settings(), now=now)
    store.accept("sample", ORIGIN, settings(), clearance(expires_at=now + 1), now=now)
    assert store.read("sample", ORIGIN, settings(), now=now + 2) is None


def test_future_reservation_clock_correction_fail_closed():
    now = time.time()
    store.reserve("sample", ORIGIN, settings(), now=now + 3600)
    assert store.reserve("sample", ORIGIN, settings(), now=now) is False


@pytest.mark.parametrize("budget", [0, -1, float("nan"), float("inf"), True])
def test_invalid_or_exhausted_budget_is_noop(safe_dns, budget):
    assert list(chain.candidates("sample", ORIGIN, settings(), budget_sec=budget)) == []
    assert safe_dns == []
    assert not (data_dir() / "protection.enc").exists()


def test_budget_passed_to_solver(safe_dns, monkeypatch):
    calls = []
    monkeypatch.setattr(
        chain, "adapters", lambda: {"byparr": lambda p, o, timeout: calls.append(timeout) or clearance("second")}
    )
    assert len(list(chain.candidates("sample", ORIGIN, settings(), budget_sec=3))) == 1
    assert 0 < calls[0] <= 3


@pytest.mark.parametrize("budget", [5, 6, 2])
def test_short_budget_still_attempts_available_providers(safe_dns, monkeypatch, budget):
    configured = settings(
        timeout_sec=5,
        max_attempts=4,
        providers=[
            {"id": f"service{i}", "kind": "byparr", "url": f"http://127.0.0.1:{8191 + i}", "enabled": True}
            for i in range(4)
        ],
    )
    clock = [100.0]
    monkeypatch.setattr(chain.time, "monotonic", lambda: clock[0])
    calls = []

    def solve(provider, origin, timeout):
        calls.append((provider["id"], timeout))
        clock[0] += timeout
        raise ValueError("synthetic site refusal")

    monkeypatch.setattr(chain, "adapters", lambda: {"byparr": solve})
    assert list(chain.candidates("sample", ORIGIN, configured, budget_sec=budget)) == []
    assert calls
    assert all(timeout >= 1 for _, timeout in calls)
    effective_budget = min(5, budget)
    assert sum(timeout for _, timeout in calls) <= effective_budget * 2 / 3 + 1e-9
    assert len(calls) <= 4


@pytest.mark.parametrize("budget", [1, 1.25, 1.49])
def test_insufficient_solver_budget_does_not_consume_cooldown(safe_dns, monkeypatch, budget):
    configured = settings()
    monkeypatch.setattr(chain.time, "monotonic", lambda: 100.0)
    calls = []
    monkeypatch.setattr(
        chain, "adapters", lambda: {"byparr": lambda p, o, timeout: calls.append(timeout) or clearance("second")}
    )
    assert list(chain.candidates("sample", ORIGIN, configured, budget_sec=budget)) == []
    assert calls == []
    assert not (data_dir() / "protection.enc").exists()
    assert len(list(chain.candidates("sample", ORIGIN, configured))) == 1
    assert len(calls) == 1


@pytest.mark.parametrize("stage", ["reserve", "cursor", "log"])
def test_preparation_exhaustion_releases_only_unused_reservation(safe_dns, monkeypatch, stage):
    configured = settings(max_attempts=1)
    clock = [100.0]
    monkeypatch.setattr(chain.time, "monotonic", lambda: clock[0])
    if stage == "log":
        monkeypatch.setattr(chain, "record_event", lambda *args, **kwargs: clock.__setitem__(0, clock[0] + 0.5))
    else:
        name = "reserve_attempt" if stage == "reserve" else "note_attempt"
        original = getattr(store, name)

        def slow(*args, **kwargs):
            result = original(*args, **kwargs)
            clock[0] += 0.5
            return result

        monkeypatch.setattr(store, name, slow)
    calls = []
    monkeypatch.setattr(
        chain, "adapters", lambda: {"byparr": lambda p, o, timeout: calls.append(timeout) or clearance("second")}
    )
    assert list(chain.candidates("sample", ORIGIN, configured, budget_sec=2)) == []
    assert calls == []
    assert store.reserve("sample", ORIGIN, configured, now=time.time()) is True


def test_missing_plugin_not_counted_as_attempt(safe_dns, monkeypatch):
    monkeypatch.setattr(chain, "adapters", lambda: {"byparr": lambda *args: clearance("second")})
    assert [c.provider for c in chain.candidates("sample", ORIGIN, settings(max_attempts=1))] == ["second"]


def test_lease_prevents_overlap_when_timeout_exceeds_min_interval():
    configured = settings(timeout_sec=90, min_interval_sec=60)
    now = time.time()
    assert store.reserve("sample", ORIGIN, configured, now=now)
    assert store.reserve("sample", ORIGIN, configured, now=now + 61) is False
    assert store.reserve("sample", ORIGIN, settings(timeout_sec=5, min_interval_sec=60), now=now + 61) is False
    assert store.reserve("sample", ORIGIN, configured, now=now + 96) is True


@pytest.mark.parametrize("url", ["http://@127.0.0.1", "http://127.0.0.1:", "http://127.0.0.1\x00"])
def test_hidden_or_empty_credentials_and_controls_refused(url):
    with pytest.raises(ValueError, match="invalid clearance service"):
        service_url(url)


def test_stream_operations_share_absolute_deadline(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(providers.time, "monotonic", lambda: clock[0])
    calls = []

    class FakeStream(httpcore.NetworkStream):
        def read(self, max_bytes, timeout=None):
            calls.append(("read", timeout))
            return b"x"

        def write(self, buffer, timeout=None):
            calls.append(("write", timeout))

        def start_tls(self, ssl_context, server_hostname=None, timeout=None):
            calls.append(("tls", timeout))
            return self

        def get_extra_info(self, info):
            return info

        def close(self):
            calls.append(("close", None))

    from tow.protection.transport import DeadlineStream

    wrapped = DeadlineStream(FakeStream(), 110)
    assert wrapped.read(1, timeout=30) == b"x"
    clock[0] = 104
    wrapped.write(b"x", timeout=30)
    clock[0] = 107
    secured = wrapped.start_tls(None, "service", timeout=30)
    assert secured.get_extra_info("field") == "field"
    assert calls == [("read", 10), ("write", 6), ("tls", 3)]
    clock[0] = 110
    with pytest.raises(httpcore.ReadTimeout, match="budget exhausted"):
        secured.read(1, timeout=30)
    secured.close()
    assert calls[-1] == ("close", None)


def test_compressed_response_refused(monkeypatch):
    import gzip

    real_client = httpx.Client

    def client(**kwargs):
        kwargs["transport"].close()
        kwargs["transport"] = httpx.MockTransport(
            lambda request: httpx.Response(
                200, content=gzip.compress(json.dumps(solution()).encode()), headers={"content-encoding": "gzip"}
            )
        )
        return real_client(**kwargs)

    monkeypatch.setattr(providers.httpx, "Client", client)
    with pytest.raises(ValueError, match="compressed clearance"):
        providers.cookie_api(settings()["providers"][0], ORIGIN, 15)


def test_cached_is_read_only_without_dns_or_lock(monkeypatch):
    configured = settings()
    now = time.time()
    store.reserve("sample", ORIGIN, configured, now=now)
    value = clearance()
    store.accept("sample", ORIGIN, configured, value, now=now)
    monkeypatch.setattr(store, "persistence_lock", lambda: pytest.fail("read acquired write lock"))
    monkeypatch.setattr(chain, "public_addresses", lambda *a: pytest.fail("read asked DNS"))
    assert chain.cached("sample", ORIGIN, configured) == value
    assert chain.cached("other", ORIGIN, configured) is None
    assert chain.cached("sample", ORIGIN, settings(timeout_sec=40)) is None


def test_cached_disabled_no_storage(monkeypatch):
    monkeypatch.setattr(store, "read", lambda *a, **kw: pytest.fail("storage accessed"))
    assert chain.cached("sample", ORIGIN, settings(enabled=False)) is None


def test_cached_unavailable_preserves_file():
    path = data_dir() / "protection.enc"
    path.write_bytes(b"corrupt")
    assert chain.cached("sample", ORIGIN, settings()) is None
    assert path.read_bytes() == b"corrupt"


def test_concurrent_reservations_only_one_external_chain():
    from concurrent.futures import ThreadPoolExecutor

    now = time.time()
    configured = settings()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: store.reserve("sample", ORIGIN, configured, now=now), range(4)))
    assert results.count(True) == 1
    assert results.count(False) == 3


def test_capacity_is_bounded_and_only_expired_reservations_pruned(monkeypatch):
    monkeypatch.setattr(store, "_MAX_RECORDS", 2)
    now = time.time()
    assert store.reserve("first", ORIGIN, settings(), now=now)
    assert store.reserve("second", ORIGIN, settings(), now=now)
    assert store.reserve("third", ORIGIN, settings(), now=now) is False
    assert store.reserve("third", ORIGIN, settings(), now=now + 86401)
    assert len(store._load()) == 1


def test_response_only_one_clearance_cookie_allowed():
    raw = solution()
    raw["solution"]["cookies"].append(dict(raw["solution"]["cookies"][0]))
    with pytest.raises(AccessFailure, match="ambiguous clearance") as caught:
        providers._solution(raw, ORIGIN, "first")
    assert caught.value.reason == "invalid_response"


def test_session_cookie_lifetime_is_bounded(monkeypatch):
    now = time.time()
    monkeypatch.setattr(providers.time, "time", lambda: now)
    raw = solution()
    raw["solution"]["cookies"][0]["expires"] = -1
    assert providers._solution(raw, ORIGIN, "first").expires_at == now + 3600
    raw["solution"]["cookies"][0]["expires"] = now + 30 * 86400
    assert providers._solution(raw, ORIGIN, "first").expires_at == now + 86400


def test_credentials_do_not_appear_in_repr_or_failure_log(caplog, safe_dns, monkeypatch):
    assert "synthetic-token" not in repr(clearance())

    def fail(*args):
        raise ValueError("synthetic-token must not be logged")

    monkeypatch.setattr(chain, "adapters", lambda: {"byparr": fail})
    with caplog.at_level("DEBUG", logger="tow.protection.chain"):
        assert list(chain.candidates("sample", ORIGIN, settings())) == []
    assert "synthetic-token" not in caplog.text


def test_replay_elapsed_budget_prevents_new_external_call(safe_dns, monkeypatch):
    configured = settings()
    now = time.time()
    store.reserve("sample", ORIGIN, configured, now=now - 3600)
    store.accept("sample", ORIGIN, configured, clearance(), now=now)
    clock = [100.0]
    monkeypatch.setattr(chain.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(chain, "adapters", lambda: pytest.fail("replay exhausted budget"))
    iterator = chain.candidates("sample", ORIGIN, configured, budget_sec=5)
    assert next(iterator).provider == "first"
    clock[0] = 106.0
    assert list(iterator) == []


def test_failed_old_replay_cannot_discard_newer_verified_clearance():
    configured = settings()
    now = time.time()
    store.reserve("sample", ORIGIN, configured, now=now)
    old = clearance(cookies={"cf_clearance": "synthetic-old-token"})
    store.accept("sample", ORIGIN, configured, old, now=now)
    assert chain.cached("sample", ORIGIN, configured) == old
    old = chain.cached("sample", ORIGIN, configured)
    replacement = clearance(cookies={"cf_clearance": "synthetic-new-token"}, provider="second")
    store.accept("sample", ORIGIN, configured, replacement, now=now)
    chain.discard("sample", ORIGIN, expected=old)
    assert chain.cached("sample", ORIGIN, configured) == replacement
    chain.discard("sample", ORIGIN, expected=chain.cached("sample", ORIGIN, configured))
    assert chain.cached("sample", ORIGIN, configured) is None
    assert store.reserve("sample", ORIGIN, configured, now=now + 1) is False


def test_discard_compares_full_credential_not_only_token():
    configured = settings()
    now = time.time()
    store.reserve("sample", ORIGIN, configured, now=now)
    old = clearance()
    new = clearance(user_agent="Synthetic/2.0", expires_at=old.expires_at + 1)
    store.accept("sample", ORIGIN, configured, new, now=now)
    chain.discard("sample", ORIGIN, expected=old)
    assert chain.cached("sample", ORIGIN, configured) == new


@pytest.mark.parametrize("domain", ["example.org", ".example.org", ".EXAMPLE.ORG", "www.example.org"])
def test_parent_domain_cookie_is_exact_origin_scoped(domain):
    origin = "https://www.example.org"
    raw = solution(url=origin + "/landing")
    raw["solution"]["cookies"][0]["domain"] = domain
    assert providers._solution(raw, origin, "first").cookies == {"cf_clearance": "synthetic-token"}


@pytest.mark.parametrize(
    "domain", ["org", ".org", ".evil-example.org", ".other.example.org", "example.org.evil", "..example.org"]
)
def test_cookie_scope_refuses_suffix_and_sibling_domains(domain):
    raw = solution(url="https://www.example.org/")
    raw["solution"]["cookies"][0]["domain"] = domain
    with pytest.raises(ValueError, match="scope mismatch"):
        providers._solution(raw, "https://www.example.org", "first")


def test_publicsuffix_shaped_cookie_attribute_never_grants_parent_or_sibling_reuse():
    origin = "https://www.example.co.uk"
    raw = solution(url="https://www.example.co.uk/")
    raw["solution"]["cookies"][0]["domain"] = ".co.uk"
    candidate = providers._solution(raw, origin, "first")
    assert candidate.cookies == {"cf_clearance": "synthetic-token"}
    assert "domain" not in candidate.cookies
    configured = settings()
    now = time.time()
    store.reserve("sample", origin, configured, now=now)
    store.accept("sample", origin, configured, candidate, now=now)
    assert chain.cached("sample", origin, configured) == candidate
    assert chain.cached("sample", "https://co.uk", configured) is None
    assert chain.cached("sample", "https://other.example.co.uk", configured) is None
    assert chain.cached("sample", "https://www.other.co.uk", configured) is None


@pytest.mark.parametrize("domain", ["example_.org", "-example.org", "example-.org", "example..org", "example.org."])
def test_cookie_scope_refuses_invalid_dns_labels(domain):
    raw = solution(url="https://www.example.org/")
    raw["solution"]["cookies"][0]["domain"] = domain
    with pytest.raises(ValueError, match="scope mismatch"):
        providers._solution(raw, "https://www.example.org", "first")


# --- a local module that misses its startup deadline -------------------------------------------


@pytest.mark.parametrize("where", ["launch", "after_launch"])
def test_local_startup_deadline_is_a_service_outage(monkeypatch, where):
    from tow.protection import local_service
    from tow.protection.spec import ProviderUnavailableError

    if where == "launch":
        monkeypatch.setattr(local_service, "ensure", lambda *a: (_ for _ in ()).throw(TimeoutError("synthetic")))
    else:
        clock = [100.0]
        monkeypatch.setattr(providers.time, "monotonic", lambda: clock[0])

        def started(*args):
            clock[0] += 14.5  # the module came up, leaving no time for its request
            return True

        monkeypatch.setattr(local_service, "ensure", started)
    monkeypatch.setattr(providers, "_transport", lambda *a: pytest.fail("no request after a missed startup"))
    with pytest.raises(ProviderUnavailableError) as caught:
        providers.cookie_api(settings()["providers"][0], ORIGIN, 15)
    assert (caught.value.reason, caught.value.phase) == ("unavailable", "startup")
    assert chain.failure_details(caught.value) == ("unavailable", "startup")


def test_startup_timeout_pauses_that_service_for_every_site(safe_dns, monkeypatch):
    from tow.protection import local_service

    calls = []
    monkeypatch.setattr(local_service, "ensure", lambda *a: (_ for _ in ()).throw(TimeoutError("synthetic")))

    def other(provider, origin, timeout):
        calls.append(("second", origin))
        return clearance("second")

    def local(provider, origin, timeout):
        calls.append(("first", origin))
        return providers.cookie_api(provider, origin, timeout)

    monkeypatch.setattr(chain, "adapters", lambda: {"flaresolverr": local, "byparr": other})
    assert [c.provider for c in chain.candidates("sample", ORIGIN, settings())] == ["second"]
    states = store.service_states(settings()["providers"], now=time.time())
    assert states["first"]["status"] == "unavailable"
    assert states["first"]["retry_at"] > time.time()
    assert states["second"]["status"] == "succeeded"
    # Another site: the paused module is not started again, the other service still runs.
    assert [c.provider for c in chain.candidates("other", "https://other.example", settings())] == ["second"]
    assert calls == [("first", ORIGIN), ("second", ORIGIN), ("second", "https://other.example")]


def test_a_site_solving_timeout_does_not_pause_the_service(safe_dns, monkeypatch):
    def solving(provider, origin, timeout):
        raise AccessFailure("timeout", "service", "synthetic")

    monkeypatch.setattr(chain, "adapters", lambda: {"flaresolverr": solving})
    assert list(chain.candidates("sample", ORIGIN, settings())) == []
    assert store.service_states(settings()["providers"], now=time.time())["first"]["status"] == "unknown"


# --- a warming local module does not cost the site its retry interval --------------------------


def _warming():
    from tow.protection.spec import ProviderUnavailableError

    return ProviderUnavailableError("busy", retry_after=5, phase="startup")


@pytest.fixture
def clock(monkeypatch):
    now = [time.time()]
    monkeypatch.setattr(chain.time, "time", lambda: now[0])
    return now


def test_warming_gives_the_site_its_reservation_back(safe_dns, monkeypatch, clock):
    calls = []

    def solve(provider, origin, timeout):
        calls.append(provider["id"])
        if len(calls) == 1:
            raise _warming()
        return clearance()

    monkeypatch.setattr(chain, "adapters", lambda: {"flaresolverr": solve})
    configured = settings(min_interval_sec=1800)
    assert list(chain.candidates("sample", ORIGIN, configured)) == []
    state = store.service_states(configured["providers"], now=clock[0])["first"]
    assert state["status"] == "busy"  # the module's own backoff stays
    assert list(chain.candidates("sample", ORIGIN, configured)) == []  # ... and is not retried at once
    assert calls == ["first"]
    clock[0] = state["retry_at"] + 1  # warm now: no 1800-second wait for the site
    assert [c.provider for c in chain.candidates("sample", ORIGIN, configured)] == ["first"]
    assert calls == ["first", "first"]


@pytest.mark.parametrize(
    "failure",
    [
        lambda: __import__("tow.protection.spec", fromlist=["x"]).ProviderUnavailableError("busy", retry_after=5),
        lambda: AccessFailure("timeout", "service", "synthetic"),
        lambda: httpx.ReadTimeout("synthetic"),
    ],
    ids=["service_busy", "solve_timeout", "read_timeout"],
)
def test_a_call_that_may_have_asked_the_site_keeps_the_reservation(safe_dns, monkeypatch, clock, failure):
    calls = []

    def solve(provider, origin, timeout):
        calls.append(provider["id"])
        raise failure()

    monkeypatch.setattr(chain, "adapters", lambda: {"flaresolverr": solve})
    configured = settings(min_interval_sec=1800)
    assert list(chain.candidates("sample", ORIGIN, configured)) == []
    clock[0] += 1799
    assert list(chain.candidates("sample", ORIGIN, configured)) == []
    assert calls == ["first"]


def test_warming_after_another_service_tried_the_site_keeps_the_reservation(safe_dns, monkeypatch, clock):
    calls = []

    def refused(provider, origin, timeout):
        calls.append(provider["id"])
        raise AccessFailure("refused", "service", "synthetic")

    def warming(provider, origin, timeout):
        calls.append(provider["id"])
        raise _warming()

    monkeypatch.setattr(chain, "adapters", lambda: {"byparr": refused, "flaresolverr": warming})
    configured = settings(min_interval_sec=1800, providers=[*reversed(settings()["providers"])])
    assert list(chain.candidates("sample", ORIGIN, configured)) == []
    assert calls == ["second", "first"]
    clock[0] += 600
    assert list(chain.candidates("sample", ORIGIN, configured)) == []
    assert calls == ["second", "first"]


def test_warming_never_removes_a_newer_reservation(safe_dns, monkeypatch, clock):
    newer = []

    def solve(provider, origin, timeout):
        (data_dir() / "protection.enc").unlink()  # an explicit reset during the call ...
        newer.append(store.reserve_attempt("sample", ORIGIN, settings(), now=clock[0]))  # ... and a new run
        raise _warming()

    monkeypatch.setattr(chain, "adapters", lambda: {"flaresolverr": solve})
    assert list(chain.candidates("sample", ORIGIN, settings())) == []
    assert newer[0] is not None
    assert store.reserve_attempt("sample", ORIGIN, settings(), now=clock[0] + 60) is None


@pytest.mark.parametrize(("local", "phase"), [(True, "startup"), (False, "service")])
def test_only_the_local_module_can_say_it_is_warming(monkeypatch, local, phase):
    from tow.protection import local_service
    from tow.protection.spec import ProviderUnavailableError

    real_client = httpx.Client

    def client(**kwargs):
        kwargs["transport"].close()
        kwargs["transport"] = httpx.MockTransport(
            lambda request: httpx.Response(
                503, json={"status": "error"}, headers={"Retry-After": "5", "X-TOW-Module-State": "warming"}
            )
        )
        return real_client(**kwargs)

    monkeypatch.setattr(providers.httpx, "Client", client)
    monkeypatch.setattr(local_service, "ensure", lambda *a: local)
    with pytest.raises(ProviderUnavailableError) as caught:
        providers.cookie_api(settings()["providers"][0], ORIGIN, 15)
    assert (caught.value.reason, caught.value.phase) == ("busy", phase)


def test_services_are_asked_for_the_uncompressed_answer_they_must_give(monkeypatch):
    seen = []
    real_client = httpx.Client

    def client(**kwargs):
        kwargs["transport"].close()
        kwargs["transport"] = httpx.MockTransport(
            lambda request: seen.append(request) or httpx.Response(200, json=solution())
        )
        return real_client(**kwargs)

    monkeypatch.setattr(providers.httpx, "Client", client)
    providers.cookie_api(settings()["providers"][0], ORIGIN, 15)
    assert seen[0].headers["accept-encoding"] == "identity"


@pytest.mark.parametrize(
    "reason", ["redirect_policy", "redirect_loop", "maintenance", "gone", "unexpected", "http", "http_topic"]
)
def test_every_replay_refusal_reason_is_recorded_and_explained(monkeypatch, reason):
    from tow import i18n
    from tow.log import format_event

    events = []
    monkeypatch.setattr(chain, "log_event", lambda kind, **fields: events.append(fields))
    chain.record_event("protection_replay_refused", "demo", "local", "failed", reason=reason, phase="resource")
    assert events[0]["reason"] == reason
    try:
        for lang in ("en", "ru"):
            i18n.use(lang)
            detail = format_event({"kind": "protection_replay_refused", "reason": reason, "phase": "resource"})
            assert i18n.t(f"site_access.reason.{reason}") in detail["detail"]
    finally:
        i18n.use("en")


def test_a_refused_replay_names_the_sites_actual_answer(monkeypatch):
    from test_protection import CF, install
    from test_protection import clearance as verified
    from test_protection import settings as protection

    from tow import mirrors

    def handler(request):
        if "cf_clearance=" not in request.headers.get("cookie", ""):
            return httpx.Response(403, text=CF)
        if request.url.path == "/":
            return httpx.Response(200, text="front page")
        return httpx.Response(302, headers={"location": "https://elsewhere.example/topic/1"})

    install(monkeypatch, handler)
    monkeypatch.setattr(chain, "candidates", lambda *a, **kw: iter([verified()]))
    events = []
    monkeypatch.setattr(chain, "log_event", lambda kind, **fields: events.append((kind, fields)))
    with pytest.raises(mirrors.MirrorFetchError):
        mirrors.pick_and_get("demo", ["https://site.example"], "/topic/1", protection=protection())
    refused = [fields for kind, fields in events if kind == "protection_replay_refused"]
    assert refused[0]["reason"] == "redirect_policy"
    assert refused[0]["phase"] == "resource"
