import gzip
from typing import ClassVar

import httpx
import pytest
from helpers import raises_code

from tow import http as thttp
from tow.config import load_config
from tow.mirrors import MirrorFetchError
from tow.store import load_state
from tow.trackers import load_trackers, match_tracker
from tow.trackers.generic import GenericHttpTracker, magnet_infohash


def test_login_does_not_follow_credential_redirect(monkeypatch):
    seen = {}

    class Client:
        cookies: ClassVar = {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def request(self, method, url, data):
            seen.update({"method": method, "url": url, "data": data})
            return httpx.Response(302)

    def fake_client(**kwargs):
        seen["follow_redirects"] = kwargs["follow_redirects"]
        return Client()

    monkeypatch.setattr(thttp, "client", fake_client)
    tr = GenericHttpTracker(
        "demo",
        {"url_regex": r"^https://demo/(\\d+)$", "login_hosts": ["https://demo"], "login_path": "/login"},
    )

    tr._login({"trackers": {"demo": {"username": "u", "password": "p"}}}, "ua", persist=False)

    assert seen["follow_redirects"] is False
    assert seen["method"] == "POST"
    assert seen["url"] == "https://demo/login"


def test_login_keeps_all_response_cookies_when_cookie_names_are_unspecified(monkeypatch):
    class Client:
        cookies: ClassVar = {"bb_session": "session-value", "bb_userid": "user-value"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def request(self, _method, _url, data):
            assert data == {"username": "u", "password": "p"}
            return httpx.Response(302)

    monkeypatch.setattr(thttp, "client", lambda **_kwargs: Client())
    tr = GenericHttpTracker(
        "demo",
        {
            "url_regex": r"^https://demo/(\\d+)$",
            "login_hosts": ["https://demo"],
            "login_path": "/login",
            "cookie_names": [],
        },
    )

    cookies = tr._login({"trackers": {"demo": {"username": "u", "password": "p"}}}, "ua", persist=False)

    assert cookies == {"https://demo:443": {"bb_session": "session-value", "bb_userid": "user-value"}}


def test_browser_auth_tracker_never_posts_stored_password(monkeypatch):
    monkeypatch.setattr(thttp, "client", lambda **_kwargs: (_ for _ in ()).throw(AssertionError("HTTP login used")))
    tracker = GenericHttpTracker(
        "nnmclub",
        {
            "url_regex": r"^https://nnmclub\.to/forum/viewtopic\.php\?t=(\d+)$",
            "browser_auth": True,
            "login_hosts": ["https://nnmclub.to"],
            "login_path": "/forum/login.php",
        },
    )

    cookies = tracker._login(
        {"trackers": {"nnmclub": {"username": "stored", "password": "stored"}}},
        "ua",
        persist=False,
    )

    assert cookies == {}


def test_kinozal_and_rutor_ids():
    tr = load_trackers(load_config())
    kz = tr["kinozal"]
    assert kz.parse_id("https://kinozal.guru/details.php?id=3456789") == "3456789"
    assert kz.parse_id("https://kinozal.jumpingcrab.com/details.php?id=1") == "1"
    assert kz.parse_id("https://kinozal.me/details.php?id=2") == "2"
    ru = tr["rutor"]
    assert ru.parse_id("https://rutor.info/torrent/1234567/slug") == "1234567"
    assert ru.parse_id("https://new-rutor.org/torrent/1/x") == "1"
    assert ru.parse_id("https://d.rutor.info/download/1234568") == "1234568"
    assert ru.parse_id("http://rutor.info/download/1234568") == "1234568"
    assert tr["nnmclub"].parse_id("https://nnmclub.to/forum/viewtopic.php?t=12") == "12"
    assert tr["rutracker"].parse_id("https://rutracker.org/forum/viewtopic.php?t=9") == "9"
    assert match_tracker(tr, "https://example.com/x") is None


def test_rutor_direct_download_host_is_explicit_and_preferred():
    rutor = load_config()["trackers"]["rutor"]
    assert rutor["fetch_hosts"][0] == "http://d.rutor.info"
    assert "http://d.rutor.info" in rutor["fetch_hosts"]


def test_nnmclub_download_redirect_host_is_explicitly_allowlisted():
    nnmclub = load_config()["trackers"]["nnmclub"]
    assert nnmclub["download_redirect_hosts"] == ["https://bulk.nnmclub.to"]


def test_page_download_id():
    tr = GenericHttpTracker(
        "nnmclub",
        {
            "url_regex": r"^https://nnmclub\.to/forum/viewtopic\.php\?t=(\d+)$",
            "download_href_regex": r"download\.php\?id=(\d+)",
        },
    )
    html = '<a href="download.php?id=99">torrent</a>'
    assert tr._page_download_id(html) == "99"


def test_page_magnet_is_validated_and_decodes_html_entities():
    tracker = GenericHttpTracker("demo", {"url_regex": r"^https://demo/(\d+)$"})
    infohash = "31D3891A" + "0" * 32

    magnet, parsed_hash = tracker._page_magnet(f'<a href="magnet:?xt=urn:btih:{infohash}&amp;dn=Show">magnet</a>')

    assert magnet.endswith("&dn=Show")
    assert parsed_hash == infohash
    assert magnet_infohash("https://tracker.example/file") is None


def test_page_magnet_supports_btmh_and_rejects_ambiguous_links():
    tracker = GenericHttpTracker("demo", {"url_regex": r"^https://demo/(\d+)$"})
    v2_hash = "AB" * 32
    magnet = f"magnet:?xt=urn:btmh:1220{v2_hash}&dn=Show"

    assert tracker._page_magnet(f'<a href="{magnet}">v2</a>') == (magnet, v2_hash)

    with raises_code("tracker.magnet_ambiguous", RuntimeError):
        tracker._page_magnet(
            '<a href="magnet:?xt=urn:btih:'
            + "11" * 20
            + '">one</a><a href="magnet:?xt=urn:btih:'
            + "22" * 20
            + '">two</a>'
        )


def test_magnet_rejects_conflicting_exact_topics_of_same_hash_kind():
    magnet = "magnet:?xt=urn:btih:" + "11" * 20 + "&xt=urn:btih:" + "22" * 20
    assert magnet_infohash(magnet) is None


def test_cookie_jar_requires_origin_scope_for_multi_origin_tracker():
    tr = GenericHttpTracker(
        "demo",
        {
            "url_regex": r"^https://demo/(\\d+)$",
            "fetch_hosts": ["https://a.example", "https://b.example"],
            "cookie_names": ["uid", "pass"],
        },
    )

    assert tr._cookie_jar({"trackers": {"demo": {"uid": "legacy"}}}) == {}
    assert tr._cookie_jar(
        {
            "trackers": {
                "demo": {
                    "cookies_by_origin": {
                        "https://a.example:443": {"uid": "a"},
                        "https://b.example:443": {"uid": "b"},
                    }
                }
            }
        }
    ) == {
        "https://a.example:443": {"uid": "a"},
        "https://b.example:443": {"uid": "b"},
    }


def test_cookie_jar_never_sends_scoped_store_or_metadata_as_legacy_cookies():
    tracker = GenericHttpTracker(
        "demo",
        {"url_regex": r"^https://b\.example/(\d+)$", "fetch_hosts": ["https://b.example"], "cookie_names": []},
    )
    cookies = tracker._cookie_jar(
        {
            "trackers": {
                "demo": {
                    "cookies_by_origin": {
                        "https://a.example:443": {"sid": "A_ONLY"},
                        "https://b.example:443": {"sid": "B_ONLY"},
                    },
                    "browser_user_agent": "UA",
                    "sid": "OLD_LEGACY",
                }
            }
        }
    )
    assert cookies["https://b.example:443"] == {"sid": "B_ONLY"}
    header = (
        httpx.Client(cookies=cookies["https://b.example:443"])
        .build_request("GET", "https://b.example/1")
        .headers.get("cookie", "")
    )
    assert "A_ONLY" not in header
    assert "OLD_LEGACY" not in header
    assert "browser_user_agent" not in header
    assert "cookies_by_origin" not in header


def test_unscoped_legacy_cookie_is_not_assigned_to_new_single_mirror():
    tracker = GenericHttpTracker(
        "demo",
        {
            "url_regex": r"^https://new\.example/(\d+)$",
            "fetch_hosts": ["https://new.example"],
            "cookie_names": [],
        },
    )
    assert tracker._cookie_jar({"trackers": {"demo": {"sid": "old-domain-session"}}}) == {}


def test_login_collects_sessions_for_each_configured_origin(monkeypatch):
    posts = []

    class Client:
        def __init__(self):
            self.cookies = {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def request(self, _method, url, data):
            posts.append(url)
            self.cookies = {"sid": url.split("/")[2]}
            return httpx.Response(200)

    monkeypatch.setattr(thttp, "client", lambda **_kwargs: Client())
    tracker = GenericHttpTracker(
        "demo",
        {
            "url_regex": r"^https://a\.example/(\d+)$",
            "login_hosts": ["https://a.example", "https://b.example"],
            "login_path": "/login",
        },
    )
    cookies = tracker._login({"trackers": {"demo": {"username": "u", "password": "p"}}}, None, persist=False)
    assert posts == ["https://a.example/login", "https://b.example/login"]
    assert cookies == {
        "https://a.example:443": {"sid": "a.example"},
        "https://b.example:443": {"sid": "b.example"},
    }


def test_deleted_site_cannot_persist_late_login_session(monkeypatch, tmp_path):
    monkeypatch.setenv("TOW_HOME", str(tmp_path))
    monkeypatch.setattr("tow.config.load_config", lambda: {"trackers": {}})
    monkeypatch.setattr(
        "tow.trackers.generic.load_secrets", lambda: {"trackers": {"demo": {"username": "u", "password": "p"}}}
    )
    monkeypatch.setattr("tow.trackers.generic.save_secrets", lambda _value: pytest.fail("late cookie write"))

    class Client:
        cookies: ClassVar = {"sid": "new"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def request(self, _method, _url, data):
            return httpx.Response(200)

    monkeypatch.setattr(thttp, "client", lambda **_kwargs: Client())
    tracker = GenericHttpTracker(
        "demo",
        {
            "url_regex": r"^https://a\.example/(\d+)$",
            "login_hosts": ["https://a.example"],
            "login_path": "/login",
            "fetch_hosts": ["https://a.example"],
        },
    )
    assert (
        tracker._login({"trackers": {"demo": {"username": "u", "password": "p"}}}, None)["https://a.example:443"]["sid"]
        == "new"
    )


def test_cooldown_does_not_retry_or_post_login(monkeypatch, tmp_path):
    monkeypatch.setenv("TOW_HOME", str(tmp_path))
    from tow.store import save_state

    save_state(
        {
            "topics": [],
            "mirrors": {
                "demo": {
                    "cool": {"https://a.example": 9e18},
                    "fail": {},
                    "active": None,
                }
            },
        }
    )
    tracker = GenericHttpTracker(
        "demo",
        {
            "url_regex": r"^https://a\.example/(\d+)$",
            "fetch_hosts": ["https://a.example"],
            "login_hosts": ["https://a.example"],
            "login_path": "/login",
        },
    )
    monkeypatch.setattr(tracker, "_login", lambda *a, **k: pytest.fail("login during cooldown"))
    secrets = {"trackers": {"demo": {"username": "u", "password": "p"}}}
    with pytest.raises(MirrorFetchError) as error:
        tracker.fetch_title("https://a.example/1", secrets, None)
    assert error.value.failure == "paused"


def test_preview_never_posts_login(monkeypatch):
    tracker = GenericHttpTracker(
        "demo",
        {
            "url_regex": r"^https://a\.example/(\d+)$",
            "fetch_hosts": ["https://a.example"],
            "login_hosts": ["https://a.example"],
            "login_path": "/login",
        },
    )
    monkeypatch.setattr(tracker, "_login", lambda *a, **k: pytest.fail("login during preview"))
    monkeypatch.setattr(
        tracker, "_get", lambda *a, **k: (type("R", (), {"text": "<title>Show</title>"})(), "https://a.example")
    )
    assert (
        tracker.fetch_title(
            "https://a.example/1",
            {"trackers": {"demo": {"username": "u", "password": "p"}}},
            None,
            persist=False,
        )
        == "Show"
    )


def test_page_download_uses_session_refreshed_during_page_fetch(monkeypatch):
    tracker = GenericHttpTracker(
        "demo",
        {
            "url_regex": r"^https://a\.example/(\d+)$",
            "fetch_hosts": ["https://a.example"],
            "page_download": True,
            "topic_path": "/topic/{id}",
            "download_path": "/download.php?id={id}",
        },
    )
    stored = {"trackers": {"demo": {}}}
    monkeypatch.setattr("tow.trackers.generic.load_secrets", lambda: stored)
    monkeypatch.setattr("tow.trackers.generic.looks_like_torrent", lambda _blob: True)
    calls = []

    def get(_hosts, path, _secrets, _ua, cookies, **_kwargs):
        calls.append((path, cookies))
        if path == "/topic/1":
            stored["trackers"]["demo"]["cookies_by_origin"] = {"https://a.example:443": {"sid": "fresh"}}
            return type("R", (), {"text": '<a href="/download.php?id=42">torrent</a>'})(), "https://a.example"
        return type("R", (), {"content": b"torrent"})(), "https://a.example"

    monkeypatch.setattr(tracker, "_get", get)
    assert tracker.fetch_torrent("https://a.example/1", stored, None) == b"torrent"
    assert calls[1][1] == {"https://a.example:443": {"sid": "fresh"}}


def test_one_transport_failure_counts_once(monkeypatch, tmp_path):
    monkeypatch.setenv("TOW_HOME", str(tmp_path))
    monkeypatch.setattr(
        "tow.config.load_config", lambda: {"trackers": {"demo": {"fetch_hosts": ["https://a.example"]}}}
    )
    tracker = GenericHttpTracker(
        "demo",
        {
            "url_regex": r"^https://a\.example/(\d+)$",
            "fetch_hosts": ["https://a.example"],
        },
    )

    class Response:
        status_code = 500
        headers: ClassVar = {"content-type": "text/plain"}
        content = b"failed"
        text = "failed"
        url = "https://a.example/1"

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, _url):
            return Response()

    monkeypatch.setattr(thttp, "client", lambda **_kw: Client())
    with pytest.raises(MirrorFetchError):
        tracker.fetch_title("https://a.example/1", {}, None)
    assert load_state()["mirrors"]["demo"]["fail"]["https://a.example"] == 1


def test_fetch_title_uses_tracker_page_and_preserves_current_range(monkeypatch):
    tr = GenericHttpTracker(
        "rutor",
        {
            "url_regex": r"^https://rutor\.info/torrent/(\d+)$",
            "fetch_hosts": ["https://rutor.info"],
        },
    )

    class Response:
        text = "<html><head><title>Сериал Г [04x01-22 из 24]</title></head></html>"

    seen = {}

    def fake_get(hosts, path, secrets, ua, cookies, torrent_only, ignore_cool=False, persist=True):
        seen.update({"hosts": hosts, "path": path, "persist": persist, "torrent_only": torrent_only})
        return Response(), "https://rutor.info"

    monkeypatch.setattr(tr, "_get", fake_get)

    title = tr.fetch_title("https://rutor.info/torrent/1234567", {}, "ua", persist=False)

    assert title == "Сериал Г [04x01-22 из 24]"
    assert seen == {
        "hosts": ["https://rutor.info"],
        "path": "/torrent/1234567",
        "persist": False,
        "torrent_only": False,
    }


def test_limited_response_rejects_oversized_content_length_before_reading_body():
    body_read = False

    class Body(httpx.SyncByteStream):
        def __iter__(self):
            nonlocal body_read
            body_read = True
            yield b"not reached"

    def handler(request):
        return httpx.Response(200, headers={"Content-Length": "100"}, stream=Body(), request=request)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(thttp.ResponseTooLargeError, match="exceeds 10 bytes"),
    ):
        thttp.get_limited(client, "https://tracker.example/file", max_bytes=10)

    assert body_read is False


def test_limited_response_rejects_oversized_chunked_body_cumulatively():
    class Body(httpx.SyncByteStream):
        def __iter__(self):
            yield b"123456"
            yield b"78901"

    def handler(request):
        return httpx.Response(200, stream=Body(), request=request)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(thttp.ResponseTooLargeError, match="exceeds 10 bytes"),
    ):
        thttp.get_limited(client, "https://tracker.example/file", max_bytes=10)


def test_limited_response_does_not_decode_compressed_body_twice():
    payload = "NNMClub страница".encode()

    def handler(request):
        return httpx.Response(
            200,
            headers={"Content-Encoding": "gzip", "Content-Type": "text/html; charset=utf-8"},
            content=gzip.compress(payload),
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        response = thttp.get_limited(client, "https://tracker.example/topic", max_bytes=1024)

    assert response.content == payload
    assert response.text == "NNMClub страница"
    assert "content-encoding" not in response.headers
    assert response.headers["content-length"] == str(len(payload))


def test_tracker_uses_separate_html_and_torrent_response_limits(monkeypatch):
    seen = []

    def fake_pick_and_get(_name, _hosts, _path, **kwargs):
        seen.append(kwargs["max_bytes"])
        content = b"d4:info0:e" if kwargs["ok"] else b"<html></html>"
        return httpx.Response(200, content=content), "https://tracker.example"

    monkeypatch.setattr("tow.trackers.generic.pick_and_get", fake_pick_and_get)
    tracker = GenericHttpTracker(
        "demo",
        {
            "url_regex": r"^https://tracker\.example/topic/(\d+)$",
            "fetch_hosts": ["https://tracker.example"],
        },
    )

    tracker._get([], "/topic/1", {}, None, {}, torrent_only=False)
    tracker._get([], "/download/1", {}, None, {}, torrent_only=True)

    assert seen == [thttp.MAX_HTML_RESPONSE_BYTES, thttp.MAX_TORRENT_RESPONSE_BYTES]


def test_tracker_regex_and_url_are_bounded():
    with raises_code("tracker.regex_too_long", ValueError):
        GenericHttpTracker("demo", {"url_regex": "a" * 513})

    tracker = GenericHttpTracker("demo", {"url_regex": r"^https://tracker\.example/(\d+)$"})
    assert tracker.parse_id("https://tracker.example/" + "1" * 5000) is None


def _login_client(monkeypatch, *, status=302, body="", cookies=None, seen=None):
    class Client:
        def __init__(self):
            self.cookies = dict(cookies or {"bb_session": "s"})

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def request(self, _method, _url, data):
            if seen is not None:
                seen.update(data)
            return httpx.Response(status, text=body)

    monkeypatch.setattr(thttp, "client", lambda **_kwargs: Client())


_TORRENTPIER = {
    "url_regex": r"^https://demo/(\\d+)$",
    "login_hosts": ["https://demo"],
    "login_path": "/forum/login.php",
    "login_form": {"user_field": "login_username", "pw_field": "login_password", "extra": {"login": "Вход"}},
}


def test_login_uses_the_trackers_form_field_names(monkeypatch):
    seen = {}
    _login_client(monkeypatch, seen=seen)
    GenericHttpTracker("demo", _TORRENTPIER)._login(
        {"trackers": {"demo": {"username": "u", "password": "p"}}}, "ua", persist=False
    )
    assert seen == {"login_username": "u", "login_password": "p", "login": "Вход"}


def test_refused_login_page_does_not_yield_guest_cookies(monkeypatch):
    page = '<form><input name="login_username"><input type="password" name="login_password"></form>'
    _login_client(monkeypatch, status=200, body=page, cookies={"bb_guid": "guest"})
    cookies = GenericHttpTracker("demo", _TORRENTPIER)._login(
        {"trackers": {"demo": {"username": "u", "password": "wrong"}}}, "ua", persist=False
    )
    assert cookies == {}


def test_successful_login_page_without_password_field_is_accepted(monkeypatch):
    _login_client(monkeypatch, status=200, body="<html>Добро пожаловать</html>", cookies={"bb_session": "s"})
    cookies = GenericHttpTracker("demo", _TORRENTPIER)._login(
        {"trackers": {"demo": {"username": "u", "password": "p"}}}, "ua", persist=False
    )
    assert cookies == {"https://demo:443": {"bb_session": "s"}}


def test_config_template_torrentpier_trackers_declare_their_login_form():
    from tow.trackers.presets import known_sites

    sites = known_sites()
    for name in ("rutracker", "tapochek", "unionpeer"):
        assert sites[name]["login_form"]["pw_field"] == "login_password"
    assert "login_form" not in sites["kinozal"]


def test_a_topic_page_read_for_the_download_link_also_gives_the_title(monkeypatch):
    from types import SimpleNamespace

    from tow.trackers.generic import GenericHttpTracker

    tracker = GenericHttpTracker(
        "nnm",
        {
            "fetch_hosts": ["https://nnm.example"],
            "page_download": True,
            "url_regex": r"^https://nnm\.example/forum/viewtopic\.php\?t=(\d+)$",
        },
    )
    calls = []
    page = SimpleNamespace(content=b"<title>Show S01 :: NNM</title><a href='download.php?id=77'>x</a>")
    torrent = SimpleNamespace(content=b"d4:infod4:name4:showee")

    def fake_get(hosts, path, *args, **kwargs):
        calls.append(path)
        return (page if "viewtopic" in path else torrent), hosts[0]

    monkeypatch.setattr(tracker, "_get", fake_get)
    monkeypatch.setattr(tracker, "_page_download_id", lambda _html: "77")
    monkeypatch.setattr("tow.trackers.generic.looks_like_torrent", lambda _c: True)
    tracker.fetch_torrent("https://nnm.example/forum/viewtopic.php?t=5", {}, None, persist=False)
    title = tracker.fetch_title("https://nnm.example/forum/viewtopic.php?t=5", {}, None, persist=False)
    assert "Show S01" in title
    assert len(calls) == 2  # the page and the file; the title came from the same page


def _streaming_login_client(monkeypatch, response):
    """thttp.client as a real httpx client (the streaming branch of request_limited)."""
    real_client = httpx.Client
    monkeypatch.setattr(
        thttp,
        "client",
        lambda **kwargs: real_client(
            transport=httpx.MockTransport(lambda _request: response()),
            follow_redirects=kwargs["follow_redirects"],
        ),
    )


def test_the_login_answer_is_read_like_a_topic_page_never_whole(monkeypatch):
    # Audit 08.10.2026: the login POST read the answer whole, without the limit topic pages have.
    sent = []

    def endless():
        for _ in range(thttp.MAX_HTML_RESPONSE_BYTES // (1 << 20) + 8):
            sent.append(1)
            yield b"x" * (1 << 20)

    _streaming_login_client(
        monkeypatch, lambda: httpx.Response(200, headers={"Set-Cookie": "bb_session=s; Path=/"}, content=endless())
    )
    cookies = GenericHttpTracker("demo", _TORRENTPIER)._login(
        {"trackers": {"demo": {"username": "u", "password": "p"}}}, "ua", persist=False
    )

    assert cookies == {}  # that host's login failed: the next one would be tried
    assert len(sent) <= thttp.MAX_HTML_RESPONSE_BYTES // (1 << 20) + 1


def test_a_login_answer_within_the_limit_still_yields_its_cookies(monkeypatch):
    _streaming_login_client(
        monkeypatch,
        lambda: httpx.Response(302, headers={"Set-Cookie": "bb_session=s; Path=/", "Location": "/"}, content=b"moved"),
    )
    cookies = GenericHttpTracker("demo", _TORRENTPIER)._login(
        {"trackers": {"demo": {"username": "u", "password": "p"}}}, "ua", persist=False
    )
    assert cookies == {"https://demo:443": {"bb_session": "s"}}
