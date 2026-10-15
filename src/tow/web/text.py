"""Texts in the page's language: ``t`` (a catalog text), ``tm`` (one with its markup), sizes,
messages as sentences and times (``short_time``, the one time rule).

Outside a request (a test, a background thread) the language is the owner's.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from contextlib import suppress
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from markupsafe import Markup, escape

from tow import clock, i18n
from tow.log import owner_language


def t(key: str, /, **params: Any) -> str:
    """A text in the page's language: ``t("web.common.saved")``, ``t(key, name=value)``;
    templates use the ``t`` global."""
    return i18n.translate(key, owner_language(), **params)


# Markup a translation may carry (``tm``): these tags, and a link whose address is a value.
_MARKUP_TAG = re.compile(r"&lt;(/?)(b|strong|em|i|code|kbd)&gt;")
_MARKUP_LINK = re.compile(r"&lt;a href=&#34;\{([a-z_][a-z0-9_]*)\}&#34;&gt;")
_SAFE_HREF = re.compile(r"(?:/(?!/)|#|https://)[^\s\"'<>`]*")


def tm(key: str, /, **params: Any) -> Markup:
    """A text with markup in the page's language: one sentence with its bold, code and links, so a
    translation may reorder it. The catalog text is escaped first; then only ``<b>``, ``<strong>``,
    ``<em>``, ``<i>``, ``<code>``, ``<kbd>`` and ``<a href="{name}">`` come back, the link's
    address being a value (a path, ``#…`` or ``https://…``). Values are escaped too."""
    lang = owner_language()
    plural = {"n": params["n"]} if "n" in params else {}
    text = str(escape(i18n.translate(key, lang, **plural)))

    def link(match: re.Match[str]) -> str:
        href = str(params.get(match.group(1)) or "")
        return f'<a href="{escape(href) if _SAFE_HREF.fullmatch(href) else "#"}">'

    text = _MARKUP_LINK.sub(link, text).replace("&lt;/a&gt;", "</a>")
    text = _MARKUP_TAG.sub(r"<\1\2>", text)
    return Markup(i18n.fill(text, {name: str(escape(value)) for name, value in params.items()}))


_FIRST_WORD = re.compile(r"\w[\w.+-]*")


def sentence(text: str, names: Iterable[str] = ()) -> str:
    """``text`` said on its own, as a sentence: its first letter upper-case ("saved" -> "Saved";
    catalog texts that are also the end of "{subject} — {result}" start in lower case). A text that
    starts with a name keeps its spelling: a word with a capital inside it ("qBittorrent",
    "macOS"), a dot, a digit or "_" in it (a host, a site key), or one of ``names`` (a site, a
    client or a messenger as the settings call it: "nnmclub", "ntfy")."""
    word = _FIRST_WORD.match(text)
    if word is None or not text[:1].islower():
        return text
    first = word.group(0)
    inner_capital = any(letter.isupper() for letter in first[1:])
    if not first.isalpha() or inner_capital or first.casefold() in {str(name).casefold() for name in names}:
        return text
    return text[:1].upper() + text[1:]


# A time TOW 1.24 and before wrote out in the page language of that moment, with its zone:
# "07.10.2026 12:16:13 UTC+03:00", "2026-10-07 12:16:13 MSK UTC+03:00".
_WRITTEN_OUT = re.compile(r"(?P<when>.+?) (?:[A-Z]{2,5} )?UTC(?:(?P<sign>[+-])(?P<hours>\d{2}):(?P<minutes>\d{2}))?")
# A time "to come" is more than this ahead: a time of this minute is now, not a scheduled one.
_NOW_SLACK = timedelta(minutes=1)


def moment_of(value: Any) -> datetime | None:
    """``value`` as an aware time, or None: a datetime, a Unix time in seconds (a number or a text
    of digits, as a message keeps it), an ISO text (one without a zone is the local time an old
    TOW wrote) or a time written out by TOW 1.24 and before. Zero, a bool or anything else: None."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, datetime):
        return clock.parse_timestamp(value)
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
        with suppress(TypeError, ValueError, OverflowError, OSError):
            seconds = float(value)
            return datetime.fromtimestamp(seconds, UTC) if seconds > 0 else None
        return None
    text = str(value).strip()
    if not text:
        return None
    with suppress(TypeError, ValueError):
        return clock.parse_timestamp(text)
    match = _WRITTEN_OUT.fullmatch(text)
    if match is None:
        return None
    minutes = int(match["hours"] or 0) * 60 + int(match["minutes"] or 0)
    zone = timezone(timedelta(minutes=-minutes if match["sign"] == "-" else minutes))
    for code in i18n.codes():
        with suppress(ValueError):
            return datetime.strptime(match["when"], i18n.datetime_pattern(code)).replace(tzinfo=zone)
    return None


def utc_offset(moment: datetime) -> str:
    """The UTC offset of an aware time: "UTC+02:00", "UTC-04:00", "UTC" for none."""
    offset = moment.utcoffset()
    seconds = int(offset.total_seconds()) if offset is not None else 0
    if not seconds:
        return "UTC"
    hours, rest = divmod(abs(seconds), 3600)
    return f"UTC{'+' if seconds > 0 else '-'}{hours:02d}:{rest // 60:02d}"


def short_time(value: Any, *, now: datetime | None = None) -> Markup:
    """A time as every page writes it (the one time rule): today "20:31"; another day of this
    year "15 Oct, 20:31" / "15 окт, 20:31"; another year "15 Oct 2025, 20:31"; a time still to
    come (a scheduled check, a retry) always with its date. 24 h, in TOW's own zone (its
    computer's, ``tow.clock.local_zone``), whatever zone the browser is in. A ``<time>`` whose
    ``datetime`` is the ISO time and whose tooltip is the full time with seconds and the UTC offset;
    ``data-short`` lets the page script write it again after midnight (app.js, ``towTime``, has
    the same rule). A value ``moment_of`` reads no time from is shown as it is; none, as nothing.
    Templates: ``{{ value|short_time }}`` or ``{{ short_time(value) }}``."""
    moment = moment_of(value)
    if moment is None:
        text = "" if value is None or isinstance(value, (bool, int, float)) else str(value).strip()
        return Markup(escape(text))
    zone = clock.local_zone()
    moment = moment.astimezone(zone)
    today = (now or clock.machine_now()).astimezone(zone)
    lang = owner_language()
    at = moment.strftime("%H:%M")
    if moment.date() == today.date() and moment <= today + _NOW_SLACK:
        shown = at
    else:
        month = i18n.translate(f"time.month_short.{moment.month}", lang)
        key = "time.this_year" if moment.year == today.year else "time.other_year"
        shown = i18n.translate(key, lang, day=moment.day, month=month, year=moment.year, time=at)
    return Markup('<time datetime="{}" title="{}" data-short>{}</time>').format(
        moment.isoformat(timespec="seconds"),
        f"{i18n.format_datetime(moment, lang)} {utc_offset(moment)}",
        shown,
    )


def zone_minutes(seconds_ago: float = 0) -> int:
    """TOW's offset from UTC in minutes, now or ``seconds_ago`` (the page scripts' ``towTime``
    writes times in TOW's zone, not the browser's: summer time is told by two of them)."""
    moment = datetime.fromtimestamp(datetime.now(UTC).timestamp() - seconds_ago, UTC).astimezone(clock.local_zone())
    offset = moment.utcoffset()
    return int(offset.total_seconds() // 60) if offset is not None else 0


def format_bytes(value: Any) -> str:
    """A size in the page's language and its decimal point: "1.5 GB", "1,5 ГБ"; "—" for no size."""
    try:
        size = int(value)
    except TypeError, ValueError, OverflowError:
        return "—"
    try:
        return i18n.format_size(size, owner_language())
    except OverflowError:
        return "—"
