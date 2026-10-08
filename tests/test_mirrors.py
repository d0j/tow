from typing import ClassVar

import pytest
from helpers import raises_code

from tow import http as thttp
from tow.mirrors import (
    MirrorFetchError,
    _cookies_for_host,
    _ordered,
    _save_bucket,
    pick_and_get,
)
from tow.store import load_state, save_state


def test_cross_host_redirect_is_rejected_before_cookie_bearing_request(monkeypatch, tmp_path):
    monkeypatch.setenv("TOW_HOME", str(tmp_path))
    calls = []

    class Response:
        status_code = 302
        headers: ClassVar = {"location": "https://evil.example/file"}
        url = "https://trusted.example/start"
        content = b""

    class Client:
        cookies: ClassVar = {"uid": "secret"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url):
            calls.append(url)
            return Response()

    monkeypatch.setattr(thttp, "client", lambda **kwargs: Client())

    with pytest.raises(RuntimeError, match="перенаправление на другой адрес"):
        pick_and_get("x", ["https://trusted.example"], "/start", cookies={"uid": "secret"})
    assert calls == ["https://trusted.example/start"]


def test_cross_origin_redirect_is_rejected_before_cookie_bearing_request(monkeypatch, tmp_path):
    monkeypatch.setenv("TOW_HOME", str(tmp_path))
    calls = []

    class Response:
        status_code = 302
        headers: ClassVar = {"location": "https://trusted.example:444/file"}
        url = "https://trusted.example/start"
        content = b""

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url):
            calls.append(url)
            return Response()

    monkeypatch.setattr(thttp, "client", lambda **kwargs: Client())

    with pytest.raises(RuntimeError, match="перенаправление на другой адрес"):
        pick_and_get("x", ["https://trusted.example"], "/start", cookies={"uid": "secret"})
    assert calls == ["https://trusted.example/start"]


def test_allowlisted_download_redirect_uses_fresh_cookie_free_client(monkeypatch, tmp_path):
    monkeypatch.setenv("TOW_HOME", str(tmp_path))
    seen = []

    class Response:
        def __init__(self, url, status_code, location=""):
            self.url = url
            self.status_code = status_code
            self.headers = {"content-type": "application/x-bittorrent"}
            if location:
                self.headers["location"] = location
            self.content = b"torrent"
            self.text = ""

    class Client:
        def __init__(self, cookies):
            self.cookies = cookies

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url):
            seen.append((url, self.cookies))
            if "trusted.example" in url:
                return Response(url, 302, "https://bulk.example/file")
            return Response(url, 200)

    monkeypatch.setattr(thttp, "client", lambda **kwargs: Client(kwargs.get("cookies")))

    response, _host = pick_and_get(
        "x",
        ["https://trusted.example"],
        "/start",
        cookies={"https://trusted.example:443": {"uid": "secret"}},
        allowed_redirect_origins=["https://bulk.example"],
    )

    assert response.content == b"torrent"
    assert seen == [
        ("https://trusted.example/start", {"uid": "secret"}),
        ("https://bulk.example/file", None),
    ]


def test_multi_origin_mirrors_do_not_receive_unscoped_cookies(monkeypatch, tmp_path):
    monkeypatch.setenv("TOW_HOME", str(tmp_path))
    seen = []

    class Response:
        def __init__(self, url, status_code):
            self.url = url
            self.status_code = status_code
            self.headers = {"content-type": "text/plain"}
            self.content = b"ok"
            self.text = "ok"

    class Client:
        def __init__(self, cookies):
            self.cookies = cookies

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url):
            seen.append((url, self.cookies))
            return Response(url, 200 if "b.example" in url else 500)

    monkeypatch.setattr(thttp, "client", lambda **kwargs: Client(kwargs.get("cookies")))

    pick_and_get("x", ["https://a.example", "https://b.example"], "/file", cookies={"uid": "secret"})

    assert seen == [("https://a.example/file", None), ("https://b.example/file", None)]


def test_scoped_mirror_cookie_matches_exact_canonical_origin():
    cookies = {
        "https://a.example:443": {"uid": "a"},
        "https://b.example:8443": {"uid": "b"},
    }
    hosts = ["https://a.example", "https://b.example:8443"]

    assert _cookies_for_host(cookies, "https://a.example", hosts) == {"uid": "a"}
    assert _cookies_for_host(cookies, "https://b.example:8443", hosts) == {"uid": "b"}
    assert _cookies_for_host(cookies, "https://b.example", hosts) == {}


def test_frozen_blocks(monkeypatch, tmp_path):
    monkeypatch.setenv("TOW_HOME", str(tmp_path))
    st = load_state()
    st.setdefault("mirrors", {})["x"] = {"frozen": True, "fail": {}, "cool": {}}
    save_state(st)
    with raises_code("mirrors.frozen", RuntimeError):
        pick_and_get("x", ["http://127.0.0.1:1"], "/")


def test_ordered_active_first():
    assert _ordered(["http://a", "http://b"], "http://b") == ["http://b", "http://a"]


def test_all_cool_message(monkeypatch, tmp_path):
    monkeypatch.setenv("TOW_HOME", str(tmp_path))
    st = load_state()
    st.setdefault("mirrors", {})["x"] = {
        "fail": {},
        "cool": {"http://127.0.0.1:1": 9e18},
        "active": None,
    }
    save_state(st)
    with pytest.raises(RuntimeError, match="паузе"):
        pick_and_get("x", ["http://127.0.0.1:1"], "/")


def test_redirect_loop_is_failure_not_success(monkeypatch, tmp_path):
    monkeypatch.setenv("TOW_HOME", str(tmp_path))

    class Response:
        status_code = 302
        headers: ClassVar = {"location": "/loop"}
        url = "https://a.example/loop"
        content = b""
        text = ""

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, _url):
            return Response()

    monkeypatch.setattr(thttp, "client", lambda **_kwargs: Client())
    with raises_code("mirrors.all_failed", MirrorFetchError) as error:
        pick_and_get("demo", ["https://a.example"], "/loop", persist=False)
    assert error.value.failure == "redirect_loop"
    assert error.value.params["error"].code == "mirrors.redirect_limit"


def test_login_page_failure_does_not_cool_reachable_host(monkeypatch, tmp_path):
    monkeypatch.setenv("TOW_HOME", str(tmp_path))

    class Response:
        status_code = 200
        headers: ClassVar = {"content-type": "text/html"}
        url = "https://a.example/download"
        content = b"<html>login</html>"
        text = "<html>login</html>"

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, _url):
            return Response()

    monkeypatch.setattr(thttp, "client", lambda **_kwargs: Client())
    with pytest.raises(MirrorFetchError) as error:
        pick_and_get("demo", ["https://a.example"], "/download", ok=lambda _r: False)
    assert error.value.failure == "auth"
    bucket = load_state()["mirrors"].get("demo", {"fail": {}, "cool": {}})
    assert bucket["fail"].get("https://a.example") is None
    assert bucket["cool"].get("https://a.example") is None


def test_auth_error_is_not_hidden_by_later_transport_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("TOW_HOME", str(tmp_path))

    class Response:
        status_code = 401
        headers: ClassVar = {"content-type": "text/html"}
        url = "https://login.example/file"
        content = b"login"
        text = "login"

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url):
            if "broken.example" in url:
                raise OSError("connection refused")
            return Response()

    monkeypatch.setattr(thttp, "client", lambda **_kwargs: Client())
    with raises_code("mirrors.all_failed", MirrorFetchError) as error:
        pick_and_get("demo", ["https://login.example", "https://broken.example"], "/file", persist=False)
    assert error.value.failure == "auth"
    assert error.value.error_class == "tracker_auth"
    assert error.value.params["error"].code == "mirrors.http_auth"


def test_quota_takes_precedence_over_earlier_auth(monkeypatch, tmp_path):
    monkeypatch.setenv("TOW_HOME", str(tmp_path))

    class Response:
        headers: ClassVar = {"content-type": "text/html"}

        def __init__(self, url):
            self.url = url
            self.status_code = 401 if "login.example" in url else 200
            self.content = b"login" if self.status_code == 401 else "количество торрент-файлов исчерпано".encode()
            self.text = self.content.decode("utf-8")

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url):
            return Response(url)

    monkeypatch.setattr(thttp, "client", lambda **_kwargs: Client())
    with pytest.raises(MirrorFetchError, match="лимит") as error:
        pick_and_get(
            "demo",
            ["https://login.example", "https://quota.example"],
            "/file",
            ok=lambda _r: False,
            persist=False,
            download_limit=True,
        )
    assert error.value.failure == "quota"
    assert error.value.error_class == "quota"


def test_late_fetch_cannot_recreate_deleted_mirror(monkeypatch, tmp_path):
    monkeypatch.setenv("TOW_HOME", str(tmp_path))
    monkeypatch.setattr("tow.config.load_config", lambda: {"trackers": {}})
    _save_bucket("deleted", {"fail": {"https://old.example": 1}, "cool": {}, "active": None}, "https://old.example")
    assert "deleted" not in load_state().get("mirrors", {})


def _status_client(monkeypatch, status_for):
    class Response:
        def __init__(self, url, status_code):
            self.url = url
            self.status_code = status_code
            self.headers = {"content-type": "text/plain"}
            self.content = b"nope"
            self.text = "nope"

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url):
            return Response(url, status_for(url))

    monkeypatch.setattr(thttp, "client", lambda **kwargs: Client())


@pytest.mark.parametrize(("status", "cools"), [(404, False), (410, False), (500, True), (429, True)])
def test_only_host_level_http_errors_put_a_mirror_into_cooldown(monkeypatch, tmp_path, status, cools):
    from tow.mirrors import MirrorFetchError
    from tow.store import load_state

    monkeypatch.setenv("TOW_HOME", str(tmp_path))
    _status_client(monkeypatch, lambda _url: status)
    for _ in range(3):
        with pytest.raises(MirrorFetchError):
            pick_and_get("rutor", ["http://rutor.info"], "/topic/1", fail_threshold=3)

    bucket = load_state()["mirrors"].get("rutor", {})
    assert bool(bucket.get("cool", {}).get("http://rutor.info")) is cools


def test_oversized_topic_does_not_cool_the_mirror(monkeypatch, tmp_path):
    from tow.mirrors import MirrorFetchError
    from tow.store import load_state

    monkeypatch.setenv("TOW_HOME", str(tmp_path))

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(thttp, "client", lambda **kwargs: Client())

    def too_large(*_args, **_kwargs):
        raise thttp.ResponseTooLargeError("response body exceeds 1 bytes")

    monkeypatch.setattr(thttp, "get_limited", too_large)
    for _ in range(3):
        with pytest.raises(MirrorFetchError):
            pick_and_get("rutor", ["http://rutor.info"], "/topic/1", fail_threshold=3)

    assert not load_state()["mirrors"].get("rutor", {}).get("cool", {}).get("http://rutor.info")


def test_healthy_fetches_from_the_active_mirror_do_not_rewrite_state(monkeypatch, tmp_path):
    # F3: every successful fetch rewrote and fsynced state.json although nothing changed.
    from tow import mirrors

    monkeypatch.setenv("TOW_HOME", str(tmp_path))
    monkeypatch.setattr(
        "tow.config.load_config", lambda: {"trackers": {"rutor": {"fetch_hosts": ["http://rutor.info"]}}}
    )
    _status_client(monkeypatch, lambda _url: 200)
    writes = []
    real_save = mirrors.save_state
    monkeypatch.setattr(mirrors, "save_state", lambda state: writes.append(1) or real_save(state))

    for _ in range(5):
        pick_and_get("rutor", ["http://rutor.info"], "/topic/1")

    assert len(writes) == 1  # the first success makes it the active mirror; the rest change nothing
    assert load_state()["mirrors"]["rutor"]["active"] == "http://rutor.info"


def test_tow_failing_on_an_answer_does_not_cool_the_mirror(monkeypatch, tmp_path):
    # A mirror answered and TOW failed on the answer (a header it could not rebuild): the
    # mirror is not to blame - three such checks paused every mirror and the whole site.
    monkeypatch.setenv("TOW_HOME", str(tmp_path))

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(thttp, "client", lambda **kwargs: Client())

    def broken(*_args, **_kwargs):
        raise UnicodeEncodeError("ascii", "Кириллица", 0, 1, "ordinal not in range(128)")

    monkeypatch.setattr(thttp, "get_limited", broken)
    for _ in range(3):
        with raises_code("mirrors.local", MirrorFetchError) as error:
            pick_and_get("rutor", ["http://rutor.info", "http://rutor.is"], "/topic/1", fail_threshold=3)
    assert error.value.error_class == "error"
    assert error.value.params["error"] == "UnicodeEncodeError"
    bucket = load_state().get("mirrors", {}).get("rutor", {})
    assert not bucket.get("cool")
    assert not any(bucket.get("fail", {}).values())


def test_a_lost_connection_still_counts_towards_the_cooldown(monkeypatch, tmp_path):
    import httpx

    monkeypatch.setenv("TOW_HOME", str(tmp_path))

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(thttp, "client", lambda **kwargs: Client())

    def down(*_args, **_kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(thttp, "get_limited", down)
    for _ in range(3):
        with raises_code("mirrors.all_failed", MirrorFetchError):
            pick_and_get("rutor", ["http://rutor.info"], "/topic/1", fail_threshold=3)
    assert load_state()["mirrors"]["rutor"]["cool"].get("http://rutor.info")
