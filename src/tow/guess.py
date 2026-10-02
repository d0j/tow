"""New site settings from a pasted topic link: a known site's own rules (tow.trackers.presets),
then the shapes most trackers share (phpBB forums, /torrent/<id>, a number in the link)."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlparse

from tow.i18n import t
from tow.trackers import presets
from tow.trackers.presets import UrlParts


class GuessError(ValueError):
    """A safe, localized reason why a pasted topic link cannot be used."""

    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(t(key))


def canon_watch_url(url: str) -> str:
    """A site's link that is not its topic page (a CDN download link) -> the topic page."""
    return presets.canonical_url((url or "").strip())


_SECOND_LEVEL = frozenset({"co", "com", "org", "net", "gov", "ac", "edu", "msk", "spb"})


def _name_from_host(host: str) -> str:
    """N10: a known tracker brand from any label (kinozal.jumpingcrab.com -> kinozal),
    else the registrable domain (tracker.example.org -> example, x.example.co.uk ->
    example) - the first label used to give "d" for d.rutor.info."""
    brands = re.compile(rf"^(?:{'|'.join(presets.brands())})$", re.IGNORECASE)
    labels = [label for label in host.lower().removeprefix("www.").split(".") if label]
    brand = next((label for label in labels if brands.match(label)), "")
    if brand:
        core = brand
    elif len(labels) >= 3 and labels[-2] in _SECOND_LEVEL and len(labels[-1]) == 2:
        core = labels[-3]
    elif len(labels) >= 2:
        core = labels[-2]
    else:
        core = labels[0] if labels else ""
    core = re.sub(r"[^a-z0-9]+", "_", core).strip("_")
    return core or "site"


def _host_rx(host: str) -> str:
    h = host.lower().removeprefix("www.")
    return r"(?:www\.)?" + re.escape(h)


def _url_parts(url: str) -> UrlParts:
    """The pasted link split for the rules; a link TOW cannot watch is refused in plain words."""
    raw = (url or "").strip()
    if raw.lower().startswith("magnet:"):
        raise GuessError("guess.magnet")
    if not raw.startswith(("http://", "https://")):
        raise GuessError("guess.http_needed")
    p = urlparse(raw)
    host = (p.hostname or "").lower()
    if not host:
        raise GuessError("guess.no_host")
    try:
        port = p.port
    except ValueError as exc:
        raise GuessError("guess.bad_port") from exc
    authority = f"{host}:{port}" if port is not None else host
    return UrlParts(
        host=host,
        origin=f"{p.scheme}://{authority}",
        path=p.path or "/",
        query=parse_qs(p.query),
        raw_query=p.query,
        name=_name_from_host(host),
        host_rx=_host_rx(authority),
    )


def _forum_guess(parts: UrlParts) -> dict[str, Any] | None:
    """phpBB/TorrentPier forums: a dl.php, download.php or viewtopic.php link."""
    path, query, prefix = parts.path, parts.query, parts.prefix
    if re.search(r"/dl\.php$", path, re.IGNORECASE) and query.get("t"):
        return parts.phpbb(prefix, dl=f"{prefix}/dl.php?t={{id}}", page=False)
    if re.search(r"/(?:download|get)\.php$", path, re.IGNORECASE) and (query.get("id") or query.get("t")):
        if query.get("t") and not query.get("id"):
            return parts.phpbb(prefix, dl=f"{prefix}/download.php?t={{id}}", page=False)
        return parts.phpbb(prefix, dl=f"{prefix}/download.php?id={{id}}", page=True)
    if re.search(r"viewtopic\.php$", path, re.IGNORECASE) and query.get("t"):
        return parts.phpbb(prefix, dl=f"{prefix}/download.php?id={{id}}", page=True)
    if re.search(r"details\.php$", path, re.IGNORECASE) and query.get("id"):
        return parts.spec(
            url_regex=parts.rx(r"/details\.php\?id=(\d+)"),
            download_path="/download.php?id={id}",
            login_path="/takelogin.php",
            need_login=True,
        )
    return None


def _path_guess(parts: UrlParts) -> dict[str, Any] | None:
    """Sites with the topic number in the path: /download/torrent/<id>, /torrent/<id>."""
    if re.search(r"/download/torrent/(\d+)(?:/|$)", parts.path, re.IGNORECASE):
        return parts.spec(
            url_regex=parts.rx(r"/download/torrent/(\d+)(?:/.*)?"),
            download_path="/download/torrent/{id}",
        )
    if re.search(r"/torrent/(\d+)", parts.path):
        return parts.spec(url_regex=parts.rx(r"/torrent/(\d+)(?:/.*)?"), download_path="/download/{id}")
    return None


def _last_replaced(text: str, value: str) -> tuple[str, str]:
    """``text`` with the last ``value`` as ``{id}``, and the regex matching it."""
    head, _, tail = text.rpartition(value)
    return head + "{id}" + tail, re.escape(head) + r"(\d+)" + re.escape(tail)


def _number_guess(parts: UrlParts) -> dict[str, Any]:
    """N10: an explicit id parameter wins; otherwise the LAST number of the path (the first
    one was often a year or a category: /2024/serial-12345.html)."""
    path, raw_query = parts.path, parts.raw_query
    query_id = next(
        (values[0] for key in ("id", "t", "topic", "tid") if (values := parts.query.get(key)) and values[0].isdigit()),
        "",
    )
    path_numbers = list(re.finditer(r"\d{3,}", path))
    if query_id:
        where, num = "query", query_id
    elif path_numbers:
        where, num = "path", path_numbers[-1].group(0)
    elif query_numbers := list(re.finditer(r"\d{3,}", raw_query)):
        where, num = "query", query_numbers[-1].group(0)
    else:
        raise GuessError("guess.no_topic_number")
    if where == "path":
        templ, path_rx = _last_replaced(path, num)
        query_templ, query_rx = raw_query, re.escape(raw_query)
    else:
        templ, path_rx = path, re.escape(path)
        query_templ, query_rx = _last_replaced(raw_query, num)
    if raw_query:
        templ += "?" + query_templ
        path_rx += r"\?" + query_rx
    return parts.spec(url_regex=parts.rx(path_rx), download_path=templ)


def guess_from_url(url: str) -> dict[str, Any]:
    """Settings for a new site from one of its topic links (raises ValueError, in words)."""
    try:
        parts = _url_parts(url)
        return presets.guess(parts) or _forum_guess(parts) or _path_guess(parts) or _number_guess(parts)
    except GuessError:
        raise
    except ValueError as exc:
        # urlparse or a preset may reject malformed input with internal details. The form only
        # needs a safe reason, never the raw exception (which may include the pasted URL).
        raise GuessError("guess.invalid_url") from exc
