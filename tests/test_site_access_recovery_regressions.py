"""Whole recovery chains across mirrors, with synthetic HTTP and isolated storage."""

from __future__ import annotations

import time
from dataclasses import replace

import httpx
import pytest
from test_protection import CF, TORRENT, clearance, install, settings
from test_protection_providers import settings as configured_settings

from tow import mirrors
from tow.errors import diagnostic_details
from tow.paths import data_dir
from tow.protection import chain, store
from tow.protection.spec import RecoveryBudget
from tow.store import atomic_write_bytes, load_state, master_fernet

HOSTS = ["https://a.example", "https://b.example", "https://c.example"]


@pytest.mark.parametrize(
    "first_failure", ["path_challenge", "probe_500", "probe_transport", "probe_rate", "resource_transport"]
)
def test_other_challenged_mirror_is_recovered_after_first_origin_fails(monkeypatch, first_failure):
    attempts, closed = [], []

    def response(request):
        if "cf_clearance=" not in request.headers.get("cookie", ""):
            return httpx.Response(403, text=CF)
        if request.url.host == "a.example":
            if request.url.path == "/":
                if first_failure == "probe_500":
                    return httpx.Response(500)
                if first_failure == "probe_transport":
                    raise httpx.ConnectError("synthetic outage")
                if first_failure == "probe_rate":
                    return httpx.Response(429, headers={"retry-after": "600"})
            else:
                if first_failure == "resource_transport":
                    raise httpx.ReadTimeout("synthetic outage")
                return httpx.Response(403, text=CF)
        return httpx.Response(200, content=b"public page" if request.url.path == "/" else TORRENT)

    install(monkeypatch, response, hosts=HOSTS[:2])

    def candidates(site, origin, *args, **kwargs):
        attempts.append(origin)
        try:
            yield clearance()
            pytest.fail("a non-challenge probe failure or verified path refusal must stop this origin")
        finally:
            closed.append(origin)

    monkeypatch.setattr(chain, "candidates", candidates)
    result, origin = mirrors.pick_and_get(
        "demo", HOSTS[:2], "/download/1", ok=lambda r: r.content == TORRENT, protection=settings()
    )
    assert origin == HOSTS[1]
    assert result.content == TORRENT
    assert attempts == HOSTS[:2]
    assert closed == HOSTS[:2]
    if first_failure == "probe_rate":
        assert load_state()["mirrors"]["demo"]["rate_cool"][HOSTS[0]] > time.time()
        assert load_state()["mirrors"]["demo"]["fail"][HOSTS[0]] == 2


def test_failed_recovery_keeps_each_mirrors_actual_reason(monkeypatch):
    def response(request):
        if "cf_clearance=" not in request.headers.get("cookie", ""):
            return httpx.Response(403, text=CF)
        if request.url.path == "/":
            return httpx.Response(200, text="public page")
        return httpx.Response(404) if request.url.host == "a.example" else httpx.Response(503)

    install(monkeypatch, response, hosts=HOSTS[:2])
    monkeypatch.setattr(chain, "candidates", lambda *args, **kwargs: iter([clearance()]))
    with pytest.raises(mirrors.MirrorFetchError) as failure:
        mirrors.pick_and_get("demo", HOSTS[:2], "/download/1", ok=lambda r: False, protection=settings())
    assert failure.value.error_class != "gone"
    details = diagnostic_details(failure.value.record())
    assert "a.example" in details
    assert "b.example" in details
    assert "404" in details
    assert "503" in details


def test_provider_call_limit_is_shared_by_all_mirrors(monkeypatch):
    calls = []
    cfg = configured_settings(max_attempts=2)
    install(monkeypatch, lambda request: httpx.Response(403, text=CF), hosts=HOSTS, protection=cfg)
    monkeypatch.setattr(chain, "public_addresses", lambda *args: ["93.184.216.34"])

    def refused(provider, origin, timeout):
        calls.append((provider["id"], origin))
        raise ValueError("synthetic unresolved challenge")

    monkeypatch.setattr(chain, "adapters", lambda: {"flaresolverr": refused, "byparr": refused})
    with pytest.raises(mirrors.MirrorFetchError):
        mirrors.pick_and_get("demo", HOSTS, "/topic/1", protection=cfg)
    assert len(calls) == 2


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
def test_root_health_decodes_unicode_challenge_title(monkeypatch, encoding):
    install(
        monkeypatch,
        lambda request: httpx.Response(
            403,
            content="<title>Just a moment…</title>".encode(encoding),
            headers={"content-type": f"text/html; charset={encoding}"},
        ),
    )
    assert not mirrors._host_answers(HOSTS[0], cookies=None, ua=None, public_only=True)


def test_bounded_utf8_verdict_does_not_misdecode_a_split_final_character():
    from tow.protection import Verdict

    title = "<title>Just a moment…</title>".encode()
    body = title + b" " * (65535 - len(title)) + "Я".encode()
    response = httpx.Response(403, content=body, headers={"content-type": "text/html; charset=utf-8"})
    assert mirrors.response_verdict(response) == Verdict.CHALLENGE


def test_failed_new_attempt_keeps_previously_verified_clearance(monkeypatch):
    cfg = configured_settings(min_interval_sec=60, max_attempts=1)
    now = time.time()
    nonce = store.reserve_attempt("demo", HOSTS[0], cfg, now=now - 61)
    old = replace(clearance(), provider="first", attempt_id=nonce)
    store.accept("demo", HOSTS[0], cfg, old, now=now, require_attempt=True)
    assert chain.cached("demo", HOSTS[0], cfg).attempt_id == old.attempt_id
    monkeypatch.setattr(chain, "public_addresses", lambda *args: ["93.184.216.34"])

    def refused(*args):
        raise ValueError("synthetic challenge failure")

    monkeypatch.setattr(chain, "adapters", lambda: {"flaresolverr": refused})
    assert list(chain.candidates("demo", HOSTS[0], cfg, skip_cached=True)) == []
    retained = chain.cached("demo", HOSTS[0], cfg)
    assert retained == old
    assert retained.attempt_id == old.attempt_id


def _verified_access(cfg, *, legacy=False):
    now = time.time()
    nonce = store.reserve_attempt("demo", HOSTS[0], cfg, now=now - 61)
    value = replace(clearance(), provider="first", attempt_id=nonce)
    store.accept("demo", HOSTS[0], cfg, value, now=now, require_attempt=True)
    if legacy:
        import json

        path = data_dir() / "protection.enc"
        payload = json.loads(master_fernet().decrypt(path.read_bytes()))
        for record in payload["records"].values():
            record["clearance"].pop("attempt_id")
        atomic_write_bytes(path, master_fernet().encrypt(json.dumps(payload).encode()))
    return now, value


@pytest.mark.parametrize("legacy", [False, True])
def test_retained_clearance_identity_is_independent_of_new_reservation(legacy):
    cfg = configured_settings(min_interval_sec=60)
    now, old = _verified_access(cfg, legacy=legacy)
    pending = store.prepare_attempt("demo", HOSTS[0], cfg, now=now)
    assert pending.proof.nonce != old.attempt_id
    retained = store.read("demo", HOSTS[0], cfg, now=now)
    assert retained.attempt_id == old.attempt_id
    # The old replay can revoke only the old clearance, not the new reservation.
    store.discard("demo", HOSTS[0], expected=retained)
    new = replace(old, attempt_id=pending.proof.nonce)
    store.accept("demo", HOSTS[0], cfg, new, now=now, require_attempt=True)
    store.accept("demo", HOSTS[0], cfg, old, now=now, require_attempt=True)
    store.discard("demo", HOSTS[0], expected=old)
    assert store.read("demo", HOSTS[0], cfg, now=now).attempt_id == new.attempt_id
    store.finish_attempt(pending.proof, "second")
    assert store.eligible_providers("demo", HOSTS[0], cfg, cfg["providers"], now=now) == cfg["providers"]


def test_failed_chain_rotates_providers_even_with_retained_clearance():
    cfg = configured_settings(min_interval_sec=60)
    now, old = _verified_access(cfg)
    pending = store.prepare_attempt("demo", HOSTS[0], cfg, now=now)
    store.note_attempt(pending.proof, "first")
    store.finish_attempt(pending.proof, "first")
    assert store.read("demo", HOSTS[0], cfg, now=now).attempt_id == old.attempt_id
    eligible = store.eligible_providers("demo", HOSTS[0], cfg, cfg["providers"], now=now)
    assert [provider["id"] for provider in eligible] == ["second", "first"]


@pytest.mark.parametrize("change", ["settings", "expiry", "untagged"])
def test_reservation_does_not_preserve_unusable_or_untagged_access(change):
    cfg = configured_settings(min_interval_sec=60)
    now, _old = _verified_access(cfg)
    if change == "settings":
        cfg = {**cfg, "timeout_sec": cfg["timeout_sec"] + 1}
    elif change == "expiry":
        now += 7200
    else:
        import json

        path = data_dir() / "protection.enc"
        payload = json.loads(master_fernet().decrypt(path.read_bytes()))
        for record in payload["records"].values():
            record.pop("attempt_id")
            record["clearance"].pop("attempt_id")
        atomic_write_bytes(path, master_fernet().encrypt(json.dumps(payload).encode()))
    assert store.prepare_attempt("demo", HOSTS[0], cfg, now=now) is not None
    assert store.read("demo", HOSTS[0], cfg, now=now) is None


@pytest.mark.parametrize(
    "hosts",
    [HOSTS, ["https://A.example:443", "https://B.example", HOSTS[2]], ["https://ТЕСТ.example:443", HOSTS[1], HOSTS[2]]],
)
def test_mirrors_rotate_even_when_each_check_is_after_the_retry_interval(monkeypatch, hosts):
    from tow.protection.spec import target_origin

    clock = [time.time()]
    monkeypatch.setattr(chain.time, "time", lambda: clock[0])
    calls = []
    cfg = configured_settings(max_attempts=1, min_interval_sec=60)
    install(monkeypatch, lambda request: httpx.Response(403, text=CF), hosts=hosts, protection=cfg)
    monkeypatch.setattr(chain, "public_addresses", lambda *args: ["93.184.216.34"])

    def refused(provider, origin, timeout):
        calls.append(origin)
        raise ValueError("synthetic challenge failure")

    monkeypatch.setattr(chain, "adapters", lambda: {"flaresolverr": refused, "byparr": refused})
    for _ in hosts:
        with pytest.raises(mirrors.MirrorFetchError):
            mirrors.pick_and_get("demo", hosts, "/topic/1", protection=cfg, ignore_cool=True)
        clock[0] += 3601
    assert calls == [target_origin(host) for host in hosts]


def test_zero_shared_call_budget_never_reserves_or_contacts_any_service(monkeypatch):
    cfg = configured_settings()
    monkeypatch.setattr(store, "prepare_attempt", lambda *args, **kw: pytest.fail("zero budget must not reserve"))
    assert list(chain.candidates("demo", HOSTS[0], cfg, call_budget=RecoveryBudget(0))) == []


@pytest.mark.parametrize("stage", ["probe", "resource"])
@pytest.mark.parametrize("refusal", ["quota", "blocked"])
def test_quota_or_ban_stops_the_entire_recovery_phase(monkeypatch, stage, refusal):
    attempts = []

    def response(request):
        if "cf_clearance=" not in request.headers.get("cookie", ""):
            return httpx.Response(403, text=CF)
        if stage == "probe" or request.url.path != "/":
            if refusal == "blocked":
                return httpx.Response(403, text="<title>Cloudflare</title>Error code: 1020")
            return httpx.Response(200, text="Daily .torrent download limit reached")
        return httpx.Response(200, text="public page")

    install(monkeypatch, response, hosts=HOSTS[:2])

    def candidates(site, origin, *args, **kw):
        attempts.append(origin)
        yield clearance()

    monkeypatch.setattr(chain, "candidates", candidates)
    if refusal == "quota":
        real_verify = mirrors._verify_clearance

        def quota_probe(response, host, markers):
            if stage == "probe":
                raise mirrors.MirrorFetchError("mirrors.tracker_daily_limit", failure="quota", tracker="demo")
            return real_verify(response, host, markers)

        monkeypatch.setattr(mirrors, "_verify_clearance", quota_probe)
        monkeypatch.setattr(mirrors, "is_download_limit", lambda content: b"Daily .torrent" in content)
    with pytest.raises(mirrors.MirrorFetchError) as failure:
        mirrors.pick_and_get(
            "demo", HOSTS[:2], "/download/1", ok=lambda r: False, protection=settings(), download_limit=True
        )
    assert attempts == HOSTS[:1]
    if stage == "resource" and refusal == "quota":
        assert failure.value.failure == "quota"


@pytest.mark.parametrize("status", [404, 410])
def test_topic_is_gone_only_when_every_recovered_mirror_says_so(monkeypatch, status):
    def response(request):
        if "cf_clearance=" not in request.headers.get("cookie", ""):
            return httpx.Response(403, text=CF)
        return httpx.Response(200, text="public page") if request.url.path == "/" else httpx.Response(status)

    install(monkeypatch, response, hosts=HOSTS[:2])
    monkeypatch.setattr(chain, "candidates", lambda *args, **kw: iter([clearance()]))
    with pytest.raises(mirrors.MirrorFetchError) as failure:
        mirrors.pick_and_get("demo", HOSTS[:2], "/topic/1", protection=settings())
    assert failure.value.error_class == "gone"


def test_local_resource_validation_failure_is_not_reported_as_site_transport(monkeypatch):
    def response(request):
        return (
            httpx.Response(200, text="public page")
            if "cf_clearance=" in request.headers.get("cookie", "")
            else httpx.Response(403, text=CF)
        )

    install(monkeypatch, response, hosts=HOSTS[:1])
    monkeypatch.setattr(chain, "candidates", lambda *args, **kw: iter([clearance()]))

    def broken_validation(response):
        raise RuntimeError("synthetic local validation error")

    with pytest.raises(mirrors.MirrorFetchError) as failure:
        mirrors.pick_and_get("demo", HOSTS[:1], "/topic/1", ok=broken_validation, protection=settings())
    assert failure.value.code == "mirrors.local"
    assert failure.value.error_class == "error"


def test_unreadable_optional_store_does_not_replace_original_mirror_failure(monkeypatch):
    install(monkeypatch, lambda request: httpx.Response(403, text=CF), hosts=HOSTS[:1])
    path = data_dir() / "protection.enc"
    path.write_bytes(b"synthetic-invalid-encrypted-record")
    with pytest.raises(mirrors.MirrorFetchError) as failure:
        mirrors.pick_and_get("demo", HOSTS[:1], "/topic/1", protection=settings())
    assert failure.value.failure == "cloudflare"
    assert path.read_bytes() == b"synthetic-invalid-encrypted-record"


@pytest.mark.parametrize("mode", ["root", "topic", "path_only", "preview"])
def test_cached_access_is_discarded_only_after_an_applying_public_probe_refusal(monkeypatch, mode):
    cfg = configured_settings(min_interval_sec=60, max_attempts=1)
    _now, old = _verified_access(cfg)

    def response(request):
        if mode == "path_only" and request.url.path == "/":
            return httpx.Response(200, text="public page")
        return httpx.Response(403, text=CF)

    install(monkeypatch, response, hosts=HOSTS[:1], protection=cfg)
    monkeypatch.setattr(chain, "public_addresses", lambda *args: ["93.184.216.34"])

    def refused(*args):
        raise ValueError("synthetic challenge failure")

    monkeypatch.setattr(chain, "adapters", lambda: {"flaresolverr": refused})
    with pytest.raises(mirrors.MirrorFetchError):
        mirrors.pick_and_get(
            "demo",
            HOSTS[:1],
            "/" if mode in {"root", "preview"} else "/topic/1",
            protection=cfg,
            persist=mode != "preview",
        )
    retained = chain.cached("demo", HOSTS[0], cfg)
    if mode in {"root", "topic"}:
        assert retained is None
    else:
        assert retained.attempt_id == old.attempt_id


def test_refused_old_request_cannot_discard_a_new_clearance_with_identical_cookie(monkeypatch):
    cfg = configured_settings(min_interval_sec=60, max_attempts=1)
    _now, old = _verified_access(cfg)
    new = []

    def response(request):
        if request.url.path == "/topic/1" and not new:
            nonce = store.reserve_attempt("demo", HOSTS[0], cfg, now=time.time())
            replacement = replace(old, attempt_id=nonce)
            store.accept("demo", HOSTS[0], cfg, replacement, now=time.time(), require_attempt=True)
            new.append(replacement)
        return httpx.Response(403, text=CF)

    install(monkeypatch, response, hosts=HOSTS[:1], protection=cfg)
    monkeypatch.setattr(chain, "public_addresses", lambda *args: ["93.184.216.34"])
    with pytest.raises(mirrors.MirrorFetchError):
        mirrors.pick_and_get("demo", HOSTS[:1], "/topic/1", protection=cfg)
    retained = chain.cached("demo", HOSTS[0], cfg)
    assert retained.attempt_id == new[0].attempt_id
    assert retained.attempt_id != old.attempt_id
