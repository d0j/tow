"""Discord: messages into a channel through its webhook."""

from __future__ import annotations

from typing import Any

from tow.errors import Msg
from tow.notifiers.base import DeliveryError, Field, request, status_error

KIND = "discord"
TITLE = "Discord"
ORDER = 20
MAX_LEN = 2000
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


def send(settings: dict[str, Any], text: str) -> None:
    response = request(
        "POST",
        str(settings.get("webhook_url") or ""),
        what="Discord",
        # No @everyone / role pings from torrent titles.
        json={"content": text, "allowed_mentions": {"parse": []}},
    )
    if response.status_code in (200, 204):
        return
    if response.status_code in (401, 403, 404):
        raise DeliveryError(Msg("notifier.discord.webhook_gone"), transient=False)
    raise status_error(response.status_code, Msg("notifier.discord.error", code=response.status_code))
