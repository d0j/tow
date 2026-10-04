"""Texts in the page's language: ``t`` (a catalog text), ``tm`` (one with its markup) and sizes.

Outside a request (a test, a background thread) the language is the owner's.
"""

from __future__ import annotations

import re
from typing import Any

from markupsafe import Markup, escape

from tow import i18n
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


def format_bytes(value: Any) -> str:
    """A size in the page's language and its decimal point: "1.5 GB", "1,5 ГБ"; "—" for no size."""
    try:
        size = max(0, int(value))
    except TypeError, ValueError, OverflowError:
        return "—"
    if size < 1024:
        return t("web.bytes.b", size=size)
    lang = owner_language()
    key, power = next(
        (
            (key, power)
            for key, power in (("web.bytes.tb", 4), ("web.bytes.gb", 3), ("web.bytes.mb", 2))
            if size >= 1024**power
        ),
        ("web.bytes.kb", 1),
    )
    try:
        return t(key, size=i18n.format_decimal(size / 1024**power, 1, lang))
    except OverflowError:
        return "—"
