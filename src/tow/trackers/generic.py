from __future__ import annotations

import bisect
import html
import logging
import re
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

import httpx
import regex as timed_regex

from tow import http as thttp
from tow.config import as_bool, load_config
from tow.errors import Msg, TowError
from tow.jsonish import as_dict
from tow.mirrors import (
    MirrorFetchError,
    has_available_host,
    login_recent,
    note_login,
    origin_key,
    pick_and_get,
    says_topic_removed,
)
from tow.store import load_secrets, persistence_lock, save_secrets
from tow.title import title_from_html
from tow.torrent import is_download_limit, looks_like_torrent, parse_magnet_hashes
from tow.trackers import presets

_LOG = logging.getLogger("tow.trackers")
MAX_TRACKER_REGEX_CHARS = 512
MAX_TRACKER_URL_CHARS = 4096
MAX_DOWNLOAD_PAGE_CHARS = thttp.MAX_HTML_RESPONSE_BYTES
MAX_URL_REGEX_SECONDS = 0.5
MAX_PAGE_REGEX_SECONDS = 2.0
_COOKIE_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_SECRET_METADATA_KEYS = {"username", "password", "cookies_by_origin", "browser_user_agent"}
# What a topic page shown to a guest (no session) has: a sign-in form, link or request.
_GUEST_MARKS = re.compile(
    r"""type\s*=\s*["']?password|login\.php|войдите|авторизуйтесь|зарегистрируйтесь|sign in|log in""",
    re.IGNORECASE,
)
# Page titles of a sign-in page (a site showing it instead of the topic).
_SIGN_IN_TITLES = frozenset(
    {"вход", "войти", "вход на сайт", "авторизация", "login", "log in", "sign in", "authorization"}
)


# A way to sign out: a link to the site's logout (login.php?logout=1, /logout.php ...): "log_?out"
# between an "href=" and the link's end. One regex for it read the rest of the link again from
# every "href=" inside it (minutes on a page of them); the three are found once instead.
_HREF = re.compile(r"""href\s*=\s*["']?""", re.IGNORECASE)
_LOGOUT = re.compile(r"log_?out", re.IGNORECASE)
_LINK_END = re.compile(r"""["'\s>]""")
_TAG = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9-]*)([^>]*)>")
_ATTR = re.compile(r"""(?:^|\s)(class|id)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""", re.IGNORECASE)
# The topic's own download block: TorrentPier's attachment table, the phpBB torrent table, a
# download box, a .torrent or magnet link styled as the site's own.
_OWN_BLOCK_CLASSES = frozenset({"attach", "bttbl", "dl-link", "dl-stub", "magnet-link"})
_OWN_BLOCK_IDS = frozenset({"download"})
# What the site's users wrote (posts, comments): a link there may be another topic's, a phrase
# there is not the site's message.
_USER_TEXT_CLASSES = frozenset({"post_body", "postbody", "post-body", "post", "comment", "comments"})
# How far (in tags) an element's end is looked for; an element not closed by then is its tag alone.
_MAX_ELEMENT_TAGS = 5000


def _tags(page: str) -> list[re.Match[str]]:
    """The page's tags. Searched only up to the last ``>``: past it no tag can end, and a
    broken ``<x`` there was scanned to the end of the page for every one (seconds on a page
    full of them)."""
    return list(_TAG.finditer(page, 0, page.rfind(">") + 1))


def _closing_tags(tags: list[re.Match[str]]) -> dict[int, int]:
    """Which tag closes which: opening tag's index -> its closing tag's index. One pass with a
    stack per tag name; a tag never closed (or closed too far away) has no entry."""
    open_by_name: dict[str, list[int]] = {}
    closes: dict[int, int] = {}
    for i, tag in enumerate(tags):
        stack = open_by_name.setdefault(tag.group(2).casefold(), [])
        if not tag.group(1):
            stack.append(i)
        elif stack:
            start = stack.pop()
            if i - start < _MAX_ELEMENT_TAGS:
                closes[start] = i
    return closes


def _elements(page: str, classes: frozenset[str], ids: frozenset[str]) -> list[tuple[int, int]]:
    """Where the outermost elements with one of ``classes`` (or ``ids``) are: (start, end)."""
    wanted = {f"class:{name}" for name in classes} | {f"id:{name}" for name in ids}
    tags = _tags(page[:MAX_DOWNLOAD_PAGE_CHARS])
    closes: dict[int, int] | None = None
    found: list[tuple[int, int]] = []
    taken_until = 0
    for i, tag in enumerate(tags):
        if tag.group(1) or tag.start() < taken_until:
            continue
        names: set[str] = set()
        for attr in _ATTR.finditer(tag.group(3)):
            value = (attr.group(2) or attr.group(3) or attr.group(4) or "").casefold()
            if attr.group(1).casefold() == "class":
                names.update(f"class:{word}" for word in value.split())
            else:
                names.add(f"id:{value.strip()}")
        if not names & wanted:
            continue
        if closes is None:
            closes = _closing_tags(tags)
        end = tags[closes[i]].end() if i in closes else tag.end()
        found.append((tag.start(), end))
        taken_until = end
    return found


def _without(page: str, spans: list[tuple[int, int]]) -> str:
    out, at = [], 0
    for start, end in spans:
        out.append(page[at:start])
        at = end
    out.append(page[at:])
    return " ".join(out)


def site_text(page: str) -> str:
    """The page without what its users wrote (posts, comments): the site's own words."""
    head = page[:MAX_DOWNLOAD_PAGE_CHARS]
    return _without(head, _elements(head, _USER_TEXT_CLASSES, frozenset()))


def link_text(page: str) -> str:
    """Where the topic's own .torrent or magnet link is: its download block, or - on a page
    without one - the site's own part of the page. A link in a post may be another topic's."""
    head = page[:MAX_DOWNLOAD_PAGE_CHARS]
    blocks = _elements(head, _OWN_BLOCK_CLASSES, _OWN_BLOCK_IDS)
    if blocks:
        return "\n".join(head[start:end] for start, end in blocks)
    return site_text(head)


def _has_logout_link(text: str) -> bool:
    logouts = [found.start() for found in _LOGOUT.finditer(text)]
    if not logouts:
        return False
    ends = [found.start() for found in _LINK_END.finditer(text)]
    for link in _HREF.finditer(text):
        start = link.end()
        logout = bisect.bisect_left(logouts, start)
        end = bisect.bisect_left(ends, start)
        if logout < len(logouts) and logouts[logout] < (ends[end] if end < len(ends) else len(text)):
            return True
    return False


def signed_in(page: str) -> bool:
    """The page is shown to a signed-in member: the site offers to sign out."""
    return _has_logout_link(site_text(page))


def guest_page(page: str) -> bool:
    """A page as a guest sees it: it asks to sign in and has no way to sign out."""
    own = site_text(page)
    return not _has_logout_link(own) and bool(_GUEST_MARKS.search(own))


def sign_in_page(page: str, title: str) -> bool:
    """The site's sign-in page, not a topic: a password field under a sign-in title."""
    return title.strip().casefold() in _SIGN_IN_TITLES and bool(
        re.search(r"""type\s*=\s*["']?password""", page[:MAX_DOWNLOAD_PAGE_CHARS], re.IGNORECASE)
    )


def magnet_infohash(value: str) -> str | None:
    hashes = parse_magnet_hashes(value)
    if hashes is None:
        return None
    btih, btmh = hashes
    return next(iter(btmh or btih))


def login_form_data(spec: dict[str, Any], user: str, pw: str) -> dict[str, str]:
    """POST body for a password login; field names come from the tracker's ``login_form``.

    Kinozal-style forms use ``username``/``password`` (the default); TorrentPier forums
    (rutracker, tapochek, unionpeer) use ``login_username``/``login_password`` plus a
    ``login`` submit value.
    """
    form = as_dict(spec.get("login_form"))
    data = {str(k): str(v) for k, v in (form.get("extra") or {}).items()} if isinstance(form.get("extra"), dict) else {}
    data[str(form.get("user_field") or "username")] = user
    data[str(form.get("pw_field") or "password")] = pw
    return data


def login_form_came_back(response: Any, pw_field: str) -> bool:
    """True when the login POST answered with a page that still asks for the password."""
    if getattr(response, "status_code", 0) != 200:
        return False
    try:
        body = thttp.html_text(response)[:200_000]
    except (LookupError, ValueError, TypeError) as exc:  # a page that cannot be decoded asks for nothing
        _LOG.warning("login answer not readable: %s", type(exc).__name__)
        return False
    return bool(
        re.search(
            r"""type\s*=\s*["']?password\b|name\s*=\s*["']?""" + re.escape(pw_field) + r"""["'\s>]""",
            body,
            re.IGNORECASE,
        )
    )


def regex_redos_risk(pattern: str) -> bool:
    """True for the classic catastrophic-backtracking shapes: ``(a+)+``, ``(a*)*``, ``(a|aa)*``.

    Python's ``re`` has no timeout and these patterns run on untrusted tracker HTML, so
    a group that itself repeats (inner quantifier or alternation) may not be repeated
    again with ``+``, ``*`` or ``{n,}``. An optional group (``(...)?``) is fine.
    """
    stack: list[bool] = []  # per open group: does it contain a quantifier or "|"?
    i, n = 0, len(pattern)
    while i < n:
        ch = pattern[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "[":  # character class: skip to its closing bracket
            i += 1
            if i < n and pattern[i] == "^":
                i += 1
            if i < n and pattern[i] == "]":
                i += 1
            while i < n and pattern[i] != "]":
                i += 2 if pattern[i] == "\\" else 1
            i += 1
            continue
        if ch == "(":
            stack.append(False)
        elif ch == ")":
            risky_inside = stack.pop() if stack else False
            rest = pattern[i + 1 :]
            repeated = rest[:1] in {"+", "*"} or bool(re.match(r"\{\d*,\d*\}", rest))
            if risky_inside and repeated:
                return True
            if stack and risky_inside:
                stack[-1] = True
        elif stack and (ch in "+*|" or (ch == "{" and re.match(r"\{\d*,?\d*\}", pattern[i:]))):
            stack[-1] = True
        i += 1
    return False


class TrackerError(TowError, RuntimeError):
    """A site did not give what TOW needs (its text: ``tracker.*`` in the language files)."""


class TrackerSettingError(TowError, ValueError):
    """A site's settings (a pattern, a path) or a link cannot be used."""


def validate_tracker_regex(value: str, *, label: str = "url", flags: int = 0) -> timed_regex.Pattern[str]:
    """``label``: which pattern it is, ``url`` (the topic link) or ``download`` (the download link)."""
    what = Msg("tracker.regex_label_download" if label == "download" else "tracker.regex_label_url")
    if not isinstance(value, str) or not value:
        raise TrackerSettingError("tracker.regex_required", what=what)
    if len(value) > MAX_TRACKER_REGEX_CHARS:
        raise TrackerSettingError("tracker.regex_too_long", what=what, limit=MAX_TRACKER_REGEX_CHARS)
    if regex_redos_risk(value):
        raise TrackerSettingError("tracker.regex_redos", what=what)
    try:
        return timed_regex.compile(value, flags)
    except timed_regex.error as exc:
        raise TrackerSettingError("tracker.regex_invalid", what=what) from exc


class GenericHttpTracker:
    # A topic page fetched for its download link also gives the title: one request, not two.
    PAGE_REUSE_SEC = 120.0

    def __init__(self, name: str, spec: dict[str, Any]) -> None:
        self.name = name
        # What TOW knows about a site of this name (its search link, a daily download limit,
        # a browser login) unless the site's own settings say otherwise.
        known = presets.defaults(name)
        self.spec = {**known, **spec} if known else spec
        self._pages: dict[str, tuple[float, str]] = {}
        self._rx = validate_tracker_regex(spec["url_regex"], label="url")

    def parse_id(self, url: str) -> str | None:
        candidate = url.strip()
        if len(candidate) > MAX_TRACKER_URL_CHARS:
            return None
        try:
            m = self._rx.match(candidate, timeout=MAX_URL_REGEX_SECONDS)
        except TimeoutError as exc:
            raise TrackerSettingError("tracker.regex_timeout", what=Msg("tracker.regex_label_url")) from exc
        return m.group(1) if m else None

    def _cookie_jar(self, secrets: dict[str, Any]) -> dict[str, dict[str, str]]:
        raw = (secrets.get("trackers") or {}).get(self.name) or {}
        names = list(self.spec.get("cookie_names") or [])

        def allowed(values: dict[str, Any]) -> dict[str, str]:
            out = {}
            for key, value in values.items():
                if (
                    key in _SECRET_METADATA_KEYS
                    or not isinstance(key, str)
                    or not _COOKIE_NAME.fullmatch(key)
                    or not isinstance(value, str)
                    or not value
                ):
                    continue
                if not names or key in names or key in ("uid", "pass"):
                    out[key] = value
            return out

        out: dict[str, dict[str, str]] = {}
        scoped = raw.get("cookies_by_origin")
        if isinstance(scoped, dict):
            for origin, values in scoped.items():
                if not isinstance(origin, str) or origin_key(origin) != origin or not isinstance(values, dict):
                    continue
                cookies = allowed(values)
                if cookies:
                    out[origin] = cookies

        return out

    def _effective_ua(self, secrets: dict[str, Any], fallback: str | None) -> str | None:
        tracker = (secrets.get("trackers") or {}).get(self.name) or {}
        browser_ua = tracker.get("browser_user_agent")
        return str(browser_ua) if isinstance(browser_ua, str) and browser_ua.strip() else fallback

    def _login(
        self, secrets: dict[str, Any], ua: str | None, *, persist: bool = True, ignore_cool: bool = False
    ) -> dict[str, dict[str, str]]:
        # Browser-auth trackers require a user-operated challenge (NNMClub
        # currently uses Turnstile). A raw password POST cannot satisfy it and
        # only creates misleading failed-login attempts.
        if self.spec.get("browser_auth"):
            return self._cookie_jar(secrets)
        creds = (secrets.get("trackers") or {}).get(self.name) or {}
        user, pw = creds.get("username"), creds.get("password")
        path = self.spec.get("login_path") or ""
        hosts = list(self.spec.get("login_hosts") or [])
        if not (user and pw and path and hosts):
            return self._cookie_jar(secrets)
        if not path.startswith("/") or path.startswith("//") or "\\" in path:
            raise TrackerSettingError("tracker.bad_login_path", prefix=self.name)
        if persist and not ignore_cool and login_recent(self.name):
            # Tried a moment ago (by this check or an earlier one): what that saved is used and
            # the password is not sent again. A check the owner starts (ignore_cool) logs in.
            return self._cookie_jar(load_secrets())
        if persist:
            note_login(self.name)
        form = login_form_data(self.spec, user, pw)
        pw_field = str((self.spec.get("login_form") or {}).get("pw_field") or "password")
        scoped = {}
        for host in hosts:
            if persist and not has_available_host(self.name, [host], ignore_cool=ignore_cool):
                continue
            try:
                with thttp.client(
                    ua=ua,
                    follow_redirects=False,
                    public_only=not as_bool(load_config().get("allow_private_tracker_hosts")),
                ) as c:
                    # Read like a topic page: at most MAX_HTML_RESPONSE_BYTES, never a body of any size.
                    response = thttp.request_limited(
                        c, "POST", host.rstrip("/") + path, max_bytes=thttp.MAX_HTML_RESPONSE_BYTES, data=form
                    )
                    if response.status_code >= 400 or login_form_came_back(response, pw_field):
                        # A login page shown again means the credentials were refused; its
                        # cookies are guest cookies and must not replace working ones.
                        continue
                    jar = {k: v for k, v in c.cookies.items() if v}
            except Exception as exc:  # noqa: BLE001 - one host failing to log in: the next one is tried
                # Only the error's type: its text may carry the login URL's query.
                _LOG.warning("%s: login at %s failed: %s", self.name, origin_key(host), type(exc).__name__)
                continue
            names = list(self.spec.get("cookie_names") or [])
            got = jar if not names else {n: jar[n] for n in names if jar.get(n)}
            origin = origin_key(host)
            if not got or not origin:
                continue
            scoped[origin] = got
        if persist and scoped:
            with persistence_lock():
                if self.name not in (load_config().get("trackers") or {}):
                    return {**self._cookie_jar(secrets), **scoped}
                s = load_secrets()
                tracker = (s.get("trackers") or {}).get(self.name)
                if not isinstance(tracker, dict) or tracker.get("username") != user or tracker.get("password") != pw:
                    return {**self._cookie_jar(secrets), **scoped}
                tracker.setdefault("cookies_by_origin", {}).update(scoped)
                for key in names:
                    tracker.pop(key, None)
                save_secrets(s)
                return self._cookie_jar(s)
        return {**self._cookie_jar(secrets), **scoped}

    def _page_download_id(self, html: str) -> str | None:
        """The topic's own .torrent id: from its download block (``link_text``), never from a
        post that links another topic's torrent."""
        rx = self.spec.get("download_href_regex") or r"(?:download|dl)\.php\?(?:id|t)=(\d+)"
        pattern = validate_tracker_regex(rx, label="download", flags=re.IGNORECASE)
        try:
            found = dict.fromkeys(m.group(1) for m in pattern.finditer(link_text(html), timeout=MAX_PAGE_REGEX_SECONDS))
        except TimeoutError as exc:
            raise TrackerSettingError("tracker.regex_timeout", what=Msg("tracker.regex_label_download")) from exc
        if len(found) > 1:
            # The description may link another topic's torrent (an earlier season): the first
            # link on the page is not necessarily this topic's own.
            raise TrackerError("tracker.download_link_ambiguous", prefix=self.name)
        return next(iter(found), None)

    def _page_magnet(self, page: str) -> tuple[str, str] | None:
        candidates: dict[tuple[frozenset[str], frozenset[str]], tuple[str, str]] = {}
        for match in re.finditer(r"""href\s*=\s*["'](magnet:\?[^"']+)["']""", link_text(page), re.IGNORECASE):
            magnet = html.unescape(match.group(1))
            identity = parse_magnet_hashes(magnet)
            infohash = magnet_infohash(magnet)
            if identity is not None and infohash:
                candidates.setdefault(identity, (magnet, infohash))
        if len(candidates) > 1:
            raise TrackerError("tracker.magnet_ambiguous", prefix=self.name)
        return next(iter(candidates.values()), None)

    def _get(
        self,
        hosts: list[str],
        path: str,
        secrets: dict[str, Any],
        ua: str | None,
        cookies: dict[str, dict[str, str]] | None,
        torrent_only: bool,
        ignore_cool: bool = False,
        persist: bool = True,
    ) -> tuple[httpx.Response, str]:
        kw: dict[str, Any] = {
            "cookies": cookies or None,
            "ua": ua,
            "fail_threshold": int(self.spec.get("fail_threshold") or 3),
            "cooldown_sec": int(self.spec.get("cooldown_sec") or 3600),
            "ok": (lambda resp: looks_like_torrent(resp.content)) if torrent_only else None,
            "ignore_cool": ignore_cool,
            "persist": persist,
            "max_bytes": (thttp.MAX_TORRENT_RESPONSE_BYTES if torrent_only else thttp.MAX_HTML_RESPONSE_BYTES),
            "allowed_redirect_origins": (list(self.spec.get("download_redirect_hosts") or []) if torrent_only else []),
            "public_only": not as_bool(load_config().get("allow_private_tracker_hosts")),
            "download_limit": torrent_only and as_bool(self.spec.get("download_limit")),
        }
        try:
            return pick_and_get(self.name, hosts, path, **kw)
        except MirrorFetchError as exc:
            if exc.failure != "auth":
                raise
            refreshed = self._relogin(secrets, ua, cookies, ignore_cool=ignore_cool, persist=persist)
            if refreshed is None:
                raise
            kw["cookies"] = refreshed or None
            return pick_and_get(self.name, hosts, path, **kw)

    def _relogin(
        self,
        secrets: dict[str, Any],
        ua: str | None,
        cookies: dict[str, dict[str, str]] | None,
        *,
        ignore_cool: bool,
        persist: bool,
    ) -> dict[str, dict[str, str]] | None:
        """The site refused the session: log in again with the saved password. None when that
        is not possible here (a preview, no password, a browser login) or gives nothing new."""
        creds = (secrets.get("trackers") or {}).get(self.name) or {}
        if (
            not persist
            or self.spec.get("browser_auth")
            or not (creds.get("username") and creds.get("password"))
            or not (self.spec.get("login_path") and self.spec.get("login_hosts"))
        ):
            return None
        refreshed = self._login(secrets, ua, persist=True, ignore_cool=ignore_cool)
        return None if refreshed == (cookies or {}) else refreshed

    def fetch_torrent(
        self,
        url: str,
        secrets: dict[str, Any],
        ua: str | None,
        *,
        ignore_cool: bool = False,
        persist: bool = True,
    ) -> bytes:
        ua = self._effective_ua(secrets, ua)
        tid = self.parse_id(url)
        if not tid:
            raise TrackerSettingError("tracker.bad_url", prefix=self.name)
        cookies = self._cookie_jar(secrets)
        hosts = list(self.spec.get("fetch_hosts") or [])
        if (
            persist
            and self.spec.get("login_hosts")
            and self.spec.get("login_path")
            and not cookies
            and has_available_host(self.name, hosts, ignore_cool=ignore_cool)
        ):
            cookies = self._login(secrets, ua, persist=persist, ignore_cool=ignore_cool)
        if self.spec.get("page_download"):
            dlid, cookies = self._download_id_from_page(
                tid, hosts, secrets, ua, cookies, ignore_cool=ignore_cool, persist=persist
            )
            path = str(self.spec.get("download_path") or "/forum/download.php?id={id}").format(id=dlid)
        else:
            path = str(self.spec.get("download_path") or "/download/{id}").format(id=tid)
        r, _host = self._get(
            hosts, path, secrets, ua, cookies, torrent_only=True, ignore_cool=ignore_cool, persist=persist
        )
        if not looks_like_torrent(r.content):
            if as_bool(self.spec.get("download_limit")) and is_download_limit(r.content):
                raise TrackerError("mirrors.tracker_daily_limit", tracker=self.name)
            raise TrackerError("tracker.not_torrent", prefix=self.name)
        return r.content

    def _download_id_from_page(
        self,
        tid: str,
        hosts: list[str],
        secrets: dict[str, Any],
        ua: str | None,
        cookies: dict[str, dict[str, str]],
        *,
        ignore_cool: bool,
        persist: bool,
    ) -> tuple[str, dict[str, dict[str, str]]]:
        """The torrent's id from the topic page (TorrentPier forums: tapochek, unionpeer), and
        the cookies to download it with.

        These sites show an expired session the page as to a guest - 200, without the download
        link - so that page counts as a refused session: the saved password logs in once more.
        Nothing on a guest page is believed (a post may link another topic's torrent or say
        "not found"); a member's page without the topic's own link has a layout TOW does not
        know."""
        topic_path = str(self.spec.get("topic_path") or "/forum/viewtopic.php?t={id}").format(id=tid)
        for attempt in range(2):
            page, _host = self._get(
                hosts, topic_path, secrets, ua, cookies, torrent_only=False, ignore_cool=ignore_cool, persist=persist
            )
            if persist:
                # A login during the fetch saved its session: the download goes with it.
                cookies = self._cookie_jar(load_secrets()) or cookies
            page_html = thttp.html_text(page)
            self._pages[tid] = (time.monotonic(), page_html)
            if guest_page(page_html):
                refreshed = (
                    None if attempt else self._relogin(secrets, ua, cookies, ignore_cool=ignore_cool, persist=persist)
                )
                if refreshed is None:
                    break
                cookies = refreshed
                continue
            if says_topic_removed(site_text(page_html)):
                raise MirrorFetchError("mirrors.topic_removed", failure="gone")
            dlid = self._page_download_id(page_html)
            if dlid:
                return dlid, cookies
            if signed_in(page_html):
                raise TrackerError("tracker.page_not_understood", cls="tracker", prefix=self.name)
            break
        raise TrackerError("tracker.no_download_link", prefix=self.name)

    def fetch_magnet(
        self,
        url: str,
        secrets: dict[str, Any],
        ua: str | None,
        *,
        ignore_cool: bool = False,
        persist: bool = True,
    ) -> tuple[str, str]:
        """Return a validated public magnet fallback from the topic page."""
        ua = self._effective_ua(secrets, ua)
        tid = self.parse_id(url)
        if not tid:
            raise TrackerSettingError("tracker.bad_url", prefix=self.name)
        if self.spec.get("topic_path"):
            path = str(self.spec["topic_path"]).format(id=tid)
        else:
            parsed = urlparse(url)
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
        response, _host = self._get(
            list(self.spec.get("fetch_hosts") or []),
            path,
            secrets,
            ua,
            self._cookie_jar(secrets),
            torrent_only=False,
            ignore_cool=ignore_cool,
            persist=persist,
        )
        result = self._page_magnet(thttp.html_text(response))
        if result is None:
            raise TrackerError("tracker.no_magnet", prefix=self.name)
        return result

    def fetch_title(
        self,
        url: str,
        secrets: dict[str, Any],
        ua: str | None,
        *,
        ignore_cool: bool = False,
        persist: bool = True,
    ) -> str:
        """Read the current tracker page title without changing the topic name."""
        ua = self._effective_ua(secrets, ua)
        tid = self.parse_id(url)
        if not tid:
            raise TrackerSettingError("tracker.bad_url", prefix=self.name)
        cookies = self._cookie_jar(secrets)
        hosts = list(self.spec.get("fetch_hosts") or [])
        if (
            persist
            and self.spec.get("login_hosts")
            and self.spec.get("login_path")
            and not cookies
            and has_available_host(self.name, hosts, ignore_cool=ignore_cool)
        ):
            cookies = self._login(secrets, ua, persist=persist, ignore_cool=ignore_cool)
        cached = self._pages.pop(tid, None)
        if cached and time.monotonic() - cached[0] < self.PAGE_REUSE_SEC:
            return self._page_title(cached[1])
        if self.spec.get("topic_path"):
            path = str(self.spec["topic_path"]).format(id=tid)
        else:
            parsed = urlparse(url)
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
        response, _host = self._get(
            hosts,
            path,
            secrets,
            ua,
            cookies,
            torrent_only=False,
            ignore_cool=ignore_cool,
            persist=persist,
        )
        return self._page_title(thttp.html_text(response))

    def _page_title(self, page: str) -> str:
        """The topic's title on its page; a sign-in page shown instead has none (its "Sign in"
        would become the topic's title). A Cloudflare check page never gets here (pick_and_get)."""
        title = title_from_html(page)
        if sign_in_page(page, title):
            raise TrackerError("tracker.sign_in_page", cls="tracker_auth", prefix=self.name)
        return title


def load_trackers(cfg: Mapping[str, Any]) -> dict[str, GenericHttpTracker]:
    out: dict[str, GenericHttpTracker] = {}
    for name, spec in (cfg.get("trackers") or {}).items():
        out[name] = GenericHttpTracker(name, spec)
    return out


def match_tracker(trackers: dict[str, GenericHttpTracker], url: str) -> GenericHttpTracker | None:
    for tracker in trackers.values():
        if tracker.parse_id(url):
            return tracker
    return None
