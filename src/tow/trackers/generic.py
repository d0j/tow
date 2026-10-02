from __future__ import annotations

import html
import logging
import re
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

import httpx

from tow import http as thttp
from tow.errors import Msg, TowError
from tow.jsonish import as_dict
from tow.mirrors import MirrorFetchError, has_available_host, origin_key, pick_and_get
from tow.store import load_secrets, persistence_lock, save_secrets
from tow.title import title_from_html
from tow.torrent import is_download_limit, looks_like_torrent, parse_magnet_hashes
from tow.trackers import presets

_LOG = logging.getLogger("tow.trackers")
MAX_TRACKER_REGEX_CHARS = 512
MAX_TRACKER_URL_CHARS = 4096
MAX_DOWNLOAD_PAGE_CHARS = thttp.MAX_HTML_RESPONSE_BYTES
_COOKIE_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_SECRET_METADATA_KEYS = {"username", "password", "cookies_by_origin", "browser_user_agent"}


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
        _LOG.warning("login answer not readable: %s: %s", type(exc).__name__, exc)
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


def validate_tracker_regex(value: str, *, label: str = "url", flags: int = 0) -> re.Pattern[str]:
    """``label``: which pattern it is, ``url`` (the topic link) or ``download`` (the download link)."""
    what = Msg("tracker.regex_label_download" if label == "download" else "tracker.regex_label_url")
    if not isinstance(value, str) or not value:
        raise TrackerSettingError("tracker.regex_required", what=what)
    if len(value) > MAX_TRACKER_REGEX_CHARS:
        raise TrackerSettingError("tracker.regex_too_long", what=what, limit=MAX_TRACKER_REGEX_CHARS)
    if regex_redos_risk(value):
        raise TrackerSettingError("tracker.regex_redos", what=what)
    try:
        return re.compile(value, flags)
    except re.error as exc:
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
        m = self._rx.match(candidate)
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
        form = login_form_data(self.spec, user, pw)
        pw_field = str((self.spec.get("login_form") or {}).get("pw_field") or "password")
        scoped = {}
        for host in hosts:
            if persist and not has_available_host(self.name, [host], ignore_cool=ignore_cool):
                continue
            try:
                with thttp.client(ua=ua, follow_redirects=False) as c:
                    response = c.post(host.rstrip("/") + path, data=form)
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
            from tow.config import load_config

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
        rx = self.spec.get("download_href_regex") or r"(?:download|dl)\.php\?(?:id|t)=(\d+)"
        pattern = validate_tracker_regex(rx, label="download", flags=re.IGNORECASE)
        m = pattern.search(html[:MAX_DOWNLOAD_PAGE_CHARS])
        return m.group(1) if m else None

    def _page_magnet(self, page: str) -> tuple[str, str] | None:
        candidates: dict[tuple[frozenset[str], frozenset[str]], tuple[str, str]] = {}
        for match in re.finditer(r"""href\s*=\s*["'](magnet:\?[^"']+)["']""", page, re.IGNORECASE):
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
        }
        try:
            return pick_and_get(self.name, hosts, path, **kw)
        except MirrorFetchError as exc:
            creds = (secrets.get("trackers") or {}).get(self.name) or {}
            if (
                exc.failure != "auth"
                or not persist
                or self.spec.get("browser_auth")
                or not (creds.get("username") and creds.get("password"))
                or not (self.spec.get("login_path") and self.spec.get("login_hosts"))
            ):
                raise
            refreshed = self._login(secrets, ua, persist=True, ignore_cool=ignore_cool)
            if refreshed == (cookies or {}):
                raise
            kw["cookies"] = refreshed or None
            return pick_and_get(self.name, hosts, path, **kw)

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
            topic_path = str(self.spec.get("topic_path") or "/forum/viewtopic.php?t={id}").format(id=tid)
            page, _host = self._get(
                hosts, topic_path, secrets, ua, cookies, torrent_only=False, ignore_cool=ignore_cool, persist=persist
            )
            if persist:
                cookies = self._cookie_jar(load_secrets())
            page_html = thttp.html_text(page)
            self._pages[tid] = (time.monotonic(), page_html)
            dlid = self._page_download_id(page_html)
            if not dlid:
                raise TrackerError("tracker.no_download_link", prefix=self.name)
            path = str(self.spec.get("download_path") or "/forum/download.php?id={id}").format(id=dlid)
        else:
            path = str(self.spec.get("download_path") or "/download/{id}").format(id=tid)
        r, _host = self._get(
            hosts, path, secrets, ua, cookies, torrent_only=True, ignore_cool=ignore_cool, persist=persist
        )
        if not looks_like_torrent(r.content):
            if is_download_limit(r.content):
                raise TrackerError("mirrors.tracker_daily_limit", tracker=self.name)
            raise TrackerError("tracker.not_torrent", prefix=self.name)
        return r.content

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
            return title_from_html(cached[1])
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
        return title_from_html(thttp.html_text(response))


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
