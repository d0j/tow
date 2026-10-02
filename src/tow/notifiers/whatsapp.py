"""WhatsApp through the free CallMeBot gateway: no business account, a phone and a key."""

from __future__ import annotations

import re
from typing import Any

from tow.errors import Msg
from tow.notifiers.base import DeliveryError, Field, request, status_error

KIND = "whatsapp"
TITLE = "WhatsApp"
ORDER = 30
MAX_LEN = 1000
# CallMeBot asks for a gap between messages; parts of a long message are spaced out.
CHUNK_PAUSE = 3.0
# Texts are keys of the language files (notifier.whatsapp.*).
STEPS = (
    "notifier.whatsapp.step_contact",
    "notifier.whatsapp.step_allow",
    "notifier.whatsapp.step_apikey",
    "notifier.whatsapp.step_save",
)
NOTE = "notifier.whatsapp.note"
FIELDS = (
    Field(
        "phone",
        "notifier.whatsapp.phone_label",
        placeholder="notifier.whatsapp.phone_placeholder",
        pattern=r"\+?\d{8,15}",
        error="notifier.whatsapp.phone_error",
    ),
    Field(
        "apikey",
        "notifier.whatsapp.apikey_label",
        kind="secret",
        placeholder="123456",
        pattern=r"\d{4,12}",
        error="notifier.whatsapp.apikey_error",
    ),
)


def send(settings: dict[str, Any], text: str) -> None:
    response = request(
        "GET",
        "https://api.callmebot.com/whatsapp.php",
        what="WhatsApp",
        params={"phone": str(settings.get("phone") or ""), "text": text, "apikey": str(settings.get("apikey") or "")},
    )
    # CallMeBot answers 200 with a short page; success says the message was queued/sent.
    body = re.sub(r"<[^>]+>", " ", response.text or "").lower()
    if response.status_code == 200 and ("queued" in body or "message sent" in body):
        return
    if "apikey" in body or "api key" in body:
        raise DeliveryError(Msg("notifier.whatsapp.bad_apikey"), transient=False)
    if "phone" in body:
        raise DeliveryError(Msg("notifier.whatsapp.bad_phone"), transient=False)
    if "wait" in body or response.status_code == 200:
        raise DeliveryError(Msg("notifier.whatsapp.wait"), transient=True)
    raise status_error(response.status_code, Msg("notifier.whatsapp.error", code=response.status_code))
