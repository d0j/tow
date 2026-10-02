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


def _clean(s: str) -> str:
    s = unquote(s or "").replace("\xa0", " ").strip()
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


def title_from_url_slug(url: str) -> str:
    p = urlparse((url or "").strip())
    parts = [unquote(x) for x in p.path.split("/") if x]
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


def title_from_html(html: str) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "html.parser")
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
    path = unquote(urlparse(u).path or "")
    return bool(_norm(t) and _norm(t) in _norm(path) and not re.search(r"[а-яё]", t.lower()))


def looks_like_download_url(url: str) -> bool:
    p = urlparse((url or "").strip())
    path = p.path or ""
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
        with http_client(ua=ua, follow_redirects=False) as c:
            r = get_limited(c, url.strip(), max_bytes=MAX_HTML_RESPONSE_BYTES)
    except Exception as exc:  # noqa: BLE001 - a title guess never fails the add: the link's own words are used
        logging.getLogger("tow.title").warning("topic page not read for its title: %s: %s", type(exc).__name__, exc)
        return ""
    if r.status_code >= 300:
        return ""
    if looks_like_torrent(r.content):
        return ""
    text = thttp.html_text(r) if "html" in (r.headers.get("content-type") or "").lower() else ""
    if not text:
        head = r.content[:200].lstrip().lower()
        if head.startswith((b"<!doctype", b"<html")):
            text = r.content.decode(r.encoding or "utf-8", "replace")
        else:
            return ""
    return title_from_html(text)
