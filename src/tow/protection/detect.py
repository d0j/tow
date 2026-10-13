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
_PASSWORD = re.compile(r"<input\b[^>]*\btype\s*=\s*[\"']?password\b", re.IGNORECASE)
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


def classify(
    status: int,
    text: str,
    headers: Mapping[str, str] | None = None,
    *,
    maintenance_markers: Sequence[str] = (),
    torrent_wanted: bool = False,
    daily_limit: bool = False,
) -> Verdict:
    """Do not mistake normal Cloudflare-proxied pages or quoted scripts for challenges.

    ``daily_limit`` is an already identified quota page, not a guess based on the
    word "limit". Authentication needs a 401 or an actual short password form.
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
    if status == 401 or (
        (torrent_wanted or title in _LOGIN_TITLES)
        and len(plain) < 2000
        and (
            _PASSWORD.search(body)
            or title in _LOGIN_TITLES
            or plain in _LOGIN_TITLES
            or any(message in plain for message in _AUTH_MESSAGES)
        )
    ):
        return Verdict.LOGIN_NEEDED
    return Verdict.UNEXPECTED if torrent_wanted else Verdict.OK
