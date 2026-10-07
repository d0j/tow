"""What a site's 200 page instead of a torrent means: an expired session, a Cloudflare check,
a removed topic or a daily limit - and how often the saved password is sent for it.

Requests go through the real mirror picker into an ``httpx.MockTransport`` (synthetic site).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from helpers import make_torrent

from tow import http as thttp
from tow import mirrors
from tow.config import load_config, save_config
from tow.log import error_class
from tow.mirrors import MirrorFetchError, pick_and_get
from tow.torrent import is_download_limit, looks_like_torrent, parse_torrent_metadata
from tow.trackers.generic import GenericHttpTracker, TrackerError

INFO = {b"length": 1, b"name": b"x", b"piece length": 16384, b"pieces": b"x" * 20}
TORRENT = make_torrent(INFO)
ORIGIN = "https://demo.example:443"
SPEC = {
    "url_regex": r"^https://demo\.example/viewtopic\.php\?t=(\d+)",
    "fetch_hosts": ["https://demo.example"],
    "login_hosts": ["https://demo.example"],
    "login_path": "/login.php",
}
SECRETS = {
    "trackers": {"demo": {"username": "u", "password": "p", "cookies_by_origin": {ORIGIN: {"bb_session": "stale"}}}}
}
CF_PAGE = (
    "<!DOCTYPE html><html><head><title>Just a moment...</title></head>"
    "<body><div id='challenge-platform'></div><script>window._cf_chl_opt={}</script></body></html>"
)


class Site:
    def __init__(self) -> None:
        self.pages: dict[str, Any] = {}
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.pages.get(request.url.raw_path.decode())
        if callable(answer):
            answer = answer(request)
        if answer is None:
            return httpx.Response(404, text="not found")
        if isinstance(answer, httpx.Response):
            return answer
        if isinstance(answer, bytes):
            return httpx.Response(200, content=answer, headers={"content-type": "application/x-bittorrent"})
        return httpx.Response(200, text=answer, headers={"content-type": "text/html; charset=utf-8"})

    def count(self, path: str) -> int:
        return sum(1 for r in self.requests if r.url.raw_path.decode() == path)


@pytest.fixture
def site(monkeypatch):
    holder = Site()
    transport = httpx.MockTransport(holder.handler)

    def client(ua=None, cookies=None, follow_redirects=True, *, public_only=False):
        return httpx.Client(transport=transport, cookies=cookies or {}, follow_redirects=follow_redirects)

    monkeypatch.setattr(thttp, "client", client)
    logins = {"n": 0}

    def login(_request: httpx.Request) -> httpx.Response:
        logins["n"] += 1
        return httpx.Response(302, headers={"location": "/index.php", "set-cookie": f"bb_session=fresh{logins['n']}"})

    holder.pages["/login.php"] = login
    return holder


def _tracker(**spec: Any) -> GenericHttpTracker:
    return GenericHttpTracker("demo", {**SPEC, **spec})


def _configured(**spec: Any) -> None:
    """The site is configured (a login of a deleted site is never remembered)."""
    cfg = load_config()
    cfg["trackers"] = {"demo": {**SPEC, "title": "Demo", **spec}}
    save_config(cfg)


# --- an expired session on a page-download site (TorrentPier: tapochek, unionpeer) ------------

PAGE_SPEC = {"page_download": True, "topic_path": "/viewtopic.php?t={id}", "download_path": "/download.php?id={id}"}


def test_guest_topic_page_logs_in_again_once_and_downloads(site):
    def topic(request: httpx.Request) -> str:
        if "bb_session=fresh" in request.headers.get("cookie", ""):
            return "<html><a href='login.php?logout=1'>Выход</a><a href='download.php?id=77'>.torrent</a></html>"
        return "<html><title>Show</title><body>Войдите, чтобы скачать</body></html>"

    def download(request: httpx.Request) -> bytes | str:
        return TORRENT if "bb_session=fresh" in request.headers.get("cookie", "") else "<html>Войдите</html>"

    site.pages["/viewtopic.php?t=5"] = topic
    site.pages["/download.php?id=77"] = download
    blob = _tracker(**PAGE_SPEC).fetch_torrent("https://demo.example/viewtopic.php?t=5", SECRETS, "ua")
    assert blob == TORRENT
    assert site.count("/login.php") == 1


def test_guest_topic_page_after_a_refused_login_is_reported_once(site):
    site.pages["/viewtopic.php?t=5"] = "<html><title>Show</title><body>Войдите, чтобы скачать</body></html>"
    with pytest.raises(TrackerError) as info:
        _tracker(**PAGE_SPEC).fetch_torrent("https://demo.example/viewtopic.php?t=5", SECRETS, "ua")
    assert info.value.code == "tracker.no_download_link"
    assert error_class(info.value) == "tracker_auth"
    assert site.count("/login.php") == 1


def test_member_page_without_a_link_is_a_page_not_understood(site):
    """Signed in, but no link of the topic's own: the site changed its layout. Not a sign-in
    problem (no login, no magnet fallback), amber."""
    site.pages["/viewtopic.php?t=5"] = "<html><a href='login.php?logout=1'>Выход</a>attachment removed</html>"
    with pytest.raises(TrackerError) as info:
        _tracker(**PAGE_SPEC).fetch_torrent("https://demo.example/viewtopic.php?t=5", SECRETS, "ua")
    assert info.value.code == "tracker.page_not_understood"
    assert error_class(info.value) == "tracker"
    assert site.count("/login.php") == 0


def test_removed_topic_page_is_gone_without_a_login(site):
    site.pages["/viewtopic.php?t=5"] = "<html><body>Тема не найдена</body></html>"
    with pytest.raises(MirrorFetchError) as info:
        _tracker(**PAGE_SPEC).fetch_torrent("https://demo.example/viewtopic.php?t=5", SECRETS, "ua")
    assert error_class(info.value) == "gone"
    assert site.count("/login.php") == 0


def test_the_link_in_the_topics_own_block_wins_over_one_in_a_post(site):
    site.pages["/viewtopic.php?t=5"] = (
        "<html><div class=post>Предыдущий сезон: <a href='download.php?id=111'>скачать</a></div>"
        "<table class='attach bordered'><tr><td><a href='download.php?id=222'>Скачать .torrent</a></td></tr></table>"
        "<div class=post>ещё <a href='download.php?id=333'>старое</a></div></html>"
    )
    site.pages["/download.php?id=111"] = make_torrent({**INFO, b"name": b"other"})
    site.pages["/download.php?id=222"] = TORRENT
    tracker = _tracker(**PAGE_SPEC, login_path="")
    assert tracker.fetch_torrent("https://demo.example/viewtopic.php?t=5", {}, "ua", persist=False) == TORRENT
    assert site.count("/download.php?id=111") == site.count("/download.php?id=333") == 0


def test_page_with_different_download_links_is_refused(site):
    site.pages["/viewtopic.php?t=5"] = (
        "<html><table class=attach><a href='download.php?id=111'>скачать</a></table>"
        "<table class=attach><a href='download.php?id=222'>Скачать .torrent</a></table></html>"
    )
    site.pages["/download.php?id=111"] = make_torrent({**INFO, b"name": b"other"})
    site.pages["/download.php?id=222"] = TORRENT
    tracker = _tracker(**PAGE_SPEC, login_path="")
    with pytest.raises(TrackerError) as info:
        tracker.fetch_torrent("https://demo.example/viewtopic.php?t=5", {}, "ua", persist=False)
    assert info.value.code == "tracker.download_link_ambiguous"
    assert site.count("/download.php?id=111") == site.count("/download.php?id=222") == 0


def test_a_link_in_a_post_on_a_page_without_a_download_block_is_not_taken(site):
    site.pages["/viewtopic.php?t=5"] = (
        "<html><div class=post_body>Season 1: <a href='download.php?id=111'>.torrent</a></div></html>"
    )
    site.pages["/download.php?id=111"] = make_torrent({**INFO, b"name": b"other"})
    tracker = _tracker(**PAGE_SPEC, login_path="")
    with pytest.raises(TrackerError) as info:
        tracker.fetch_torrent("https://demo.example/viewtopic.php?t=5", {}, "ua", persist=False)
    assert info.value.code == "tracker.no_download_link"
    assert site.count("/download.php?id=111") == 0


# --- nothing on a guest page is believed, a changed layout is not a sign-in --------------------


def _configured_page_site() -> None:
    _configured(**PAGE_SPEC)


def test_guest_page_with_a_foreign_link_in_a_post_is_not_downloaded(site):
    """An expired session hides the topic's own block; a comment links an earlier season."""
    _configured_page_site()
    site.pages["/viewtopic.php?t=5"] = (
        "<html><title>Show S02</title><a href='login.php'>Вход</a>"
        "<div class=post_body>Прошлый сезон: <a href='download.php?id=999'>скачать</a></div></html>"
    )
    site.pages["/download.php?id=999"] = make_torrent({**INFO, b"name": b"OTHER-TOPIC"})
    with pytest.raises(TrackerError) as info:
        _tracker(**PAGE_SPEC).fetch_torrent("https://demo.example/viewtopic.php?t=5", SECRETS, "ua")
    assert info.value.code == "tracker.no_download_link"
    assert error_class(info.value) == "tracker_auth"
    assert site.count("/login.php") == 1
    assert site.count("/download.php?id=999") == 0


def test_a_changed_own_link_does_not_fall_back_to_the_description(site):
    site.pages["/viewtopic.php?t=5"] = (
        "<html><a href='login.php?logout=1'>Выход</a>"
        "<div class=post_body>Season 1: <a href='download.php?id=111'>.torrent</a></div>"
        "<table class=attach><a class=dl-link href='download.php?attach_id=222'>Скачать</a></table></html>"
    )
    site.pages["/download.php?id=111"] = make_torrent({**INFO, b"name": b"other"})
    with pytest.raises(TrackerError) as info:
        _tracker(**PAGE_SPEC).fetch_torrent("https://demo.example/viewtopic.php?t=5", SECRETS, "ua")
    assert info.value.code == "tracker.page_not_understood"
    assert site.count("/download.php?id=111") == 0


def test_guest_page_with_removed_words_in_a_comment_logs_in_again(site):
    _configured_page_site()
    site.pages["/viewtopic.php?t=5"] = (
        "<html><title>Show</title><a href='login.php'>Вход</a>"
        "<div class=post_body>У меня клиент пишет: торрент не найден, перезалейте</div></html>"
    )
    with pytest.raises(TrackerError) as info:
        _tracker(**PAGE_SPEC).fetch_torrent("https://demo.example/viewtopic.php?t=5", SECRETS, "ua")
    assert info.value.code == "tracker.no_download_link"
    assert site.count("/login.php") == 1


def test_removed_words_in_a_post_are_not_the_sites_message(site):
    site.pages["/viewtopic.php?t=5"] = (
        "<html><a href='login.php?logout=1'>Выход</a><div class=post_body>Topic not found in search? "
        "Use the new link.</div><button data-dl='222'>Скачать</button></html>"
    )
    with pytest.raises(TrackerError) as info:
        _tracker(**PAGE_SPEC).fetch_torrent("https://demo.example/viewtopic.php?t=5", SECRETS, "ua")
    assert info.value.code == "tracker.page_not_understood"


def test_the_sites_own_removed_message_is_gone(site):
    site.pages["/viewtopic.php?t=5"] = (
        "<html><a href='login.php?logout=1'>Выход</a><h1>Тема не найдена</h1><div class=post>ответ</div></html>"
    )
    with pytest.raises(MirrorFetchError) as info:
        _tracker(**PAGE_SPEC).fetch_torrent("https://demo.example/viewtopic.php?t=5", SECRETS, "ua")
    assert error_class(info.value) == "gone"


def test_a_logout_word_in_a_post_is_not_a_way_to_sign_out():
    from tow.trackers.generic import guest_page, signed_in

    page = "<html><a href='login.php'>Вход</a><div class=post>Нажмите Logout в клиенте</div></html>"
    assert guest_page(page)
    assert not signed_in(page)
    member = "<html><a href='login.php?logout=1'>Выход</a><div class=post>войдите в клиент</div></html>"
    assert signed_in(member)
    assert not guest_page(member)
    quoted = "<html><a href='login.php'>Вход</a><div class=post><a href='/login.php?logout=1'>x</a></div></html>"
    assert guest_page(quoted)
    assert not signed_in(quoted)


def test_page_repeating_one_download_link_downloads_it(site):
    site.pages["/viewtopic.php?t=5"] = (
        "<html><a href='download.php?id=222'>a</a><a href='download.php?id=222'>b</a></html>"
    )
    site.pages["/download.php?id=222"] = TORRENT
    tracker = _tracker(**PAGE_SPEC, login_path="")
    assert tracker.fetch_torrent("https://demo.example/viewtopic.php?t=5", {}, "ua", persist=False) == TORRENT


# --- a direct download answered by a page ------------------------------------------------------


def test_removed_topic_answer_is_gone_and_sends_no_password(site):
    site.pages["/dl.php?t=5"] = "<html><body>Тема не найдена</body></html>"
    tracker = _tracker(download_path="/dl.php?t={id}")
    with pytest.raises(MirrorFetchError) as info:
        tracker.fetch_torrent("https://demo.example/viewtopic.php?t=5", SECRETS, "ua")
    assert error_class(info.value) == "gone"
    assert site.count("/login.php") == 0


def test_cloudflare_check_with_status_200_is_cloudflare_without_a_login(site):
    site.pages["/dl.php?t=5"] = CF_PAGE
    tracker = _tracker(download_path="/dl.php?t={id}")
    with pytest.raises(MirrorFetchError) as info:
        tracker.fetch_torrent("https://demo.example/viewtopic.php?t=5", SECRETS, "ua")
    assert error_class(info.value) == "cloudflare"
    assert site.count("/login.php") == 0


def test_cloudflare_script_on_an_ordinary_page_is_not_a_check(site):
    site.pages["/viewtopic.php?t=5"] = (
        "<html><head><title>Show A</title></head><body><h1>Show A</h1>"
        "<script src='/cdn-cgi/challenge-platform/scripts/jsd/main.js'></script></body></html>"
    )
    assert _tracker(login_path="").fetch_title("https://demo.example/viewtopic.php?t=5", {}, "ua", persist=False) == (
        "Show A"
    )


def test_cloudflare_or_sign_in_page_never_becomes_the_title(site):
    tracker = _tracker(login_path="")
    site.pages["/viewtopic.php?t=5"] = CF_PAGE
    with pytest.raises(MirrorFetchError) as info:
        tracker.fetch_title("https://demo.example/viewtopic.php?t=5", {}, "ua", persist=False)
    assert error_class(info.value) == "cloudflare"
    site.pages["/viewtopic.php?t=5"] = (
        "<html><head><title>Вход :: Demo</title></head><body><h1>Вход</h1>"
        "<input type=password name=login_password></body></html>"
    )
    with pytest.raises(TrackerError) as signed_out:
        tracker.fetch_title("https://demo.example/viewtopic.php?t=5", {}, "ua", persist=False)
    assert signed_out.value.code == "tracker.sign_in_page"
    assert error_class(signed_out.value) == "tracker_auth"


def test_scheduled_checks_send_the_password_once_per_pause(site):
    """Every topic of a run, and the next runs, find the site answering with a page: the
    password goes out once, not for every topic every check. The owner's own check still logs in."""
    _configured(download_path="/dl.php?t={id}")
    site.pages["/dl.php?t=5"] = "<html><body>Пожалуйста, войдите</body></html>"
    tracker = _tracker(download_path="/dl.php?t={id}")
    for _ in range(3):
        with pytest.raises(MirrorFetchError) as info:
            tracker.fetch_torrent("https://demo.example/viewtopic.php?t=5", SECRETS, "ua")
        assert error_class(info.value) == "tracker_auth"
    assert site.count("/login.php") == 1
    with pytest.raises(MirrorFetchError):
        tracker.fetch_torrent("https://demo.example/viewtopic.php?t=5", SECRETS, "ua", ignore_cool=True)
    assert site.count("/login.php") == 2


def test_login_pause_ends(site, monkeypatch):
    _configured(download_path="/dl.php?t={id}")
    site.pages["/dl.php?t=5"] = "<html><body>Пожалуйста, войдите</body></html>"
    tracker = _tracker(download_path="/dl.php?t={id}")
    now = [1_000_000.0]
    monkeypatch.setattr(mirrors, "_now", lambda: now[0])
    for step in (0, mirrors.LOGIN_PAUSE_SEC + 1):
        now[0] += step
        with pytest.raises(MirrorFetchError):
            tracker.fetch_torrent("https://demo.example/viewtopic.php?t=5", SECRETS, "ua")
    assert site.count("/login.php") == 2


# --- a valid torrent written with unsorted top-level keys -------------------------------------


def test_unsorted_root_keys_are_a_torrent(site):
    info = TORRENT[len(b"d4:info") : -1]
    blob = b"d4:info" + info + b"8:announce14:http://t/a?k=1e"
    assert looks_like_torrent(blob)
    assert parse_torrent_metadata(blob).infohash == parse_torrent_metadata(TORRENT).infohash
    site.pages["/dl.php?t=5"] = blob
    tracker = _tracker(download_path="/dl.php?t={id}")
    assert tracker.fetch_torrent("https://demo.example/viewtopic.php?t=5", SECRETS, "ua") == blob
    assert site.count("/login.php") == 0


def test_repeated_root_key_is_not_a_torrent():
    info = TORRENT[len(b"d4:info") : -1]
    assert not looks_like_torrent(b"d4:info" + info + b"4:info" + info + b"e")


# --- the daily limit is the limit's own words, asked only of a limited site --------------------


@pytest.mark.parametrize(
    "page",
    [
        "<html><title>Вход</title><form><input type=password></form><div>Количество торрентов: 1 234</div>",
        "<html><body>Торрент-файл недоступен: раздача закрыта правообладателем</body></html>",
    ],
)
def test_pages_that_only_mention_torrents_are_not_the_limit(page):
    assert not is_download_limit(page.encode("cp1251"))
    assert not is_download_limit(page.encode("utf-8"))


def test_the_limit_page_is_the_limit():
    page = "Вам недоступен торрент-файл для скачивания. Вы скачали сегодня ( 20 )."
    assert is_download_limit(page.encode("cp1251"))
    assert is_download_limit(page.encode("utf-8"))


def test_limit_words_on_a_site_without_a_limit_are_a_sign_in(site):
    page = "<html><title>Вход</title>Вы скачали сегодня торрент-файл</html>"
    site.pages["/dl.php?t=5"] = page
    with pytest.raises(MirrorFetchError) as info:
        _tracker(download_path="/dl.php?t={id}").fetch_torrent(
            "https://demo.example/viewtopic.php?t=5", {}, "ua", persist=False
        )
    assert error_class(info.value) == "tracker_auth"
    with pytest.raises(MirrorFetchError) as limited:
        _tracker(download_path="/dl.php?t={id}", download_limit=True).fetch_torrent(
            "https://demo.example/viewtopic.php?t=5", {}, "ua", persist=False
        )
    assert error_class(limited.value) == "quota"


# --- "topic removed" needs every mirror tried to say so ----------------------------------------


@pytest.mark.parametrize("hosts", [["a", "b"], ["b", "a"]])
def test_removed_needs_every_mirror_whatever_the_order(monkeypatch, hosts):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "a.example":
            raise httpx.ConnectError("refused")
        return httpx.Response(404, text="not found")

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        thttp,
        "client",
        lambda ua=None, cookies=None, follow_redirects=True, *, public_only=False: httpx.Client(
            transport=transport, follow_redirects=follow_redirects
        ),
    )
    with pytest.raises(MirrorFetchError) as info:
        pick_and_get("demo", [f"https://{h}.example" for h in hosts], "/download/5", persist=False)
    assert error_class(info.value) == "tracker"
    with pytest.raises(MirrorFetchError) as gone:
        pick_and_get("demo", ["https://b.example", "https://c.example"], "/download/5", persist=False)
    assert error_class(gone.value) == "gone"
