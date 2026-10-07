"""GenericHttpTracker.fetch_magnet (the public magnet fallback) end to end.

Requests go through the real mirror picker (``pick_and_get``) and ``tow.http`` into an
``httpx.MockTransport``: nothing leaves the process.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from helpers import raises_code

from tow import http as thttp
from tow.mirrors import MirrorFetchError
from tow.trackers.generic import GenericHttpTracker

V1 = "31D3891A" + "0" * 32
V2 = "AB" * 32
TORRENT = b"d4:infod6:lengthi1e4:name1:x12:piece lengthi16384e6:pieces20:" + b"x" * 20 + b"ee"


class Site:
    """A tracker site: path -> response; records every request it gets."""

    def __init__(self, pages: dict[str, Any]) -> None:
        self.pages = pages
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = request.url.raw_path.decode()
        answer = self.pages.get(key)
        if callable(answer):
            answer = answer(request)
        if answer is None:
            return httpx.Response(404, text="not found")
        if isinstance(answer, httpx.Response):
            return answer
        if isinstance(answer, bytes):
            return httpx.Response(200, content=answer, headers={"content-type": "application/x-bittorrent"})
        return httpx.Response(200, text=answer, headers={"content-type": "text/html; charset=utf-8"})


@pytest.fixture
def site(monkeypatch):
    holder = Site({})
    transport = httpx.MockTransport(holder.handler)

    def client(ua=None, cookies=None, follow_redirects=True, *, public_only=False):
        return httpx.Client(
            transport=transport,
            headers={"User-Agent": ua or thttp.UA_DEFAULT},
            cookies=cookies or {},
            follow_redirects=follow_redirects,
        )

    monkeypatch.setattr(thttp, "client", client)
    return holder


def _tracker(**spec: Any) -> GenericHttpTracker:
    return GenericHttpTracker(
        "demo",
        {"url_regex": r"^https://demo\.example/(?:forum/viewtopic\.php\?t=|torrent/)(\d+)", **spec},
    )


def _page(*hrefs: str) -> str:
    links = "".join(f'<a href="{href}">magnet</a>' for href in hrefs)
    return f"<html><head><title>Show</title></head><body>{links}</body></html>"


def test_bad_topic_url_is_refused_before_any_request(site):
    with raises_code("tracker.bad_url", ValueError):
        _tracker(fetch_hosts=["https://demo.example"]).fetch_magnet("https://other.example/1", {}, None)
    assert site.requests == []


def test_magnet_from_the_configured_topic_path(site):
    site.pages["/forum/viewtopic.php?t=7"] = _page(f"magnet:?xt=urn:btih:{V1}&amp;dn=Show&amp;tr=udp://t.example:80")
    tracker = _tracker(fetch_hosts=["https://demo.example"], topic_path="/forum/viewtopic.php?t={id}")

    magnet, infohash = tracker.fetch_magnet("https://demo.example/torrent/7/slug", {}, "UA-1", persist=False)

    assert magnet == f"magnet:?xt=urn:btih:{V1}&dn=Show&tr=udp://t.example:80"
    assert infohash == V1
    assert [str(r.url) for r in site.requests] == ["https://demo.example/forum/viewtopic.php?t=7"]
    assert site.requests[0].headers["user-agent"] == "UA-1"


def test_without_topic_path_the_url_path_and_query_are_requested(site):
    site.pages["/forum/viewtopic.php?t=5"] = _page(f"magnet:?xt=urn:btmh:1220{V2}")
    tracker = _tracker(fetch_hosts=["https://mirror.example"])

    assert tracker.fetch_magnet("https://demo.example/forum/viewtopic.php?t=5", {}, None, persist=False) == (
        f"magnet:?xt=urn:btmh:1220{V2}",
        V2,
    )
    assert str(site.requests[0].url) == "https://mirror.example/forum/viewtopic.php?t=5"


def test_without_topic_path_a_url_without_query_keeps_its_path(site):
    site.pages["/torrent/9"] = _page(f"magnet:?xt=urn:btih:{V1}")
    tracker = _tracker(fetch_hosts=["https://demo.example"])
    assert tracker.fetch_magnet("https://demo.example/torrent/9", {}, None, persist=False)[1] == V1
    assert site.requests[0].url.raw_path == b"/torrent/9"


def test_a_url_with_an_empty_path_requests_the_root(site):
    site.pages["/?id=3"] = _page(f"magnet:?xt=urn:btih:{V1}")
    tracker = GenericHttpTracker("demo", {"url_regex": r"^https://demo\.example\?id=(\d+)$"})
    tracker.spec["fetch_hosts"] = ["https://demo.example"]
    assert tracker.fetch_magnet("https://demo.example?id=3", {}, None, persist=False)[1] == V1


def test_browser_user_agent_and_scoped_cookies_are_used_for_the_page(site):
    site.pages["/torrent/1"] = _page(f"magnet:?xt=urn:btih:{V1}")
    secrets = {
        "trackers": {
            "demo": {
                "browser_user_agent": "Edge/1",
                "cookies_by_origin": {"https://demo.example:443": {"sid": "s1"}},
            }
        }
    }
    _tracker(fetch_hosts=["https://demo.example"]).fetch_magnet("https://demo.example/torrent/1", secrets, "TOW")
    request = site.requests[0]
    assert request.headers["user-agent"] == "Edge/1"
    assert request.headers["cookie"] == "sid=s1"


@pytest.mark.parametrize(
    "body",
    [
        "<html>no links at all</html>",
        _page("magnet:?xt=urn:btih:not-a-hash"),
        _page("https://demo.example/download/1"),
    ],
)
def test_a_page_without_a_valid_magnet_is_an_error(site, body):
    site.pages["/torrent/2"] = body
    with raises_code("tracker.no_magnet", RuntimeError):
        _tracker(fetch_hosts=["https://demo.example"]).fetch_magnet(
            "https://demo.example/torrent/2", {}, None, persist=False
        )


def test_two_different_magnets_on_the_page_are_ambiguous(site):
    site.pages["/torrent/2"] = _page(f"magnet:?xt=urn:btih:{'11' * 20}", f"magnet:?xt=urn:btih:{'22' * 20}")
    with raises_code("tracker.magnet_ambiguous", RuntimeError):
        _tracker(fetch_hosts=["https://demo.example"]).fetch_magnet(
            "https://demo.example/torrent/2", {}, None, persist=False
        )


def test_the_same_magnet_twice_is_not_ambiguous(site):
    magnet = f"magnet:?xt=urn:btih:{V1}"
    site.pages["/torrent/2"] = _page(magnet, magnet + "&amp;dn=again")
    assert _tracker(fetch_hosts=["https://demo.example"]).fetch_magnet(
        "https://demo.example/torrent/2", {}, None, persist=False
    ) == (magnet, V1)


def test_a_missing_topic_page_is_a_mirror_error(site):
    with pytest.raises(MirrorFetchError) as error:
        _tracker(fetch_hosts=["https://demo.example"]).fetch_magnet(
            "https://demo.example/torrent/404", {}, None, persist=False
        )
    assert error.value.failure == "http_topic"


def test_torrent_link_fails_then_the_page_magnet_is_used(site):
    # The download link answers with an HTML page (no torrent): fetch_torrent fails, and the
    # same topic page still offers the magnet the check falls back to.
    site.pages["/download/4"] = "<html>please log in</html>"
    site.pages["/torrent/4"] = _page(f"magnet:?xt=urn:btih:{V1}")
    tracker = _tracker(fetch_hosts=["https://demo.example"])

    with pytest.raises(MirrorFetchError) as error:
        tracker.fetch_torrent("https://demo.example/torrent/4", {}, None, persist=False)
    assert error.value.failure == "auth"
    assert tracker.fetch_magnet("https://demo.example/torrent/4", {}, None, persist=False)[1] == V1


def test_page_without_download_link_and_daily_limit_are_reported(site, monkeypatch):
    site.pages["/forum/viewtopic.php?t=6"] = "<html>no link</html>"
    tracker = _tracker(fetch_hosts=["https://demo.example"], page_download=True)
    with raises_code("tracker.no_download_link", RuntimeError):
        tracker.fetch_torrent("https://demo.example/forum/viewtopic.php?t=6", {}, None, persist=False)

    # A response that passes the mirror check but is not a torrent: the daily limit, or not a torrent.
    responses = iter([b"limit page", b"garbage"])
    monkeypatch.setattr(
        tracker, "_get", lambda *_a, **_k: (httpx.Response(200, content=next(responses)), "https://demo.example")
    )
    monkeypatch.setattr("tow.trackers.generic.is_download_limit", lambda content: content == b"limit page")
    tracker.spec["page_download"] = False
    tracker.spec["download_limit"] = True  # only a site with a daily limit is asked about one
    with pytest.raises(RuntimeError) as limited:
        tracker.fetch_torrent("https://demo.example/torrent/6", {}, None, persist=False)
    assert limited.value.code == "mirrors.tracker_daily_limit"
    with raises_code("tracker.not_torrent", RuntimeError):
        tracker.fetch_torrent("https://demo.example/torrent/6", {}, None, persist=False)


def test_direct_download_uses_the_default_download_path(site):
    site.pages["/download/8"] = TORRENT
    tracker = _tracker(fetch_hosts=["https://demo.example"])
    assert tracker.fetch_torrent("https://demo.example/torrent/8", {}, None, persist=False) == TORRENT


# --- the auth retry in _get (used by the magnet fallback) -----------------------------------


def _login_site(site: Site, *, cookie_value: str = "fresh") -> None:
    def topic(request: httpx.Request) -> httpx.Response:
        if "sid=" + cookie_value in request.headers.get("cookie", ""):
            return httpx.Response(200, text=_page(f"magnet:?xt=urn:btih:{V1}"))
        return httpx.Response(401, text="login required")

    def login(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "/", "set-cookie": f"sid={cookie_value}; Path=/"})

    site.pages["/torrent/1"] = topic
    site.pages["/login"] = login


_LOGIN_SPEC = {"fetch_hosts": ["https://demo.example"], "login_hosts": ["https://demo.example"], "login_path": "/login"}
_CREDS = {"trackers": {"demo": {"username": "u", "password": "p"}}}


def test_an_expired_session_is_renewed_once_and_the_magnet_read(site):
    _login_site(site)
    tracker = _tracker(**_LOGIN_SPEC)

    magnet, infohash = tracker.fetch_magnet("https://demo.example/torrent/1", _CREDS, None)

    assert infohash == V1
    assert magnet == f"magnet:?xt=urn:btih:{V1}"
    assert [(r.method, r.url.path) for r in site.requests] == [
        ("GET", "/torrent/1"),
        ("POST", "/login"),
        ("GET", "/torrent/1"),
    ]


def test_a_login_that_yields_the_same_session_is_not_retried(site, monkeypatch):
    _login_site(site)
    tracker = _tracker(**_LOGIN_SPEC)
    monkeypatch.setattr(tracker, "_login", lambda *_a, **_k: {})
    with pytest.raises(MirrorFetchError) as error:
        tracker.fetch_magnet("https://demo.example/torrent/1", _CREDS, None)
    assert error.value.failure == "auth"
    assert len(site.requests) == 1


@pytest.mark.parametrize(
    ("spec", "secrets", "persist"),
    [
        (_LOGIN_SPEC, _CREDS, False),  # a preview never logs in
        ({**_LOGIN_SPEC, "browser_auth": True}, _CREDS, True),  # browser-only login
        (_LOGIN_SPEC, {}, True),  # no stored credentials
        ({"fetch_hosts": ["https://demo.example"]}, _CREDS, True),  # no login form configured
    ],
)
def test_auth_failure_is_not_retried_without_a_usable_login(site, monkeypatch, spec, secrets, persist):
    _login_site(site)
    tracker = _tracker(**spec)
    monkeypatch.setattr(tracker, "_login", lambda *_a, **_k: pytest.fail("login attempted"))
    with pytest.raises(MirrorFetchError) as error:
        tracker.fetch_magnet("https://demo.example/torrent/1", secrets, None, persist=persist)
    assert error.value.failure == "auth"


def test_a_non_auth_failure_is_never_retried_with_a_login(site, monkeypatch):
    site.pages["/torrent/1"] = lambda _r: httpx.Response(503, text="busy")
    tracker = _tracker(**_LOGIN_SPEC)
    monkeypatch.setattr(tracker, "_login", lambda *_a, **_k: pytest.fail("login attempted"))
    with pytest.raises(MirrorFetchError) as error:
        tracker.fetch_magnet("https://demo.example/torrent/1", _CREDS, None, persist=False)
    assert error.value.failure == "http"
