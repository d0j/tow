"""The messages a check run sends: errors once per class, recoveries, and the delivery."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

from tow.check.topic import CheckRun
from tow.events import new_operation_id
from tow.log import error_class, log_event
from tow.notify import NotificationBatch, PendingNotification
from tow.records import Topic, topics_of
from tow.status import TRACKER_WARNING_CLASSES


def _audited_send(
    secrets: dict[str, Any],
    *,
    text: str,
    operation_id: str,
    topic: Topic | None = None,
    how: str = "auto",
    deliver: Callable[[dict[str, Any]], dict[str, tuple[bool, str]]] | None = None,
) -> bool:
    """Deliver a message ``delivery.dispatch`` has already put into the messengers' queue, and log
    it. ``deliver`` sends the queue (``notifiers.deliver_queued`` when not given)."""
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
    results = (deliver or deliver_queued)(secrets)
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


def _sender(secrets: dict[str, Any], *, how: str) -> Callable[..., bool]:
    """The ``send`` of one ``delivery.dispatch``: the queue is delivered once, at its first record
    (which sends every record handed over, combined), and each record is audited from that one
    result. Delivering again per record repeated every failing recipient's retries - five
    messages to three dead chats took minutes - while the check lock was held."""
    delivered: dict[str, tuple[bool, str]] | None = None

    def deliver(current: dict[str, Any]) -> dict[str, tuple[bool, str]]:
        nonlocal delivered
        if delivered is None:
            from tow.notifiers import deliver_queued

            delivered = deliver_queued(current)
        return delivered

    def send(*, text: str, operation_id: str, topic: Topic | None) -> bool:
        return _audited_send(secrets, text=text, operation_id=operation_id, topic=topic, how=how, deliver=deliver)

    return send


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
    # The run's kind (``run_check``'s ``how``): the supervisor's early retries of a site outage
    # ("retry") come minutes apart, so they do not count towards the streak below.
    how: str = "auto"

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
        previous_stopped: bool = False,
    ) -> None:
        if not self.enabled:
            return
        if kind == "error":
            if str(topic.get("client_id") or self.default_client_id) in self.client_errors:
                return  # a single "торрент-клиент недоступен" covers every topic of a dead client
            cls = error_cls or error_class(error)
            if cls in TRACKER_WARNING_CLASSES:
                if self.how == "retry":
                    # A failed retry says the outage goes on, not how long: the scheduled and
                    # manual checks count it (a successful retry still ends it).
                    return
                # A site's transport trouble (amber) often passes by itself: reported once it
                # lasted several checks in a row, not as an error/recovered pair every other run.
                streak = _streak(topic.get("error_streak")) + 1
                topic["error_streak"] = streak
                if streak < TRACKER_ERROR_NOTIFY_AFTER and not topic.get("error_notified"):
                    return
            else:
                topic.pop("error_streak", None)
            previous = self.previous_error_class.get(str(topic.get("id")))
            same = previous == cls or (previous in TRACKER_WARNING_CLASSES and cls in TRACKER_WARNING_CLASSES)
            if same and topic.get("error_notified"):
                return  # already reported; "снова работает" will close it
            topic["error_notified"] = True
        self.batch.queue(
            topic,
            kind=kind,
            operation_id=operation_id,
            tracker=tracker,
            error=error,
            episodes=episodes,
            previous_stopped=previous_stopped,
        )


def flush_notifications(cfg: dict[str, Any], secrets: dict[str, Any], *, how: str, notify: bool) -> None:
    """G4: grouped per tracker, with links, held during quiet hours, plus the digest. The
    messages were staged in the commit; here they are handed to the messengers."""
    from tow import delivery

    delivery.dispatch(_sender(secrets, how=how))
    if notify:
        # A sender of its own: the digest is queued after the messages above were delivered.
        delivery.maybe_digest(cfg=cfg, send=_sender(secrets, how=how))


def notify_topic_error(topic: dict[str, Any], error: Any, *, tracker: str = "", how: str) -> None:
    """A topic's failure outside a check run (the first check of a new topic crashed), sent as
    a check's messages are: held during the quiet hours, through the messengers' queue."""
    from tow import delivery
    from tow.config import load_config
    from tow.store import load_secrets, load_state, persistence_lock, save_state

    secrets = load_secrets()
    item = PendingNotification("error", new_operation_id("add-check"), cast(Topic, topic), tracker, error)
    delivery.deliver([item], cfg=load_config(), send=_sender(secrets, how=how))
    # As a check marks a reported failure: the next check that works says "works again" (it
    # closes only marked failures).
    with persistence_lock():
        state = load_state()
        saved = next((row for row in topics_of(state) if str(row.get("id")) == str(topic.get("id"))), None)
        if saved is not None and not saved.get("error_notified"):
            saved["error_notified"] = True
            save_state(state)
