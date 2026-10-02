"""ntfy: push notifications to a phone app, no registration."""

from __future__ import annotations

import secrets as _random
from typing import Any

from tow.errors import Msg
from tow.i18n import t
from tow.notifiers.base import DeliveryError, Field, request, status_error, ui_language

KIND = "ntfy"
# TITLE has words in it: it comes from the language files (notifier.ntfy.title), see __getattr__.
ORDER = 40
MAX_LEN = 3800
# The server keeps a message up to 4096 BYTES (a longer one becomes an attachment); Cyrillic
# takes two bytes a letter, so the parts are measured in UTF-8 bytes.
MAX_BYTES = 3800
# Texts are keys of the language files (notifier.ntfy.*).
STEPS = (
    "notifier.ntfy.step_app",
    "notifier.ntfy.step_topic",
    "notifier.ntfy.step_subscribe",
    "notifier.ntfy.step_save",
)
NOTE = "notifier.ntfy.note"
FIELDS = (
    Field(
        "topic",
        "notifier.ntfy.topic_label",
        # Shown, not masked: the owner types this name into the ntfy app to subscribe.
        placeholder="tow-…",
        pattern=r"[A-Za-z0-9_-]{8,64}",
        error="notifier.ntfy.topic_error",
    ),
    Field(
        "server",
        "notifier.ntfy.server_label",
        placeholder="https://ntfy.sh",
        pattern=r"https://[^\s/]+(?:/[^\s]*)?",
        error="notifier.ntfy.server_error",
        required=False,
        default="https://ntfy.sh",
    ),
)


def __getattr__(name: str) -> Any:
    if name == "TITLE":  # in the owner's language
        return t("notifier.ntfy.title", ui_language())
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def suggestion(field: str) -> str:
    """A ready, unguessable topic name, so the owner only has to subscribe to it."""
    return f"tow-{_random.token_hex(6)}" if field == "topic" else ""


def send(settings: dict[str, Any], text: str) -> None:
    server = str(settings.get("server") or "https://ntfy.sh").rstrip("/")
    response = request(
        "POST",
        f"{server}/{settings.get('topic')}",
        what="ntfy",
        content=text.encode("utf-8"),
        headers={"Title": "TOW", "Tags": "tv"},
    )
    if response.status_code == 200:
        return
    if response.status_code in (401, 403):
        raise DeliveryError(Msg("notifier.ntfy.login_required"), transient=False)
    raise status_error(response.status_code, Msg("notifier.ntfy.error", code=response.status_code))
