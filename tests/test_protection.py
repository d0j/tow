"""Site diagnostics and optional recovery through the real, guarded mirror flow."""

from __future__ import annotations

import time
from typing import Any

import httpx
import pytest
from helpers import make_torrent

from tow import http as thttp
from tow.config import load_config, save_config
from tow.mirrors import MirrorFetchError, _ordered, pick_and_get
from tow.protection import Verdict, chain, classify
from tow.protection.spec import Clearance
from tow.store import load_secrets, load_state, save_secrets
from tow.trackers.generic import GenericHttpTracker

CF = "<html><title>Just a moment...</title><body>check</body></html>"
LOGIN = "<html><title>Login</title><input type=password></html>"
HASH = "A" * 40
TORRENT = make_torrent({b"name": b"Show", b"length": 1, b"piece length": 16384, b"pieces": b"x" * 20})
SPEC = {
    "url_regex": r"^https://site\.example/topic/(\d+)",
    "fetch_hosts": ["https://site.example"],
    "login_hosts": ["https://site.example"],
    "download_path": "/download/{id}",
    "login_path": "/login",
    "cookie_names": ["session"],
}


@pytest.mark.parametrize(
    ("status", "text", "headers", "wanted", "verdict"),
    [
        (200, "Show", {}, False, Verdict.OK),
        (200, CF, {"CF-Mitigated": "challenge"}, False, Verdict.CHALLENGE),
        (403, CF, {}, False, Verdict.CHALLENGE),
        (503, CF, {}, False, Verdict.CHALLENGE),
        (200, CF, {}, False, Verdict.OK),
        (403, "<title>Attention Required! | Cloudflare</title>", {}, False, Verdict.INTERACTIVE),
        (403, "<title>Cloudflare</title>Error code: 1020", {}, False, Verdict.BLOCKED),
        (429, "<title>Cloudflare</title>Error 1015", {}, False, Verdict.RATE_LIMITED),
        (403, "Error 1015", {"server": "cloudflare"}, False, Verdict.RATE_LIMITED),
        (200, "<title>Cloudflare FAQ</title><pre>Error 1020</pre>", {"server": "cloudflare"}, False, Verdict.OK),
        (200, "<title>Technical works in progress</title>", {}, True, Verdict.MAINTENANCE),
        (200, "<title>Технические работы</title>", {}, True, Verdict.MAINTENANCE),
        (200, LOGIN, {}, True, Verdict.LOGIN_NEEDED),
        (401, "", {}, False, Verdict.LOGIN_NEEDED),
        (200, "<html>unexpected gateway page</html>", {}, True, Verdict.UNEXPECTED),
        (
            200,
            "<title>Show</title><script src='/cdn-cgi/challenge-platform/script.js'></script>",
            {},
            False,
            Verdict.OK,
        ),
    ],
)
def test_exact_response_classification(status, text, headers, wanted, verdict):
    assert classify(status, text, headers, torrent_wanted=wanted) == verdict


@pytest.mark.parametrize("status", range(520, 527))
def test_origin_errors_are_not_login_or_challenge(status):
    assert classify(status, CF, {"server": "cloudflare"}) == Verdict.ORIGIN_DOWN


def test_quota_evidence_is_distinct_and_a_long_post_does_not_declare_maintenance():
    assert classify(200, "quota", daily_limit=True, torrent_wanted=True) == Verdict.DAILY_LIMIT
    assert classify(200, "<title>Show</title>" + "post " * 1000 + "technical works in progress") == Verdict.OK
    assert classify(200, "<title>Site work</title>", maintenance_markers=["site work"]) == Verdict.MAINTENANCE


NEWS = "<div class=news>" + "Новые раздачи за неделю. " * 200 + "</div>"  # a long page: > 2000 characters


@pytest.mark.parametrize(
    "form",
    [
        '<form action="login.php"><input name="password" type="password"></form>',
        "<form><INPUT\n  type = 'PASSWORD' name=pass></form>",
        '<input data-hint="a > b" type="password">',  # a quoted ">" does not end the tag
        '<input value="' + "x" * 2000 + '" type=password />',
    ],
    ids=["plain", "case_and_spaces", "quoted_gt", "long_attribute"],
)
def test_a_long_page_with_a_password_field_answering_a_download_asks_to_sign_in(form):
    page = "<title>Tracker</title>" + NEWS + form
    assert classify(200, page, torrent_wanted=True) == Verdict.LOGIN_NEEDED
    assert classify(200, page) == Verdict.OK  # a topic page keeps the stricter short-page rule


@pytest.mark.parametrize(
    "decoy",
    [
        "<!-- <input type=password> -->",
        "<script>var form = '<input type=\"password\">';</script>",
        "<p>type=password</p>",
        '<input type="text" name="password">',
        "<p>" + "x" * 70000 + "</p><input type=password>",  # beyond the bounded scan
    ],
    ids=["comment", "script", "text", "text_input", "beyond_scan"],
)
def test_no_real_password_field_on_a_long_download_answer_is_not_a_sign_in(decoy):
    assert classify(200, "<title>Tracker</title>" + NEWS + decoy, torrent_wanted=True) == Verdict.UNEXPECTED


@pytest.mark.parametrize("fragment", ["<input ", "<input type=", "<input type='", "<input\t\n"])
def test_repeated_broken_input_fragments_are_read_quickly(fragment):
    """A regex re-read the rest of the page from every unended ``<input`` (quadratic in 64 KiB)."""
    page = fragment * (65536 // len(fragment))
    started = time.perf_counter()
    assert classify(200, page, torrent_wanted=True) in {Verdict.UNEXPECTED, Verdict.LOGIN_NEEDED}
    assert time.perf_counter() - started < 1.0


def test_removed_topic_and_daily_limit_keep_precedence_over_a_long_sign_in_page(monkeypatch):
    form = '<input type="password" name="password">'
    removed = "<title>Tracker</title>" + NEWS + "<h1>Тема не найдена</h1>" + form
    assert classify(200, removed, torrent_wanted=True, topic_removed=True) == Verdict.UNEXPECTED
    assert classify(200, NEWS + form, torrent_wanted=True, daily_limit=True) == Verdict.DAILY_LIMIT
    install(monkeypatch, lambda request: httpx.Response(200, text=removed))
    with pytest.raises(MirrorFetchError) as gone:
        pick_and_get("demo", SPEC["fetch_hosts"], "/download/1", ok=lambda r: False, persist=False)
    assert gone.value.failure == "gone"


def install(monkeypatch, handler, *, protection=None, hosts=None):
    cfg = load_config()
    spec = {**SPEC, "fetch_hosts": hosts or SPEC["fetch_hosts"]}
    cfg["trackers"] = {"demo": spec}
    if protection is not None:
        cfg["protection"] = protection
    save_config(cfg)
    calls = []

    def tracked(request):
        calls.append(request)
        return handler(request)

    transport = httpx.MockTransport(tracked)
    monkeypatch.setattr(
        thttp,
        "client",
        lambda **kw: httpx.Client(
            transport=transport,
            cookies=kw.get("cookies"),
            headers={"User-Agent": kw.get("ua") or "ordinary"},
            follow_redirects=False,
        ),
    )
    return calls, spec


def settings():
    return {
        "enabled": True,
        "providers": [{"id": "test", "kind": "flaresolverr", "url": "http://127.0.0.1:8191", "enabled": True}],
    }


def clearance():
    return Clearance({"cf_clearance": "test-clearance"}, "verified-agent", time.time() + 3600, "test")


def test_working_regular_mirror_wins_without_solver(monkeypatch):
    calls, _ = install(
        monkeypatch,
        lambda r: (
            httpx.Response(403, text=CF, headers={"cf-mitigated": "challenge"})
            if r.url.host == "site.example"
            else httpx.Response(200, content=TORRENT)
        ),
        hosts=["https://site.example", "https://free.example"],
    )
    monkeypatch.setattr(chain, "candidates", lambda *a, **kw: pytest.fail("a working mirror must win first"))
    answer, host = pick_and_get(
        "demo",
        ["https://site.example", "https://free.example"],
        "/download/1",
        ok=lambda r: r.content == TORRENT,
        protection=settings(),
    )
    assert host == "https://free.example"
    assert answer.content == TORRENT
    assert calls[0].url.host == "site.example"


@pytest.mark.parametrize(
    ("persist", "status", "body", "headers"),
    [
        (False, 403, CF, {"cf-mitigated": "challenge"}),
        (True, 403, "<title>Cloudflare</title>Error 1020", {"server": "cloudflare"}),
        (True, 429, "slow down", {}),
        (True, 520, "origin", {}),
        (True, 200, "<title>Технические работы</title>", {}),
        (True, 200, LOGIN, {}),
        (True, 200, "Вы скачали сегодня торрент-файл", {}),
    ],
)
def test_provider_never_runs_on_preview_or_nonchallenge(monkeypatch, persist, status, body, headers):
    install(monkeypatch, lambda r: httpx.Response(status, text=body, headers=headers))
    monkeypatch.setattr(chain, "candidates", lambda *a, **kw: pytest.fail("not a solver request"))
    with pytest.raises(MirrorFetchError):
        pick_and_get(
            "demo",
            SPEC["fetch_hosts"],
            "/download/1",
            ok=lambda r: False,
            protection=settings(),
            persist=persist,
            download_limit=True,
        )


def test_verified_clearance_root_then_normal_request_with_scoped_credentials(monkeypatch):
    accepted = []

    def handler(r):
        if "cf_clearance=test-clearance" not in r.headers.get("cookie", ""):
            return httpx.Response(403, text=CF, headers={"cf-mitigated": "challenge"})
        assert r.headers["user-agent"] == "verified-agent"
        return httpx.Response(200, text="front page") if r.url.path == "/" else httpx.Response(200, content=TORRENT)

    calls, _ = install(monkeypatch, handler)
    monkeypatch.setattr(chain, "candidates", lambda *a, **kw: iter([clearance()]))
    monkeypatch.setattr(chain, "accept", lambda *a: accepted.append(a))
    answer, host = pick_and_get(
        "demo", SPEC["fetch_hosts"], "/download/1", ok=lambda r: r.content == TORRENT, protection=settings()
    )
    assert answer.content == TORRENT
    assert host == SPEC["fetch_hosts"][0]
    assert [r.url.path for r in calls if "cf_clearance=" in r.headers.get("cookie", "")] == ["/", "/download/1"]
    assert len(accepted) == 1


def test_cross_origin_root_never_accepts_candidate(monkeypatch):
    install(
        monkeypatch,
        lambda r: (
            httpx.Response(302, headers={"location": "https://other.example/"})
            if "cf_clearance=" in r.headers.get("cookie", "")
            else httpx.Response(403, text=CF)
        ),
    )
    monkeypatch.setattr(chain, "candidates", lambda *a, **kw: iter([clearance()]))
    monkeypatch.setattr(chain, "accept", lambda *a: pytest.fail("unverified origin"))
    with pytest.raises(MirrorFetchError):
        pick_and_get("demo", SPEC["fetch_hosts"], "/topic/1", protection=settings())


@pytest.mark.parametrize("status", [200, 401, 403])
@pytest.mark.parametrize("title", ["Login", "Вход :: Example Site", "Example account"])
def test_signin_root_status_does_not_change_verified_recovery(monkeypatch, status, title):
    def handler(request):
        if "cf_clearance=" not in request.headers.get("cookie", ""):
            return httpx.Response(403, text=CF)
        login_page = f"<title>{title}</title><input type=password>"
        return (
            httpx.Response(status, text=login_page) if request.url.path == "/" else httpx.Response(200, content=TORRENT)
        )

    calls, _ = install(monkeypatch, handler)
    monkeypatch.setattr(chain, "candidates", lambda *a, **kw: iter([clearance()]))
    answer, _ = pick_and_get(
        "demo", SPEC["fetch_hosts"], "/download/1", ok=lambda r: r.content == TORRENT, protection=settings()
    )
    assert answer.content == TORRENT
    assert [request.url.path for request in calls if "cf_clearance=" in request.headers.get("cookie", "")] == [
        "/",
        "/download/1",
    ]


@pytest.mark.parametrize(
    ("status", "body", "failure", "error_class"),
    [
        (404, "", "http_topic", "gone"),
        (410, "", "http_topic", "gone"),
        (200, "torrent not found", "gone", "gone"),
        (200, "<title>Технические работы</title>", "maintenance", "tracker"),
        (200, "unexpected gateway", "unexpected", "not_torrent"),
        (429, "slow down", "rate_limited", "tracker"),
    ],
)
def test_topic_error_after_verified_root_keeps_access_and_actual_reason(
    monkeypatch, status, body, failure, error_class
):
    def handler(request):
        if "cf_clearance=" not in request.headers.get("cookie", ""):
            return httpx.Response(403, text=CF)
        return httpx.Response(200, text="front page") if request.url.path == "/" else httpx.Response(status, text=body)

    calls, _ = install(monkeypatch, handler)
    yielded = []

    def candidates(*args, **kwargs):
        yielded.append(1)
        yield clearance()
        pytest.fail("a topic error must not spend another solve attempt")

    monkeypatch.setattr(chain, "candidates", candidates)
    monkeypatch.setattr(chain, "discard", lambda *a, **kw: pytest.fail("independently verified access must be kept"))
    with pytest.raises(MirrorFetchError) as result:
        pick_and_get("demo", SPEC["fetch_hosts"], "/download/1", ok=lambda r: False, protection=settings())
    assert result.value.failure == failure
    assert result.value.error_class == error_class
    assert yielded == [1]
    assert (
        len(
            [
                request
                for request in calls
                if request.url.path == "/download/1" and "cf_clearance=" in request.headers.get("cookie", "")
            ]
        )
        == 1
    )
    assert load_state()["mirrors"]["demo"]["observed"][SPEC["fetch_hosts"][0]]["failure"] == failure


def test_topic_transport_error_after_verified_root_keeps_access(monkeypatch):
    def handler(request):
        if "cf_clearance=" not in request.headers.get("cookie", ""):
            return httpx.Response(403, text=CF)
        if request.url.path == "/":
            return httpx.Response(200, text="front page")
        raise httpx.ReadTimeout("synthetic transport failure")

    install(monkeypatch, handler)
    monkeypatch.setattr(chain, "candidates", lambda *a, **kw: iter([clearance()]))
    monkeypatch.setattr(chain, "discard", lambda *a, **kw: pytest.fail("not a clearance refusal"))
    with pytest.raises(MirrorFetchError) as result:
        pick_and_get("demo", SPEC["fetch_hosts"], "/download/1", ok=lambda r: False, protection=settings())
    assert result.value.failure == "transport"


@pytest.mark.parametrize("failure", [httpx.ReadTimeout("synthetic"), httpx.ConnectError("synthetic")])
def test_root_transport_error_keeps_cached_access_and_never_requests_another_solver(monkeypatch, failure):
    def handler(request):
        if request.url.path == "/" and "cf_clearance=" in request.headers.get("cookie", ""):
            raise failure
        return httpx.Response(403, text=CF)

    install(monkeypatch, handler)
    monkeypatch.setattr(chain, "discard", lambda *a, **kw: pytest.fail("transport cannot revoke access"))

    def candidates(*a, **kw):
        yield clearance()
        pytest.fail("transport cannot justify another solve attempt")

    monkeypatch.setattr(chain, "candidates", candidates)
    with pytest.raises(MirrorFetchError) as result:
        pick_and_get("demo", SPEC["fetch_hosts"], "/topic/1", protection=settings())
    assert result.value.code == "mirrors.all_failed"
    assert result.value.failure == "cloudflare"  # the real resource, not an optional probe's timeout


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504, 522])
def test_root_outage_does_not_revoke_access_or_consume_another_solver(monkeypatch, status):
    def handler(request):
        if request.url.path == "/" and "cf_clearance=" in request.headers.get("cookie", ""):
            return httpx.Response(status, text="site outage")
        return httpx.Response(403, text=CF)

    install(monkeypatch, handler)
    monkeypatch.setattr(chain, "discard", lambda *a, **kw: pytest.fail("outage cannot revoke access"))

    def candidates(*a, **kw):
        yield clearance()
        pytest.fail("outage cannot justify another solve attempt")

    monkeypatch.setattr(chain, "candidates", candidates)
    with pytest.raises(MirrorFetchError):
        pick_and_get("demo", SPEC["fetch_hosts"], "/topic/1", protection=settings())


def test_verified_root_is_retained_when_no_time_is_left_for_the_resource(monkeypatch):
    accepted = []
    clock = [100.0]
    monkeypatch.setattr("tow.mirrors.time.monotonic", lambda: clock[0])
    real_verify = __import__("tow.mirrors", fromlist=["_verify_clearance"])._verify_clearance

    def verify(*args):
        real_verify(*args)
        clock[0] = 130.0

    def handler(request):
        if request.url.path == "/" and "cf_clearance=" in request.headers.get("cookie", ""):
            return httpx.Response(200, text="front page")
        return httpx.Response(403, text=CF)

    install(monkeypatch, handler)
    monkeypatch.setattr("tow.mirrors._verify_clearance", verify)
    monkeypatch.setattr(chain, "candidates", lambda *a, **kw: iter([clearance()]))
    monkeypatch.setattr(chain, "accept", lambda *a: accepted.append(a))
    with pytest.raises(MirrorFetchError):
        pick_and_get("demo", SPEC["fetch_hosts"], "/topic/1", protection=settings())
    assert len(accepted) == 1


def test_long_topic_maintenance_title_is_not_a_site_outage():
    assert classify(200, "<title>Under Maintenance</title><div>" + "film description " * 1500 + "</div>") == Verdict.OK
    assert classify(200, "<title>Фильм: Технические работы — обсуждение</title><div>описание</div>") == Verdict.OK


def test_unsupported_deadline_transport_never_spends_provider_attempt(monkeypatch):
    install(monkeypatch, lambda r: httpx.Response(403, text=CF))
    monkeypatch.setattr("tow.mirrors._recovery_supported", lambda *a: False)
    monkeypatch.setattr(chain, "candidates", lambda *a, **kw: pytest.fail("cannot verify this transport"))
    with pytest.raises(MirrorFetchError):
        pick_and_get("demo", SPEC["fetch_hosts"], "/topic/1", protection=settings())


def test_root_only_clearance_does_not_clear_topic_failure_count(monkeypatch):
    events = []
    monkeypatch.setattr(chain, "record_event", lambda *a, **kw: events.append(a))

    def handler(request):
        if request.url.path == "/" and "cf_clearance=" in request.headers.get("cookie", ""):
            return httpx.Response(200, text="front page")
        return httpx.Response(403, text=CF)

    install(monkeypatch, handler)
    monkeypatch.setattr(chain, "candidates", lambda *a, **kw: iter([clearance()]))
    with pytest.raises(MirrorFetchError):
        pick_and_get("demo", SPEC["fetch_hosts"], "/topic/1", protection=settings())
    bucket = load_state()["mirrors"]["demo"]
    assert bucket["fail"][SPEC["fetch_hosts"][0]] == 1
    assert bucket.get("active") is None
    assert events == [("protection_replay_refused", "demo", "test", "failed")]


def test_recent_challenge_prefers_other_mirrors_but_expired_evidence_does_not(monkeypatch):
    monkeypatch.setattr("tow.mirrors._now", lambda: 10_000)
    observed = {"https://a": {"failure": "cloudflare", "at": 9999}}
    assert _ordered(["https://a", "https://b"], "https://a", observed) == ["https://b", "https://a"]
    observed["https://a"]["at"] = 1
    assert _ordered(["https://a", "https://b"], "https://a", observed) == ["https://a", "https://b"]


def test_mixed_mirror_failures_keep_each_real_reason_and_not_auth(monkeypatch):
    install(
        monkeypatch,
        lambda r: (
            httpx.Response(403, text=CF)
            if r.url.host == "site.example"
            else httpx.Response(200, text="<title>Технические работы</title>")
        ),
        hosts=["https://site.example", "https://free.example"],
    )
    with pytest.raises(MirrorFetchError) as result:
        pick_and_get("demo", ["https://site.example", "https://free.example"], "/download/1", ok=lambda r: False)
    assert result.value.failure == "maintenance"
    from tow.errors import diagnostic_details

    evidence = diagnostic_details(result.value.record())
    assert "site.example" in evidence
    assert "free.example" in evidence
    assert "site.example" not in str(result.value)
    assert "free.example" not in str(result.value)
    observed = load_state()["mirrors"]["demo"]["observed"]
    assert observed["https://site.example"]["failure"] == "cloudflare"
    assert observed["https://free.example"]["failure"] == "maintenance"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (f"<li>Инфо хеш: {HASH}</li>", HASH),
        (f"<li>Инфо хэш: <b>{HASH.lower()}</b></li>", HASH),
        (f"<li>Info hash: {HASH}</li>", HASH),
        (f"Инфо хеш: {HASH} Инфо хеш: {'B' * 40}", None),
        (f"Инфо хеш: {HASH}1", None),
        (f"<div class=post>Инфо хеш: {HASH}</div>", None),
        ("<html>unrecognised service page</html>", None),
    ],
)
def test_revision_service_decodes_and_requires_one_labelled_hash(monkeypatch, body, expected):
    calls, spec = install(
        monkeypatch,
        lambda r: httpx.Response(
            200, content=body.encode("cp1251"), headers={"content-type": "text/html; charset=windows-1251"}
        ),
    )
    tracker = GenericHttpTracker("demo", {**spec, "infohash_path": "/revision?id={id}"})
    assert tracker.fetch_revision_hash("https://site.example/topic/1234567", {}, None, persist=False) == expected
    assert [r.url.path for r in calls] == ["/revision"]


@pytest.mark.parametrize("manual", [False, True])
def test_new_verified_clearance_permits_one_retry_of_challenged_password_post(monkeypatch, manual):
    cache: dict[str, Any] = {}
    attempts = []

    def handler(r):
        cleared = "cf_clearance=test-clearance" in r.headers.get("cookie", "")
        if r.method == "POST":
            attempts.append(cleared)
            if not cleared:
                return httpx.Response(403, text=CF, headers={"cf-mitigated": "challenge"})
            return httpx.Response(302, headers={"location": "/", "set-cookie": "session=fresh; Path=/"})
        if not cleared:
            return httpx.Response(403, text=CF, headers={"cf-mitigated": "challenge"})
        if r.url.path == "/":
            return httpx.Response(302, headers={"location": "/login"})
        return (
            httpx.Response(200, content=TORRENT)
            if "session=fresh" in r.headers.get("cookie", "")
            else httpx.Response(200, text=LOGIN)
        )

    _calls, spec = install(monkeypatch, handler, protection=settings())
    monkeypatch.setattr(chain, "candidates", lambda *a, **kw: iter([clearance()]))
    monkeypatch.setattr(chain, "cached", lambda *a: cache.get("value"))
    monkeypatch.setattr(chain, "accept", lambda *a: cache.update(value=a[-1]))
    save_secrets({"trackers": {"demo": {"username": "test", "password": "test-password"}}})
    tracker = GenericHttpTracker("demo", spec)
    assert tracker.fetch_torrent("https://site.example/topic/1", load_secrets(), None, ignore_cool=manual) == TORRENT
    assert attempts == [False, True]
    assert tracker.fetch_torrent("https://site.example/topic/2", load_secrets(), None, ignore_cool=manual) == TORRENT
    assert attempts == [False, True]
