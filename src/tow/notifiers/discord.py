"""Discord: messages into a channel through its webhook."""

from __future__ import annotations

import re
from typing import Any

from tow.errors import Msg
from tow.notifiers.base import DeliveryError, Field, request, status_error

KIND = "discord"
TITLE = "Discord"
ORDER = 20
# Discord takes 2000 characters; escaping the Markdown (escape_markdown) at most doubles a part.
MAX_LEN = 1000
# Texts are keys of the language files (notifier.discord.*).
STEPS = (
    "notifier.discord.step_channel",
    "notifier.discord.step_new",
    "notifier.discord.step_copy",
    "notifier.discord.step_save",
)
NOTE = "notifier.discord.note"
FIELDS = (
    Field(
        "webhook_url",
        "notifier.discord.webhook_label",
        kind="secret",
        placeholder="https://discord.com/api/webhooks/…",
        pattern=r"https://(?:\w+\.)?(?:discord|discordapp)\.com/api/webhooks/\d+/[\w-]+",
        error="notifier.discord.webhook_error",
    ),
)


_URL = re.compile(r"https?://\S+")
# What Discord reads as Markdown: anywhere, and (lists, quotes, headings) at a line's start.
_MARKUP = re.compile(r"([\\*_~`|\[\]<>])")
_LINE_START = re.compile(r"(?m)^(\s*)(#|-|\d+\.)")


def _line_start(match: re.Match[str]) -> str:
    mark = match.group(2)
    # A backslash escapes punctuation only: "1." becomes "1\.", "-" becomes "\-".
    return match.group(1) + (mark[:-1] + "\\." if mark.endswith(".") else "\\" + mark)


def escape_markdown(text: str) -> str:
    """``text`` shown as written: a torrent title such as ``*Show* __Part__ ||x||`` is not bold,
    underlined or hidden. Links are left as they are (Discord still makes them clickable)."""
    out: list[str] = []
    at = 0
    for link in _URL.finditer(text):
        out.append(_MARKUP.sub(r"\\\1", text[at : link.start()]))
        out.append(link.group(0))
        at = link.end()
    out.append(_MARKUP.sub(r"\\\1", text[at:]))
    return _LINE_START.sub(_line_start, "".join(out))


def send(settings: dict[str, Any], text: str) -> None:
    response = request(
        "POST",
        str(settings.get("webhook_url") or ""),
        what="Discord",
        # No @everyone / role pings from torrent titles, and no Markdown either.
        json={"content": escape_markdown(text), "allowed_mentions": {"parse": []}},
    )
    if response.status_code in (200, 204):
        return
    if response.status_code in (401, 403, 404):
        raise DeliveryError(Msg("notifier.discord.webhook_gone"), transient=False)
    raise status_error(response.status_code, Msg("notifier.discord.error", code=response.status_code))
