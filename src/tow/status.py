from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tow.i18n import t
from tow.log import error_class, owner_language

# Transport trouble that passes by itself: amber for the status dot and the site icon.
TRACKER_WARNING_CLASSES = frozenset({"tracker", "frozen", "cloudflare"})
# The site icon is also amber for a sign-in the site asks for and the day's download limit
# (AGENTS.md, the status-colour contract; the status dot stays red for them).
_SIGN_IN_CLASSES = frozenset({"tracker_auth", "auth"})
SITE_WARNING_CLASSES = TRACKER_WARNING_CLASSES | {"quota"} | _SIGN_IN_CLASSES
# Problems on the site's side about the topic (removed, not a torrent page, no site): red site icon.
SITE_ERROR_CLASSES = frozenset({"gone", "no_tracker", "not_torrent"})

# Catalog keys of the labels (tow/locales): translated when a status is projected (the page's
# language, or the owner's outside a request).
_TORRENT_LABELS = {
    "ok": "status.torrent.ok",
    "new": "status.torrent.new",
    "warn": "status.torrent.warn",
    "bad": "status.torrent.bad",
    "mut": "status.torrent.mut",
}
_TRACKER_LABELS = {
    "ok": "status.tracker.ok",
    "warn": "status.tracker.warn",
    "bad": "status.tracker.bad",
    "mut": "status.tracker.mut",
}


@dataclass(frozen=True, slots=True)
class HomeStatus:
    """Independent Home tones derived from persisted action evidence."""

    torrent_tone: str
    tracker_tone: str
    torrent_label: str
    tracker_label: str
    torrent_action: str | None = None
    tracker_action: str | None = None


def _last_event(history_record: dict[str, Any]) -> dict[str, Any]:
    event = history_record.get("last_event")
    return event if isinstance(event, dict) else {}


def _tracker_tone(topic: dict[str, Any]) -> str:
    """The site icon shows the site only: a torrent-client or folder error is not the site's
    fault - the site is green when it answered (in this check, or once before)."""
    error = str(topic.get("last_error") or "")
    if error:
        cls = str(topic.get("last_error_class") or error_class(error))
        if cls in SITE_WARNING_CLASSES:
            return "warn"
        if cls in SITE_ERROR_CLASSES:
            return "bad"
        return "ok" if topic.get("last_ok_at") or topic.get("site_ok_at") else "mut"
    if topic.get("last_ok"):
        return "ok"
    return "mut"


def site_check_tone(topic: dict[str, Any]) -> str | None:
    """What the topic's latest check says about its site, for the site's own colour (the
    header, Sites): "warn" for transport trouble, a sign-in or the day's limit, "ok" when the
    site answered, None when that check did not reach the site."""
    error = str(topic.get("last_error") or "")
    cls = str(topic.get("last_error_class") or error_class(error)) if error else ""
    if cls in SITE_WARNING_CLASSES:
        return "warn"
    checked = topic.get("last_check")
    if not checked:
        return None
    if topic.get("site_ok_at") == checked:
        return "ok"
    if cls in SITE_ERROR_CLASSES - {"no_tracker"}:
        return "ok"  # the site answered - about this topic (removed, not a torrent)
    return None


def project_home_status(topic: dict[str, Any], history_record: dict[str, Any] | None) -> HomeStatus:
    """Project persisted action results into independent Home status channels.

    The torrent channel uses client/file history when a tracker refresh later
    fails. The tracker channel always reports the latest tracker check result.
    This prevents a tracker transport problem from rewriting a known-good
    torrent/client result while preserving the underlying error for inspection.
    """
    topic = topic if isinstance(topic, dict) else {}
    history_record = history_record if isinstance(history_record, dict) else {}
    error = str(topic.get("last_error") or "")
    error_cls = str(topic.get("last_error_class") or error_class(error))
    error_tone = "warn" if error_cls in TRACKER_WARNING_CLASSES else "bad" if error else None
    event = _last_event(history_record)
    action = str(event.get("kind") or "") or None

    if error_tone:
        torrent_tone = error_tone
    elif topic.get("last_changed"):
        torrent_tone = "new"
        action = action or "tracker_changed"
    elif topic.get("last_ok"):
        torrent_tone = "ok"
        action = action or "tracker_checked"
    else:
        torrent_tone = "mut"

    tracker_tone = _tracker_tone(topic)
    tracker_action = "tracker_check" if error or topic.get("last_ok") else None
    lang = owner_language()
    # Amber for a sign-in is not "down": the site answers and asks for a login.
    tracker_key = "status.tracker.sign_in" if error and error_cls in _SIGN_IN_CLASSES else _TRACKER_LABELS[tracker_tone]
    return HomeStatus(
        torrent_tone=torrent_tone,
        tracker_tone=tracker_tone,
        torrent_label=t(_TORRENT_LABELS[torrent_tone], lang),
        tracker_label=t(tracker_key, lang),
        torrent_action=action,
        tracker_action=tracker_action,
    )
