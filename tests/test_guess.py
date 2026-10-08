import re

import pytest

from tow.guess import guess_from_url


def _id(g: dict, url: str) -> str:
    m = re.compile(g["url_regex"]).match(url)
    assert m, (g["url_regex"], url)
    return m.group(1)


def test_nnm_download():
    g = guess_from_url("https://nnmclub.to/forum/download.php?id=2345678")
    assert g["name"] == "nnmclub"
    assert g["fetch_hosts"] == "https://nnmclub.to"
    assert g["page_download"] is True
    assert g["download_path"] == "/forum/download.php?id={id}"
    assert g["login_path"] == "/forum/login.php"
    assert g["need_login"] is True
    assert _id(g, "https://nnmclub.to/forum/viewtopic.php?t=555") == "555"
    assert not re.compile(g["url_regex"]).match("https://nnmclub.to/forum/download.php?id=2345678")


def test_nnm_viewtopic():
    g = guess_from_url("https://www.nnmclub.to/forum/viewtopic.php?t=555&sid=abc")
    assert g["name"] == "nnmclub"
    assert g["page_download"] is True
    assert _id(g, "https://nnmclub.to/forum/viewtopic.php?t=555") == "555"


def test_rutracker_dl():
    g = guess_from_url("https://rutracker.org/forum/dl.php?t=7654321")
    assert g["name"] == "rutracker"
    assert g["page_download"] is False
    assert g["download_path"] == "/forum/dl.php?t={id}"
    assert g["need_login"] is True
    assert _id(g, "https://rutracker.org/forum/viewtopic.php?t=7654321") == "7654321"
    assert _id(g, "https://rutracker.org/forum/dl.php?t=7654321") == "7654321"


def test_rutracker_net_topic():
    g = guess_from_url("https://rutracker.net/forum/viewtopic.php?t=7654321")
    assert g["name"] == "rutracker"
    assert g["download_path"] == "/forum/dl.php?t={id}"
    assert _id(g, "https://www.rutracker.net/forum/viewtopic.php?t=1") == "1"


def test_fast_torrent():
    g = guess_from_url(
        "http://fast-torrent.ru/download/torrent/456789/"
        "%D0%9F%D0%BE%D0%B1%D0%B5%D0%B3%20%D0%B8%D0%B7%20%D0%A8%D0%BE%D1%83%D1%88%D0%B5%D0%BD%D0%BA%D0%B0"
        "%20-%20The%20Shawshank%20Redemption%20(1994)%20BDRip%202160p%20%7C%204K%20%7C%20HDR%20%7C%20D%20P%20P2%20A.torrent"
    )
    assert g["name"] == "fast_torrent"
    assert g["fetch_hosts"] == "http://fast-torrent.ru"
    assert g["download_path"] == "/download/torrent/{id}"
    assert g["need_login"] is False
    assert _id(g, "http://fast-torrent.ru/download/torrent/456789/x.torrent") == "456789"


def test_rutor_topic():
    g = guess_from_url("http://rutor.info/torrent/1234567/x")
    assert g["name"] == "rutor"
    assert g["download_path"] == "/download/{id}"
    assert _id(g, "https://rutor.info/torrent/1234567/name") == "1234567"
    assert _id(g, "http://www.rutor.info/torrent/9") == "9"


def test_new_rutor():
    g = guess_from_url("https://new-rutor.org/torrent/1/x")
    assert g["name"] == "rutor"
    assert _id(g, "https://new-rutor.org/torrent/1/") == "1"


def test_rutor_cdn_download():
    g = guess_from_url("https://d.rutor.info/download/1234568")
    assert g["name"] == "rutor"
    from tow.guess import canon_watch_url

    assert canon_watch_url("https://d.rutor.info/download/1234568") == "http://rutor.info/torrent/1234568"
    assert _id(g, "https://d.rutor.info/download/1234568") == "1234568"
    assert _id(g, "http://rutor.info/torrent/1234568/x") == "1234568"


def test_kinozal_details():
    g = guess_from_url("https://kinozal.guru/details.php?id=3456789")
    assert g["name"] == "kinozal"
    assert g["download_path"] == "/.dl./download.php?id={id}"
    assert g["login_path"] == "/takelogin.php"
    assert _id(g, "https://kinozal.guru/details.php?id=3456789") == "3456789"


def test_kinozal_dl_mirror():
    g = guess_from_url("https://kinozal.jumpingcrab.com/.dl./download.php?id=3456789")
    assert g["name"] == "kinozal"
    assert g["download_path"] == "/.dl./download.php?id={id}"
    assert _id(g, "https://kinozal.jumpingcrab.com/details.php?id=7") == "7"
    assert _id(g, "https://kinozal.jumpingcrab.com/.dl./download.php?id=7") == "7"


def test_tapochek_download_not_kinozal():
    g = guess_from_url("https://tapochek.net/download.php?id=99")
    assert g["name"] == "tapochek"
    assert g["page_download"] is True
    assert g["download_path"] == "/download.php?id={id}"
    assert g["login_path"] == "/login.php"
    assert _id(g, "https://tapochek.net/viewtopic.php?t=42") == "42"


def test_magnet_rejected():
    with pytest.raises(ValueError, match="magnet"):
        guess_from_url("magnet:?xt=urn:btih:abc")


def test_generic_guess_preserves_custom_port():
    guessed = guess_from_url("https://example.org:8443/viewtopic.php?t=12")
    assert guessed["fetch_hosts"] == "https://example.org:8443"
    assert _id(guessed, "https://example.org:8443/viewtopic.php?t=45") == "45"


@pytest.mark.parametrize(
    ("host", "name"),
    [
        ("d.rutor.info", "rutor"),
        ("kinozal.jumpingcrab.com", "kinozal"),
        ("tracker.example.org", "example"),
        ("x.example.co.uk", "example"),
        ("www.tapochek.net", "tapochek"),
    ],
)
def test_site_name_is_the_brand_or_the_registrable_domain(host, name):
    # N10: the first label was used ("d" for d.rutor.info, "tracker" for tracker.example.org).
    from tow.guess import _name_from_host

    assert _name_from_host(host) == name


@pytest.mark.parametrize(
    ("url", "topic_id", "download_path"),
    [
        ("https://serials.example.org/2024/serial-12345.html", "12345", "/2024/serial-{id}.html"),
        ("https://x.example.org/view?cat=500&id=98765", "98765", "/view?cat=500&id={id}"),
    ],
)
def test_topic_number_is_the_id_parameter_or_the_last_number(url, topic_id, download_path):
    # N10: the first number won, which was often a year or a category.
    import re

    from tow.guess import guess_from_url

    guessed = guess_from_url(url)

    assert re.match(guessed["url_regex"], url).group(1) == topic_id
    assert guessed["download_path"] == download_path


@pytest.mark.parametrize(
    ("url", "name"),
    [
        ("http://127.0.0.1:18944/forum/viewtopic.php?t=7", "site_18944"),
        ("http://10.0.0.1/torrent/123", "site"),
        ("http://[::1]:8080/torrent/123", "site_8080"),
        ("http://localhost:8080/torrent/123", "site_8080"),
    ],
)
def test_a_site_on_an_address_is_named_by_its_port(url, name):
    # The address's last-but-one label named the site "0" for 127.0.0.1.
    assert guess_from_url(url)["name"] == name


FORUM_PAGE = """<!doctype html><html><body><a href="./login.php?logout=1">Log out</a>
<h1 class="maintitle">Show</h1>
<table class="attach bordered med"><tr><td><a href="dl.php?t=7" class="dl-stub">Download</a></td></tr></table>
<div class="post_body">see also <a href="download.php?id=999">another torrent</a></div></body></html>"""


def test_a_forum_guess_takes_the_download_path_from_the_topic_page(monkeypatch):
    # The forum links dl.php?t=7 on its topic page; the guess said download.php?id={id},
    # so every download of the new site failed.
    from tow import guess

    url = "https://forum.example/forum/viewtopic.php?t=7"
    monkeypatch.setattr(guess, "_topic_page", lambda page_url: FORUM_PAGE if page_url == url else "")

    guessed = guess.guess_site(url)

    assert guessed["download_path"] == "/forum/dl.php?t={id}"
    assert guessed["page_download"] is True
    assert guess.guess_from_url(url)["download_path"] == "/forum/download.php?id={id}"  # the link alone


def test_a_page_link_to_another_host_or_without_a_number_keeps_the_guess(monkeypatch):
    from tow import guess

    url = "https://forum.example/forum/viewtopic.php?t=7"
    page = '<table class="attach"><tr><td><a href="https://cdn.example/dl.php?t=7">x</a></td></tr></table>'
    monkeypatch.setattr(guess, "_topic_page", lambda _url: page)
    assert guess.guess_site(url)["download_path"] == "/forum/download.php?id={id}"
    monkeypatch.setattr(guess, "_topic_page", lambda _url: "")
    assert guess.guess_site(url)["download_path"] == "/forum/download.php?id={id}"


def test_a_known_sites_guess_does_not_read_its_page(monkeypatch):
    from tow import guess

    def refuse(_url):
        raise AssertionError("a known site's rules are not replaced by its page")

    monkeypatch.setattr(guess, "_topic_page", refuse)
    assert guess.guess_site("https://nnmclub.to/forum/viewtopic.php?t=555")["name"] == "nnmclub"
