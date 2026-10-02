"""How check notifications reach the messengers (G4): grouped, with links, quiet hours, digest.

Messages are staged in state.json (``notify_pending``) in the same write as the check result
and then moved, in one locked write, into the messengers' own queue per recipient
(tow.notifiers.outbox). A TOW stopped in between loses nothing: the next run or the watchdog
dispatches what is still pending, and the outbox sends what it holds. Delivery is at least
once: a TOW stopped between a messenger's answer and the queue's write repeats that message
(no messenger TOW uses offers a way to deduplicate it).

- Grouping: when one tracker fails the same way for several topics in one run, the
  owner gets one message for the tracker instead of one per topic.
- Links: a topic message carries the topic URL.
- Quiet hours (``quiet_hours: "23-8"`` in config.yaml): messages are queued in
  state.json and sent together by the first run after the quiet hours end.
- Daily digest (``daily_digest_hour: 9``): the first run after that hour each day
  sends a summary of the last day's additions and completions (on top of the
  immediate messages).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any, Protocol

from tow import i18n
from tow.i18n import t
from tow.log import cls_label, error_class, history_events
from tow.notify import PendingNotification, error_parts, event_text, short_series_title
from tow.records import Topic
from tow.store import load_state, persistence_lock, save_state

GROUP_MIN = 3
QUEUE_LIMIT = 200
PENDING_LIMIT = 200
_NO_LINK = frozenset({"qbit_down", "qbit_up"})


class Send(Protocol):
    """Hands one composed message to the messengers; True when every one delivered it."""

    def __call__(self, *, text: str, operation_id: str, topic: Topic | None) -> bool: ...


def quiet_now(cfg: dict[str, Any], now: datetime | None = None) -> bool:
    match = re.fullmatch(r"\s*(\d{1,2})\s*-\s*(\d{1,2})\s*", str(cfg.get("quiet_hours") or ""))
    if not match:
        return False
    start, end = int(match.group(1)) % 24, int(match.group(2)) % 24
    hour = (now or datetime.now().astimezone()).hour
    if start == end:
        return False
    return start <= hour < end if start < end else (hour >= start or hour < end)


def compose(items: Iterable[PendingNotification]) -> list[tuple[str, str, Topic | None]]:
    """(text, operation_id, topic) per message; repeated tracker failures become one."""
    items = list(items)
    lang = i18n.message_language()
    groups: dict[tuple[str, str], list[PendingNotification]] = {}
    for item in items:
        if item.kind == "error" and item.tracker:
            first = next(iter(error_parts(item.error)), "")
            groups.setdefault((item.tracker, error_class(first)), []).append(item)
    grouped = {id(item) for members in groups.values() if len(members) >= GROUP_MIN for item in members}
    messages: list[tuple[str, str, Topic | None]] = []
    for (tracker, cls), members in groups.items():
        if len(members) < GROUP_MIN:
            continue
        names = [
            short_series_title(str(m.topic.get("tracker_title") or m.topic.get("title") or ""), lang) for m in members
        ]
        listed = ", ".join(names[:5]) + (t("notify.and_more", lang, count=len(names) - 5) if len(names) > 5 else "")
        text = t(
            "notify.group_failure",
            lang,
            tracker=tracker,
            problem=cls_label(cls, lang),
            n=len(members),
            topics=listed,
        )
        messages.append((text, members[0].operation_id, None))
    for item in items:
        if id(item) in grouped:
            continue
        text = event_text(
            title=str(item.topic.get("tracker_title") or item.topic.get("title") or ""),
            kind=item.kind,
            tracker=item.tracker,
            error=item.error,
            episodes=item.episodes,
            selected_files=item.topic.get("selected_file_count"),
            total_files=item.topic.get("torrent_file_count"),
            recovered=bool(getattr(item, "recovered", False)),
        )
        url = str(item.topic.get("url") or "")
        if url and item.kind not in _NO_LINK:
            text += "\n" + url
        messages.append((text, item.operation_id, item.topic))
    return messages


def _pending(state: dict[str, Any], text: str, operation_id: str, topic: Topic | None) -> None:
    pending = state.setdefault("notify_pending", [])
    pending.append(
        {"id": uuid.uuid4().hex[:12], "text": text, "operation_id": operation_id, "topic_id": (topic or {}).get("id")}
    )
    del pending[:-PENDING_LIMIT]


def stage(
    state: dict[str, Any], items: Iterable[PendingNotification], *, cfg: dict[str, Any], now: datetime | None = None
) -> int:
    """Put the run's messages into ``state`` (the caller saves it); held during quiet hours."""
    messages = compose(items)
    queue = [str(text) for text in state.get("notify_queue") or []]
    if quiet_now(cfg, now):
        if messages:
            state["notify_queue"] = [*queue, *(text for text, _, _ in messages)][-QUEUE_LIMIT:]
        return 0
    if queue:
        state.pop("notify_queue", None)
        heading = t("notify.quiet_hours", i18n.message_language())
        _pending(state, heading + "\n\n" + "\n\n".join(queue), "quiet-hours", None)
    for text, operation_id, topic in messages:
        _pending(state, text, operation_id, topic)
    return len(messages) + (1 if queue else 0)


def dispatch(send: Send) -> int:
    """Move every pending message into the messengers' queue in ONE locked write, then deliver.

    The write puts each record into the outbox of every connected recipient
    (``tow.notifiers.outbox``, which sends under a lease) and removes it from
    ``notify_pending``. Two dispatchers - the watchdog and a check - can never take the
    same record, and a TOW stopped right after the write loses nothing: the outbox
    delivers it later. ``send`` is then called once per record that was handed over; it
    delivers what is queued (and audits), it does not queue the text again.
    """
    from tow.notifiers.outbox import enqueue
    from tow.store import SecretStoreError, load_secrets

    with persistence_lock():
        state = load_state()
        pending = [record for record in state.get("notify_pending") or [] if isinstance(record, dict)]
        if not pending:
            return 0
        try:
            secrets = load_secrets()
        except SecretStoreError:
            return 0  # kept for later: without the settings nothing can be handed over
        for record in pending:
            enqueue(state, secrets, str(record.get("text") or ""))
        state.pop("notify_pending", None)
        save_state(state)
    for record in pending:
        topic = Topic(id=str(record["topic_id"])) if record.get("topic_id") else None
        send(text=str(record.get("text") or ""), operation_id=str(record.get("operation_id") or "bot"), topic=topic)
    return len(pending)


def deliver(
    items: Iterable[PendingNotification], *, cfg: dict[str, Any], send: Send, now: datetime | None = None
) -> dict[str, Any]:
    """Stage and dispatch at once (callers outside a check transaction)."""
    with persistence_lock():
        state = load_state()
        before = list(state.get("notify_queue") or [])
        staged = stage(state, items, cfg=cfg, now=now)
        if staged or before != list(state.get("notify_queue") or []):
            save_state(state)
    if quiet_now(cfg, now):
        return {"sent": 0, "deferred": len(compose(items))}
    return {"sent": dispatch(send), "deferred": 0}


def _moment(value: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(value)
    except TypeError, ValueError:
        return None
    return moment if moment.tzinfo else moment.astimezone()


def maybe_digest(*, cfg: dict[str, Any], send: Send, now: datetime | None = None) -> bool:
    """Once a day, after ``daily_digest_hour``: what was added and completed since the last one."""
    hour = cfg.get("daily_digest_hour")
    if not isinstance(hour, int) or isinstance(hour, bool):
        return False
    now = now or datetime.now().astimezone()
    if now.hour < hour or quiet_now(cfg, now):
        return False
    today = now.date().isoformat()
    state = load_state()
    if state.get("digest_sent_on") == today:
        return False
    since = str(state.get("digest_since") or (now - timedelta(days=1)).isoformat())
    since_at = _moment(since) or now - timedelta(days=1)
    events = [
        e
        for e in history_events(group="downloads", limit=2000)
        if (moment := _moment(str(e.get("created_at") or ""))) is not None and moment >= since_at
    ]
    added = sum(1 for e in events if e.get("kind") in {"client_added", "client_updated"})
    found = sum(1 for e in events if e.get("kind") in {"new_file", "revision_updated"})
    completed = [e for e in events if e.get("kind") in {"episode_completed", "file_completed"}]
    # in the order they happened (history comes newest first)
    lang = i18n.message_language()
    titles = list(
        dict.fromkeys(
            short_series_title(str(e.get("title") or ""), lang) for e in reversed(completed) if e.get("title")
        )
    )
    text = t("notify.digest.summary", lang, added=added, found=found, completed=len(completed))
    if titles:
        more = t("notify.and_more", lang, count=len(titles) - 10) if len(titles) > 10 else ""
        text += "\n" + t("notify.digest.completed", lang, titles=", ".join(titles[:10]) + more)
    # "Sent today" and the message itself go in ONE write: a TOW stopped in between neither
    # loses today's digest nor sends it twice, and two processes cannot both queue it.
    with persistence_lock():
        state = load_state()
        if state.get("digest_sent_on") == today:
            return False
        state["digest_sent_on"] = today
        state["digest_since"] = now.isoformat()
        _pending(state, text, "daily-digest", None)
        save_state(state)
    dispatch(send)
    return True
