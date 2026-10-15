"""Bounded, side-effect-free classification of answers instead of the requested page."""

from __future__ import annotations

import html
import re
from collections.abc import Mapping, Sequence
from enum import StrEnum
from html.parser import HTMLParser


class Verdict(StrEnum):
    OK = "ok"
    CHALLENGE = "cloudflare"
    INTERACTIVE = "interactive"
    BLOCKED = "blocked"
    RATE_LIMITED = "rate_limited"
    ORIGIN_DOWN = "origin_down"
    MAINTENANCE = "maintenance"
    LOGIN_NEEDED = "auth"
    DAILY_LIMIT = "quota"
    UNEXPECTED = "unexpected"


_TITLE = re.compile(r"<title\b[^>]*>([^<]{0,200})</title>", re.IGNORECASE)
_CF_ERROR = re.compile(r"\b(?:error(?:\s+code)?|код\s+ошибки)\s*:?\s*(1020|1015)\b", re.IGNORECASE)
_CHALLENGE_TITLES = frozenset({"just a moment...", "just a moment…"})
_LOGIN_TITLES = frozenset({"sign in", "login", "log in", "вход", "авторизация"})
_AUTH_MESSAGES = (
    "please sign in",
    "please log in",
    "пожалуйста, войдите",
    "необходимо войти",
    "для скачивания необходимо авторизоваться",
)
_MAINTENANCE = ("technical works in progress", "under maintenance", "ведутся технические работы", "технические работы")


class _PasswordField(HTMLParser):
    """Finds a real ``<input type=password>`` as a browser reads the page: comments and the text
    of scripts are not markup, and a quoted attribute may hold a ``>``. One linear pass; a
    regex re-read the rest of the page from every unended ``<input``."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.found = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "input" and any(
            name == "type" and (value or "").strip().casefold() == "password" for name, value in attrs
        ):
            self.found = True

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)


def _has_password_field(page: str) -> bool:
    """Whether the bounded page (its first 64 KiB) has a password field."""
    reader = _PasswordField()
    try:
        reader.feed(page[:65536])
        reader.close()
    except AssertionError, ValueError:  # markup the parser gives up on: what it read so far counts
        pass
    return reader.found


def classify(
    status: int,
    text: str,
    headers: Mapping[str, str] | None = None,
    *,
    maintenance_markers: Sequence[str] = (),
    torrent_wanted: bool = False,
    daily_limit: bool = False,
    topic_removed: bool = False,
) -> Verdict:
    """Do not mistake normal Cloudflare-proxied pages or quoted scripts for challenges.

    ``daily_limit`` is an already identified quota page, not a guess based on the
    word "limit"; ``topic_removed`` a page already identified as saying the topic does not
    exist. Authentication needs a 401, an actual short sign-in page, or a password field on
    a page of any length that answered a download.
    """
    body = text[:65536]
    h = {str(key).casefold(): str(value).casefold() for key, value in (headers or {}).items()}
    found = _TITLE.search(body[:8192])
    title = " ".join(html.unescape(found.group(1)).split()).casefold() if found else ""
    cloudflare = "cloudflare" in h.get("server", "") or "cf-ray" in h or "cloudflare" in title
    code = _CF_ERROR.search(body) if cloudflare and status >= 400 else None
    if code and code[1] == "1020":
        return Verdict.BLOCKED
    if status == 429 or (code and code[1] == "1015"):
        return Verdict.RATE_LIMITED
    if 520 <= status <= 526:
        return Verdict.ORIGIN_DOWN
    if h.get("cf-mitigated", "").strip() == "challenge":
        return Verdict.CHALLENGE
    if status in {403, 503} and title in _CHALLENGE_TITLES:
        return Verdict.CHALLENGE
    if status in {403, 503} and title == "attention required! | cloudflare":
        return Verdict.INTERACTIVE
    # Titles and posts can describe a film about maintenance. Require a short
    # standalone page, and do not treat a phrase inside a longer topic title as proof.
    plain = " ".join(html.unescape(re.sub(r"<[^>]*>", " ", body)).split()).casefold()
    markers = (*_MAINTENANCE, *maintenance_markers)
    if (
        len(body) < 8192
        and len(plain) < 2000
        and any(
            title == marker.casefold() or (not title and marker.casefold() in plain) for marker in markers if marker
        )
    ):
        return Verdict.MAINTENANCE
    if daily_limit:
        return Verdict.DAILY_LIMIT
    if status == 401:
        return Verdict.LOGIN_NEEDED
    if not (torrent_wanted or title in _LOGIN_TITLES):
        return Verdict.OK
    password = _has_password_field(body)
    # A download answered by a full site page with its sign-in form (a header widget, news
    # around it): the session expired, however long the page. A removed topic says so first.
    if torrent_wanted and password and not topic_removed:
        return Verdict.LOGIN_NEEDED
    if len(plain) < 2000 and (
        password
        or title in _LOGIN_TITLES
        or plain in _LOGIN_TITLES
        or any(message in plain for message in _AUTH_MESSAGES)
    ):
        return Verdict.LOGIN_NEEDED
    return Verdict.UNEXPECTED if torrent_wanted else Verdict.OK
