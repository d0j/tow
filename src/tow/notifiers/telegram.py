"""Telegram: a bot writes to your chats."""

from __future__ import annotations

from typing import Any

from tow.errors import Msg
from tow.notifiers.base import DeliveryError, Field, json_or_empty, request, status_error

KIND = "telegram"
TITLE = "Telegram"
ORDER = 10
# Telegram settings stay where TOW always kept them (secrets["telegram"]).
STORAGE = "telegram"
MAX_LEN = 4000
# Texts are keys of the language files (notifier.telegram.*).
STEPS = (
    "notifier.telegram.step_bot",
    "notifier.telegram.step_start",
    "notifier.telegram.step_id",
    "notifier.telegram.step_save",
)
NOTE = "notifier.telegram.note"
FIELDS = (
    Field(
        "token",
        "notifier.telegram.token_label",
        kind="secret",
        placeholder="123456789:AA…",
        pattern=r"\d{5,12}:[A-Za-z0-9_-]{30,}",
        error="notifier.telegram.token_error",
    ),
    Field(
        "chat_ids",
        "notifier.telegram.chat_ids_label",
        kind="list",
        placeholder="123456789",
        pattern=r"-?\d{3,20}",
        error="notifier.telegram.chat_ids_error",
    ),
)


def targets(settings: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Each chat is its own recipient: one that never pressed Start does not hold back the others."""
    return [(str(chat), {**settings, "chat_ids": [chat]}) for chat in settings.get("chat_ids") or []]


def send(settings: dict[str, Any], text: str) -> None:
    token = str(settings.get("token") or "")
    for chat in settings.get("chat_ids") or []:
        response = request(
            "POST",
            f"https://api.telegram.org/bot{token}/sendMessage",
            what="Telegram",
            data={"chat_id": str(chat), "text": text, "disable_web_page_preview": "true"},
        )
        body = json_or_empty(response)
        if response.status_code == 200 and body.get("ok"):
            continue
        if response.status_code in (401, 404):
            raise DeliveryError(Msg("notifier.telegram.bad_token"), transient=False)
        description = str(body.get("description") or "").casefold()
        if response.status_code == 403 or (response.status_code == 400 and "chat not found" in description):
            raise DeliveryError(Msg("notifier.telegram.chat_unreachable", chat=chat), transient=False)
        # Any other refusal (400 for this text, 429/5xx after the retries) is about this message.
        raise status_error(response.status_code, Msg("notifier.telegram.error", code=response.status_code))
