"""A password login never keeps a site-access clearance as part of the account's session.

Cloudflare's clearance belongs to the verified-access store (origin-, expiry- and
configuration-bound) and to the browser that earned it. A password login started with a
verified clearance must save only the account cookies its own answer set; a browser sign-in
(``tow.browser_auth``) keeps its cookies as they are.
"""

from __future__ import annotations

import time

import httpx
import pytest
from test_protection import SPEC, install, settings

from tow.config import load_config, save_config
from tow.protection import chain
from tow.protection.spec import Clearance
from tow.storage.secrets import encrypted_secrets_path
from tow.store import load_secrets, save_secrets
from tow.trackers.generic import GenericHttpTracker

ORIGIN = "https://site.example:443"
OPEN_SPEC = {**SPEC, "cookie_names": []}  # rutracker-like presets keep every account cookie


def _clearance() -> Clearance:
    return Clearance({"cf_clearance": "verified-token"}, "verified-agent", time.time() + 3600, "test")


def _secrets_file_bytes() -> bytes:
    return encrypted_secrets_path().read_bytes()


def _login_site(monkeypatch, answer, spec=OPEN_SPEC):
    """A site whose password POST gives ``answer``; its pages need the account session."""

    def handler(request):
        if request.method == "POST":
            return answer(request)
        return httpx.Response(200, text="<html>page</html>")

    calls, configured = install(monkeypatch, handler, protection=settings())
    configured.update(spec)
    cfg = load_config()
    cfg["trackers"] = {"demo": configured}
    save_config(cfg)
    monkeypatch.setattr(chain, "cached", lambda *a: _clearance())
    return calls, configured


def test_password_login_saves_only_account_cookies_set_by_its_answer(monkeypatch):
    calls, spec = _login_site(
        monkeypatch,
        lambda _request: httpx.Response(
            302,
            headers=[
                ("location", "/"),
                ("set-cookie", "bb_session=account; Path=/"),
                ("set-cookie", "__cf_bm=bot-score; Path=/"),
            ],
        ),
    )
    save_secrets({"trackers": {"demo": {"username": "u", "password": "p"}}})
    tracker = GenericHttpTracker("demo", spec)

    jar = tracker._login(load_secrets(), "ua", ignore_cool=True)

    assert "cf_clearance=verified-token" in calls[0].headers["cookie"]  # the POST itself passes the check
    assert jar == {ORIGIN: {"bb_session": "account"}}
    assert load_secrets()["trackers"]["demo"]["cookies_by_origin"] == {ORIGIN: {"bb_session": "account"}}


def test_named_cookies_never_take_the_clearance_either(monkeypatch):
    _calls, spec = _login_site(
        monkeypatch,
        lambda _request: httpx.Response(302, headers={"location": "/", "set-cookie": "session=account; Path=/"}),
        spec=SPEC,  # cookie_names: ["session"]
    )
    save_secrets({"trackers": {"demo": {"username": "u", "password": "p"}}})
    assert GenericHttpTracker("demo", spec)._login(load_secrets(), "ua", ignore_cool=True) == {
        ORIGIN: {"session": "account"}
    }
    assert load_secrets()["trackers"]["demo"]["cookies_by_origin"] == {ORIGIN: {"session": "account"}}


@pytest.mark.parametrize(
    "answer",
    [
        lambda _request: httpx.Response(302, headers={"location": "/"}),  # nothing set: the seed alone
        lambda _request: httpx.Response(302, headers={"location": "/", "set-cookie": "cf_clearance=other; Path=/"}),
        lambda _request: httpx.Response(200, text="<html>welcome</html>", headers={"set-cookie": "__cf_bm=x"}),
    ],
)
def test_clearance_alone_is_not_a_login_and_keeps_the_saved_session(monkeypatch, answer):
    _calls, spec = _login_site(monkeypatch, answer)
    save_secrets(
        {"trackers": {"demo": {"username": "u", "password": "p", "cookies_by_origin": {ORIGIN: {"bb_session": "old"}}}}}
    )
    before = _secrets_file_bytes()

    jar = GenericHttpTracker("demo", spec)._login(load_secrets(), "ua", ignore_cool=True)

    assert jar == {ORIGIN: {"bb_session": "old"}}
    assert _secrets_file_bytes() == before


def test_refused_login_keeps_the_saved_session(monkeypatch):
    _calls, spec = _login_site(
        monkeypatch,
        lambda _request: httpx.Response(
            200,
            text="<title>Login</title><input type=password name=password>",
            headers={"set-cookie": "bb_session=guest; Path=/"},
        ),
    )
    save_secrets(
        {"trackers": {"demo": {"username": "u", "password": "p", "cookies_by_origin": {ORIGIN: {"bb_session": "old"}}}}}
    )
    assert GenericHttpTracker("demo", spec)._login(load_secrets(), "ua", ignore_cool=True) == {
        ORIGIN: {"bb_session": "old"}
    }
    assert load_secrets()["trackers"]["demo"]["cookies_by_origin"] == {ORIGIN: {"bb_session": "old"}}


def test_preview_login_never_saves(monkeypatch):
    _calls, spec = _login_site(
        monkeypatch,
        lambda _request: httpx.Response(302, headers={"location": "/", "set-cookie": "bb_session=new; Path=/"}),
    )
    save_secrets({"trackers": {"demo": {"username": "u", "password": "p"}}})
    before = _secrets_file_bytes()
    assert GenericHttpTracker("demo", spec)._login(load_secrets(), "ua", persist=False) == {
        ORIGIN: {"bb_session": "new"}
    }
    assert _secrets_file_bytes() == before


CONTAMINATED = {
    "username": "u",
    "password": "p",
    "cookies_by_origin": {
        ORIGIN: {"bb_session": "account", "cf_clearance": "stale-token", "__cf_bm": "stale"},
        "https://mirror.example:443": {"cf_clearance": "stale-only"},
    },
}


def test_legacy_clearance_in_a_password_session_is_not_sent_and_not_rewritten_on_read():
    save_secrets({"trackers": {"demo": CONTAMINATED}})
    before = _secrets_file_bytes()
    tracker = GenericHttpTracker(
        "demo", {**OPEN_SPEC, "fetch_hosts": ["https://site.example", "https://mirror.example"]}
    )

    assert tracker._cookie_jar(load_secrets()) == {ORIGIN: {"bb_session": "account"}}
    assert _secrets_file_bytes() == before  # filtered in memory: a read never writes


def test_next_successful_password_login_cleans_legacy_clearance(monkeypatch):
    _calls, spec = _login_site(
        monkeypatch,
        lambda _request: httpx.Response(302, headers={"location": "/", "set-cookie": "bb_session=fresh; Path=/"}),
    )
    save_secrets({"trackers": {"demo": CONTAMINATED}})
    GenericHttpTracker("demo", spec)._login(load_secrets(), "ua", ignore_cool=True)
    saved = load_secrets()["trackers"]["demo"]
    assert saved["cookies_by_origin"] == {ORIGIN: {"bb_session": "fresh"}}
    assert saved["username"] == "u"


BROWSER_SESSION = {
    "username": "u",
    "password": "p",
    "browser_user_agent": "Mozilla/5.0 Browser/Test",
    "cookies_by_origin": {ORIGIN: {"session": "browser", "cf_clearance": "browser-token"}},
}


@pytest.mark.parametrize("browser_auth", [True, False])
def test_browser_sign_in_keeps_its_clearance(browser_auth):
    """A browser window's cookies and its user agent belong together: they are sent as saved."""
    save_secrets({"trackers": {"demo": BROWSER_SESSION}})
    tracker = GenericHttpTracker("demo", {**OPEN_SPEC, "browser_auth": browser_auth})
    assert tracker._cookie_jar(load_secrets()) == {ORIGIN: {"session": "browser", "cf_clearance": "browser-token"}}


def test_browser_auth_site_never_logs_in_or_rewrites_its_session(monkeypatch):
    _calls, spec = _login_site(monkeypatch, lambda _request: pytest.fail("a browser sign-in site got a password"))
    save_secrets({"trackers": {"demo": BROWSER_SESSION}})
    before = _secrets_file_bytes()
    tracker = GenericHttpTracker("demo", {**spec, "browser_auth": True})
    assert tracker._login(load_secrets(), "ua", ignore_cool=True) == {
        ORIGIN: {"session": "browser", "cf_clearance": "browser-token"}
    }
    assert _secrets_file_bytes() == before


def test_password_login_beside_a_browser_session_leaves_its_other_origins(monkeypatch):
    _calls, spec = _login_site(
        monkeypatch,
        lambda _request: httpx.Response(302, headers={"location": "/", "set-cookie": "bb_session=fresh; Path=/"}),
    )
    other = "https://mirror.example:443"
    entry = {**BROWSER_SESSION, "cookies_by_origin": {other: {"session": "browser", "cf_clearance": "browser-token"}}}
    save_secrets({"trackers": {"demo": entry}})
    GenericHttpTracker("demo", spec)._login(load_secrets(), "ua", ignore_cool=True)
    saved = load_secrets()["trackers"]["demo"]["cookies_by_origin"]
    assert saved[other] == {"session": "browser", "cf_clearance": "browser-token"}
    assert saved[ORIGIN] == {"bb_session": "fresh"}
