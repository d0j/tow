"""The messages a check run sends: errors once per class, recoveries, and the delivery."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tow.check.topic import CheckRun
from tow.events import new_operation_id
from tow.log import error_class, log_event
from tow.notify import NotificationBatch
from tow.records import Topic, topics_of
from tow.status import TRACKER_WARNING_CLASSES


def _audited_send(
    secrets: dict[str, Any],
    *,
    text: str,
    operation_id: str,
    topic: Topic | None = None,
    how: str = "auto",
) -> bool:
    """Deliver a message ``delivery.dispatch`` has already put into the messengers' queue, and log it."""
    from tow.notifiers import connected, deliver_queued

    topic_id = (topic or {}).get("id")
    fields = {
        "operation_id": operation_id,
        "component": "notification",
        "topic_id": topic_id,
        "topic": topic_id,
        "how": how,
    }
    channels = [kind for kind, _, _ in connected(secrets)]
    if not channels:
        # No messenger is connected: nothing is sent, so nothing is logged as a delivery (a
        # "sending to bot" / "bot delivery error" pair after every check meant nothing).
        return False
    log_event("bot_delivery_started", **fields, integration_id=",".join(channels))
    results = deliver_queued(secrets)
    for kind, (ok, reason) in results.items():
        log_event(
            "bot_delivery_succeeded" if ok else "bot_delivery_failed",
            **fields,
            integration_id=kind,
            status="succeeded" if ok else "queued",
            reason=None if ok else "send_failed",
            error=reason or None,
        )
    return bool(results) and all(ok for ok, _ in results.values())


def queue_recoveries(state: dict[str, Any], results: list[dict[str, Any]], run: CheckRun) -> None:
    """Close every reported failure that is over: the owner heard "Сбой", now hears it ended."""
    topics_by_id = {str(topic.get("id")): topic for topic in topics_of(state)}
    for row in results:
        topic = topics_by_id.get(str(row.get("id")))
        if topic is None or not row.get("ok") or topic.get("last_error"):
            continue
        topic.pop("error_streak", 0)
        # A check that sends nothing (tow check --apply without --notify) keeps the mark: the
        # next one that does still tells the owner it works again.
        if run.notify and topic.pop("error_notified", False):
            run.queue_notification(
                topic, kind="recovered", operation_id=new_operation_id("bot"), tracker=str(row.get("tracker") or "")
            )


# Checks in a row a site's transport trouble (an amber class) lasts before the owner hears of it.
TRACKER_ERROR_NOTIFY_AFTER = 3


def _streak(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


@dataclass
class RunNotifications:
    """The messages one run collects; an error is reported when it starts or changes class,
    not on every run while it lasts."""

    enabled: bool
    batch: NotificationBatch
    # Error class each topic ended the previous run with ("" = healthy).
    previous_error_class: dict[str, str]
    client_errors: dict[str, Exception]
    default_client_id: str

    def queue(
        self,
        topic: Topic,
        *,
        kind: str,
        operation_id: str,
        tracker: str = "",
        error: Any = "",
        episodes: str = "",
        error_cls: str = "",
    ) -> None:
        if not self.enabled:
            return
        if kind == "error":
            if str(topic.get("client_id") or self.default_client_id) in self.client_errors:
                return  # a single "торрент-клиент недоступен" covers every topic of a dead client
            cls = error_cls or error_class(error)
            if cls in TRACKER_WARNING_CLASSES:
                # A site's transport trouble (amber) often passes by itself: reported once it
                # lasted several checks in a row, not as an error/recovered pair every other run.
                streak = _streak(topic.get("error_streak")) + 1
                topic["error_streak"] = streak
                if streak < TRACKER_ERROR_NOTIFY_AFTER and not topic.get("error_notified"):
                    return
            else:
                topic.pop("error_streak", None)
            if self.previous_error_class.get(str(topic.get("id"))) == cls and topic.get("error_notified"):
                return  # already reported; "снова работает" will close it
            topic["error_notified"] = True
        self.batch.queue(
            topic,
            kind=kind,
            operation_id=operation_id,
            tracker=tracker,
            error=error,
            episodes=episodes,
        )


def flush_notifications(cfg: dict[str, Any], secrets: dict[str, Any], *, how: str, notify: bool) -> None:
    """G4: grouped per tracker, with links, held during quiet hours, plus the digest. The
    messages were staged in the commit; here they are handed to the messengers."""
    from tow import delivery

    def send(*, text: str, operation_id: str, topic: Topic | None) -> bool:
        return _audited_send(secrets, text=text, operation_id=operation_id, topic=topic, how=how)

    delivery.dispatch(send)
    if notify:
        delivery.maybe_digest(cfg=cfg, send=send)
