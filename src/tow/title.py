from __future__ import annotations

import functools
import logging
import re
from urllib.parse import unquote, urlparse

from tow import http as thttp
from tow.torrent import looks_like_torrent


@functools.cache
def _site_words() -> re.Pattern[str]:
    """Words that name a site in a page title: every known site (tow.trackers.presets), and
    the generic "torrent tracker". Built on first use: the tracker package imports this module."""
    from tow.trackers import presets

    return re.compile(rf"({'|'.join((*presets.brands(), r'torrent[- ]?tracker'))})", re.IGNORECASE)


_PHP = re.compile(r"\.php$", re.IGNORECASE)
_DOWNLOAD_PATH = re.compile(
    r"(?:/(?:download|dl|get)\.php$|/\.dl\./|/download/torrent/)",
    re.IGNORECASE,
)


# A lone surrogate (half a UTF-16 pair) is no text: no file or page can hold it.
_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def _clean(s: str) -> str:
    """Every title TOW takes from a page or a link passes here: one line of real text."""
    s = _LONE_SURROGATE.sub("�", unquote(s or "")).replace("\xa0", " ").strip()
    s = re.sub(r"\s+", " ", s)
    if s.lower().endswith(".torrent"):
        s = s[: -len(".torrent")].rstrip()
    return s[:180].strip(" -_|")


def _drop_site_bits(s: str) -> str:
    s = _clean(s)
    for sep in (" :: ", " | ", " / "):
        if sep in s:
            parts = [p.strip() for p in s.split(sep) if p.strip()]
            keep = [p for p in parts if not _site_words().search(p) or len(p) > 40]
            if keep:
                # N2: drop only the site's own name; "Сериал А / Show A [S02E01-12]" keeps both
                # names (picking the longest fragment lost the Russian title).
                s = sep.join(keep)
                break
    return _clean(s)


def _path(url: str) -> str:
    """The link's path; "" for a link the parser refuses ("http://[x/...": no address)."""
    try:
        return urlparse((url or "").strip()).path or ""
    except ValueError:
        return ""


def title_from_url_slug(url: str) -> str:
    parts = [unquote(x) for x in _path(url).split("/") if x]
    if not parts:
        return ""
    last = parts[-1]
    if _PHP.search(last):
        return ""
    if last.isdigit():
        return ""
    if not re.search(r"[A-Za-zА-Яа-яЁё]", last):
        return ""
    slug = last.replace("_", " ").replace("+", " ")
    return _drop_site_bits(slug)


# Where a topic page names itself: <title> and og:title in its head, the <h1> above the posts.
# BeautifulSoup reads only that part (a whole 4 MiB page of tags took it 9-12 s); a first <h1>
# that starts inside it is read to its end, up to a bounded distance.
_TITLE_HEAD_CHARS = 256 * 1024
_H1_TAIL_CHARS = 64 * 1024
_H1_START = re.compile(r"<h1[\s/>]", re.IGNORECASE)
_H1_END = re.compile(r"</h1\s*>", re.IGNORECASE)


def _title_part(html: str) -> str:
    if len(html) <= _TITLE_HEAD_CHARS:
        return html
    cut = _TITLE_HEAD_CHARS
    start = _H1_START.search(html, 0, _TITLE_HEAD_CHARS)
    end = _H1_END.search(html, start.end(), _TITLE_HEAD_CHARS + _H1_TAIL_CHARS) if start else None
    if end is not None:
        cut = max(cut, end.end())
    return html[:cut]


def title_from_html(html: str) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(_title_part(html or ""), "html.parser")
    h1 = soup.find("h1")
    if h1:
        got = _drop_site_bits(h1.get_text(" ", strip=True))
        if got:
            return got
    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content"):
        got = _drop_site_bits(str(og.get("content") or ""))
        if got:
            return got
    if soup.title and soup.title.string:
        return _drop_site_bits(str(soup.title.string))
    return ""


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9а-яё]+", "", (s or "").lower().replace("ё", "е"))


def title_is_placeholder(title: str | None, url: str) -> bool:
    t = (title or "").strip()
    u = (url or "").strip()
    if not t:
        return True
    if t == u or t.startswith(("http://", "https://")):
        return True
    slug = title_from_url_slug(u)
    if slug and _norm(t) == _norm(slug):
        return True
    path = unquote(_path(u))
    return bool(_norm(t) and _norm(t) in _norm(path) and not re.search(r"[а-яё]", t.lower()))


def looks_like_download_url(url: str) -> bool:
    path = _path(url)
    if path.lower().endswith(".torrent"):
        return True
    return bool(_DOWNLOAD_PATH.search(path))


def guess_topic_title(url: str, *, fetch: bool = True) -> str:
    from tow.guess import canon_watch_url

    url = canon_watch_url(url)
    page = _title_from_page(url) if fetch and not looks_like_download_url(url) else ""
    if page:
        return page
    return title_from_url_slug(url)


def _title_from_page(url: str) -> str:
    from tow.config import load_config
    from tow.http import MAX_HTML_RESPONSE_BYTES, get_limited
    from tow.http import client as http_client
    from tow.mirrors import origin_key
    from tow.trackers import load_trackers, match_tracker

    cfg = load_config()
    tracker = match_tracker(load_trackers(cfg), url)
    if tracker is None or origin_key(url) not in {origin_key(host) for host in tracker.spec.get("fetch_hosts") or []}:
        return ""
    ua = cfg.get("user_agent")
    try:
        from tow.config import as_bool

        with http_client(
            ua=ua,
            follow_redirects=False,
            public_only=not as_bool(cfg.get("allow_private_tracker_hosts")),
        ) as c:
            r = get_limited(c, url.strip(), max_bytes=MAX_HTML_RESPONSE_BYTES)
    except Exception as exc:  # noqa: BLE001 - a title guess never fails the add: the link's own words are used
        logging.getLogger("tow.title").warning("topic page not read for its title: %s", type(exc).__name__)
        return ""
    if r.status_code >= 300:
        return ""
    if looks_like_torrent(r.content):
        return ""
    head = r.content[:200].removeprefix(b"\xef\xbb\xbf").lstrip().lower()
    if "html" not in (r.headers.get("content-type") or "").lower() and not head.startswith((b"<!doctype", b"<html")):
        return ""
    return title_from_html(thttp.html_text(r))
