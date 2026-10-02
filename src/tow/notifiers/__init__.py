"""Messengers TOW writes to. One module per messenger, found automatically.

Adding a messenger = one file in this package with KIND, TITLE, ORDER, MAX_LEN, STEPS
(the owner's how-to), NOTE, FIELDS and ``send(settings, text)`` that raises
``DeliveryError`` - plus tests with a fake HTTP transport. Optional: ``targets(settings)``
to deliver to each recipient separately (Telegram chats), ``CHUNK_PAUSE`` between parts of a
long message, ``suggestion(field)`` for a ready value. Nothing else changes: the settings
card, saving, checking, the queue and the status all come from here.

Its wording lives only in the language files, under ``notifier.<kind>.*``: STEPS, NOTE and
the Field label/placeholder/error are keys (the card shows them translated), ``send`` builds
its reasons with ``t(key, base.message_language())``, and ``notifier.<kind>.title`` (if
present) replaces TITLE where the name has words in it. Shared texts: ``notifier.common.*``.

- ``registry``: which messengers exist, their settings and recipients;
- ``outbox``: the delivery queue (written first, sent after; per recipient; resumable);
- ``view``: what the header and the settings page show.

Delivery is meant to be hard to break:
- every connected recipient gets every message; one failing recipient never stops others;
- transient failures are retried (and Retry-After honoured) inside ``base.request`` and
  later from the queue (the watchdog flushes it every 10 minutes);
- a permanent error (wrong token, deleted webhook) holds the queue until the settings change
  instead of failing - and repeating - every few minutes;
- reasons are plain words without tokens or addresses; settings live encrypted.
"""

from __future__ import annotations

from typing import Any

from tow.errors import Msg
from tow.i18n import t
from tow.notifiers.base import DeliveryError, Field, message_language, ui_language
from tow.notifiers.outbox import OUTBOX_LIMIT, enqueue, flush, parts_for, unblock
from tow.notifiers.registry import (
    connected,
    get,
    kinds,
    raw_settings,
    remove,
    settings_of,
    store,
    targets,
    title,
    validate,
)
from tow.notifiers.view import cards, health, summary


def check_message(lang: str | None = None) -> str:
    """The message the settings "Проверить" button sends (in the messages' language)."""
    return t("notifier.common.test_text", lang or message_language())


def _by_kind(results: dict[str, tuple[bool, str]], keys: list[str]) -> dict[str, tuple[bool, str]]:
    """Per messenger: delivered to all its recipients, else the first reason."""
    out: dict[str, tuple[bool, str]] = {}
    queued: str | None = None
    for key in keys:
        kind = key.split(":", 1)[0]
        if key not in results and queued is None:
            queued = t("notifier.common.queued", message_language())
        ok, reason = results.get(key, (False, queued or ""))
        before = out.get(kind)
        if before is None or (before[0] and not ok):
            out[kind] = (ok, reason)
    return out


def send_all(secrets: dict[str, Any], text: str) -> dict[str, tuple[bool, str]]:
    """Queue ``text`` for every connected recipient and deliver; {kind: (delivered, reason)}.

    The text is saved before any network call: if TOW stops halfway, the watchdog sends it.
    """
    from tow.store import load_state, persistence_lock, save_state

    with persistence_lock():
        state = load_state()
        keys = enqueue(state, secrets, text)
        if keys:
            save_state(state)
    if not keys:
        return {}
    return _by_kind(flush(secrets, only=set(keys)), keys)


def flush_outbox(secrets: dict[str, Any]) -> dict[str, tuple[bool, str]]:
    """Deliver queued messages only (the watchdog calls this every 10 minutes)."""
    results = flush(secrets)
    return _by_kind(results, list(results))


def deliver_queued(secrets: dict[str, Any]) -> dict[str, tuple[bool, str]]:
    """Deliver what is queued; {kind: (delivered, reason)} for every connected messenger.

    A recipient with nothing left to send reports how its last delivery went (an earlier
    flush in the same pass may have sent this message together with others).
    """
    from tow.store import load_state

    results = flush(secrets)
    keys = [key for key, _kind, _module, _settings in targets(secrets)]
    state = load_state()
    boxes = state.get("notify_outbox") or {}
    statuses = state.get("notify_status") or {}
    full: dict[str, tuple[bool, str]] = {}
    for key in keys:
        box = boxes.get(key)
        if key in results:
            full[key] = results[key]
        elif isinstance(box, dict) and (box.get("items") or box.get("inflight")):
            full[key] = (False, t("notifier.common.queued", message_language()))
        else:
            status = statuses.get(key)
            if not isinstance(status, dict):
                status = {}
            full[key] = (bool(status.get("ok", True)), str(status.get("error") or ""))
    return _by_kind(full, keys)


def test(secrets: dict[str, Any], kind: str) -> tuple[bool, str]:
    """The settings "Проверить" button: one real message to every recipient, nothing queued."""
    from tow.notifiers.outbox import set_status
    from tow.store import load_state, persistence_lock, save_state

    module = get(kind)
    lang = ui_language()
    if settings_of(secrets, kind) is None:
        return False, t("notifier.common.fill_first", lang, title=title(module, lang))
    reasons: dict[str, DeliveryError | None] = {}
    message = check_message()
    for key, target_kind, _module, settings in targets(secrets):
        if target_kind != kind:
            continue
        try:
            for part in parts_for(module, message):
                module.send(settings, part)
        except DeliveryError as exc:
            reasons[key] = exc
        else:
            reasons[key] = None
    with persistence_lock():
        state = load_state()
        for key, failure in reasons.items():
            set_status(state, key, failure.detail if failure else None)
        save_state(state)
    failed = [failure for failure in reasons.values() if failure]
    if failed:
        detail = failed[0].detail
        return False, detail.text(lang) if isinstance(detail, Msg) else str(detail)
    unblock(kind)  # what waited for fixed settings may go now
    return True, t("notifier.common.test_sent", lang, title=title(module, lang))


__all__ = [
    "OUTBOX_LIMIT",
    "DeliveryError",
    "Field",
    "cards",
    "check_message",
    "connected",
    "deliver_queued",
    "enqueue",
    "flush",
    "flush_outbox",
    "get",
    "health",
    "kinds",
    "raw_settings",
    "remove",
    "send_all",
    "settings_of",
    "store",
    "summary",
    "targets",
    "test",
    "title",
    "unblock",
    "validate",
]
