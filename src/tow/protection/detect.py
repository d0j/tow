"""Bounded, side-effect-free classification of answers instead of the requested page."""

from __future__ import annotations

import html
import re
from collections.abc import Mapping, Sequence
from enum import StrEnum


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


_SCAN_BYTES = 65536
_TAG_WINDOW = 8192  # one tag's attributes; a longer tag is not a form field TOW needs to see
_MARKUP = re.compile(r"<(?:!--|script\b|style\b|input\b)", re.IGNORECASE)
_TYPE_PASSWORD = re.compile(r"""(?:^|\s)type\s*=\s*(?:"\s*password\s*"|'\s*password\s*'|password(?=[\s/>]|$))""")


def _tag_end(page: str, start: int, limit: int) -> tuple[int, int]:
    """(the ``>`` closing the tag at ``start``, or -1; where reading stopped). A quoted value may
    hold a ``>``. Reading never goes back, so the whole page is read once."""
    at = start
    while at < limit:
        close = page.find(">", at, limit)
        if close < 0:
            return -1, limit
        quotes = [found for found in (page.find('"', at, close), page.find("'", at, close)) if found >= 0]
        if not quotes:
            return close, close + 1
        opened = min(quotes)
        ended = page.find(page[opened], opened + 1, limit)
        if ended < 0:
            return -1, limit
        at = ended + 1
    return -1, limit


def _has_password_field(page: str) -> bool:
    """Whether the bounded page (its first 64 KiB) has a real ``<input type=password>``, read as a
    browser reads it: comments and the text of scripts and styles are not markup, and a quoted
    attribute may hold a ``>``. One forward pass (an HTML parser re-read an unended tag on every
    ``<input`` of a hostile page: quadratic)."""
    page = page[:_SCAN_BYTES]
    lower = page.lower()
    at = 0
    while (found := _MARKUP.search(lower, at)) is not None:
        start, opener = found.start(), found.group()
        if opener == "<!--":
            end = lower.find("-->", start + 4)
            if end < 0:
                return False  # the rest of the page is a comment
            at = end + 3
        elif opener in {"<script", "<style"}:
            end = lower.find("</" + opener[1:], found.end())
            if end < 0:
                return False  # the rest of the page is script or style text
            at = end + 2
        else:
            end, at = _tag_end(page, found.end(), min(len(page), found.end() + _TAG_WINDOW))
            if end >= 0 and _TYPE_PASSWORD.search(lower, found.end(), end):
                return True
    return False


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
    a page of any length that answered a download; a password field on a removed topic's page
    is neither. The daily limit comes before all of them.
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
    plain = " ".join(html.unescape(re.sub(r"<[^<>]*>", " ", body)).split()).casefold()
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
    # A page saying the topic is gone may still carry the site's sign-in widget: the widget is no
    # sign-in page then, on a short page as on a long one. A sign-in title or message still is.
    password = not topic_removed and _has_password_field(body)
    # A download answered by a full site page with its sign-in form (a header widget, news
    # around it): the session expired, however long the page.
    if torrent_wanted and password:
        return Verdict.LOGIN_NEEDED
    if len(plain) < 2000 and (
        password
        or title in _LOGIN_TITLES
        or plain in _LOGIN_TITLES
        or any(message in plain for message in _AUTH_MESSAGES)
    ):
        return Verdict.LOGIN_NEEDED
    return Verdict.UNEXPECTED if torrent_wanted else Verdict.OK
