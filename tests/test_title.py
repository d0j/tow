import pytest

from tow.title import (
    guess_topic_title,
    looks_like_download_url,
    title_from_html,
    title_from_url_slug,
)


def test_slug_rutor():
    t = title_from_url_slug("http://rutor.info/torrent/1234567/pobeg_iz_shoushenka_1994")
    assert "pobeg iz shoushenka" in t.lower()
    assert "1234567" not in t


def test_slug_fast_torrent():
    t = title_from_url_slug(
        "http://fast-torrent.ru/download/torrent/456789/"
        "%D0%9F%D0%BE%D0%B1%D0%B5%D0%B3%20%D0%B8%D0%B7%20%D0%A8%D0%BE%D1%83%D1%88%D0%B5%D0%BD%D0%BA%D0%B0"
        "%20-%20The%20Shawshank%20Redemption%20(1994).torrent"
    )
    assert "Побег из Шоушенка" in t
    assert "Shawshank" in t
    assert ".torrent" not in t.lower()


def test_slug_skips_php():
    assert title_from_url_slug("https://nnmclub.to/forum/download.php?id=2345678") == ""
    assert title_from_url_slug("https://rutracker.org/forum/viewtopic.php?t=7654321") == ""
    assert title_from_url_slug("https://kinozal.guru/details.php?id=3456789") == ""


def test_html_prefers_h1_over_title():
    html = (
        "<html><head><title>rutor.info :: ignored slug</title></head>"
        "<body><h1>Сериал Д [01x01-05 из 10] (2026) WEBRip 1080p от ExKinoRay</h1></body></html>"
    )
    assert title_from_html(html).startswith("Сериал Д")


def test_html_strips_tracker_suffix():
    html = "<html><head><title>Побег из Шоушенка :: RuTracker.org</title></head></html>"
    assert title_from_html(html) == "Побег из Шоушенка"
    html = "<html><head><title>NNM-Club :: Matrix Reloaded</title></head></html>"
    assert "Matrix Reloaded" in title_from_html(html)


def test_slug_title_is_placeholder():
    from tow.title import title_is_placeholder

    url = "http://rutor.info/torrent/1234568/serial-d-01x01-05-iz-10-2026-webrip-1080p-ot-exkinoray"
    assert title_is_placeholder("serial-d-01x01-05-iz-10-2026-webrip-1080p-ot-exkinoray", url)
    assert title_is_placeholder(url, url)
    assert not title_is_placeholder("Сериал Д [01x01-05 из 10]", url)


def test_download_url_not_fetched():
    assert looks_like_download_url("https://nnmclub.to/forum/download.php?id=1")
    assert looks_like_download_url("https://kinozal.x/.dl./download.php?id=1")
    assert not looks_like_download_url("https://kinozal.guru/details.php?id=1")
    assert guess_topic_title("https://nnmclub.to/forum/download.php?id=1", fetch=True) == ""


class _FakeResp:
    def __init__(self, html: str) -> None:
        self.status_code = 200
        self.content = html.encode()
        self.text = html
        self.headers = {"content-type": "text/html; charset=utf-8"}
        self.encoding = "utf-8"


class _FakeClient:
    def __init__(self, html: str) -> None:
        self.html = html

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return None

    def get(self, url: str):
        return _FakeResp(self.html)


def test_guess_prefers_page_h1_not_slug(monkeypatch):
    html = "<html><h1>Сериал Д [01x01-05 из 10]</h1></html>"
    monkeypatch.setattr("tow.http.client", lambda **k: _FakeClient(html))
    t = guess_topic_title("http://rutor.info/torrent/1234568/serial-d-01x01-05-iz-10-2026-webrip-1080p-ot-exkinoray")
    assert t.startswith("Сериал Д")
    assert "serial-d" not in t.lower()


def test_title_guess_does_not_fetch_unconfigured_origin(monkeypatch):
    def unexpected_client(**_kwargs):
        raise AssertionError("unconfigured origin must not be fetched")

    monkeypatch.setattr("tow.http.client", unexpected_client)
    assert guess_topic_title("http://127.0.0.1/private", fetch=True) == "private"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Сериал А / Show A [S02E01-12 из 12] (2025)", "Сериал А / Show A [S02E01-12 из 12] (2025)"),
        ("Сериал А / Show A (2025) :: RuTracker.org", "Сериал А / Show A (2025)"),
        ("NNM-Club :: Матрица / The Matrix", "Матрица / The Matrix"),
    ],
)
def test_auto_title_keeps_every_name_and_drops_only_the_site(raw, expected):
    # N2: the longest " / " fragment won, so the Russian name disappeared.
    from tow.title import _drop_site_bits

    assert _drop_site_bits(raw) == expected


def test_half_a_character_on_a_page_never_reaches_the_state():
    # A lone surrogate in tracker_title made save_state refuse the run's whole state.
    from tow.store import load_state, save_state

    title = title_from_html("<html><head><title>Show \udfff :: RuTracker.org</title></head><h1>Show \udfff</h1></html>")
    assert title == "Show �"
    assert title_from_html("<title>Show \ud800 S01</title>") == "Show � S01"
    save_state({"topics": [{"id": "a", "tracker_title": title}], "mirrors": {}})
    assert load_state()["topics"][0]["tracker_title"] == "Show �"


@pytest.mark.parametrize("url", ["ftp://]/download.php?id=1&=1&t=t=2", "http://[x/show_name_s01"])
def test_a_link_the_parser_refuses_has_no_title_and_is_no_download(url):
    # urlparse raised ValueError("Invalid IPv6 URL") from every one of these.
    from tow.title import title_is_placeholder

    assert title_from_url_slug(url) == ""
    assert looks_like_download_url(url) is False
    assert title_is_placeholder("Show", url) is False
    assert title_is_placeholder("", url) is True
