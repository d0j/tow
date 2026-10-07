from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast

from tow import check_transaction, download_history
from tow.check import rows
from tow.check.apply import FREE_SPACE_MARGIN, free_space_problem
from tow.check.client_ops import (
    PREVIOUS_REVISION_ACTIVE,
    ClientPool,
    await_relocation,
    blocked_by_previous_revision,
    client_owned_by_tow,
    client_unreachable,
    remote_clients,
)
from tow.check.rows import fail_row, row_error, stamp_result
from tow.check.topic import CheckRun, check_topic, read_secrets
from tow.check_steps import (
    changed_owner_fields,
    mark_reconcile_failure,
    merge_check_results,
    owner_fields,
    reconcile_notification,
)
from tow.clients import factory as client_factory
from tow.clock import iso_now, machine_now
from tow.config import load_config
from tow.errors import TowError
from tow.events import new_operation_id
from tow.i18n import t
from tow.jsonish import as_dict
from tow.log import error_class, error_fields, log_event, owner_language
from tow.notify import NotificationBatch
from tow.progress import reconcile_topic
from tow.records import CheckRow, DownloadHistory, Health, HistoryRecord, Topic, health_of, topics_of
from tow.status import TRACKER_WARNING_CLASSES
from tow.store import (
    SecretStoreError,
    StoreCorruptionError,
    StoreReadError,
    check_run_lock,
    load_download_history,
    load_state,
    persistence_lock,
    save_download_history,
    save_state,
)
from tow.trackers import load_trackers

__all__ = [
    "FREE_SPACE_MARGIN",
    "PREVIOUS_REVISION_ACTIVE",
    "await_relocation",
    "blocked_by_previous_revision",
    "client_owned_by_tow",
    "free_space_problem",
    "record_check_failure",
    "run_check",
]


def _check_failure_code(error: str | BaseException) -> str:
    """The header clock's reason: by the error's type (its text is in the owner's language);
    only a plain text (from an older caller) is matched by its words."""
    if isinstance(error, SecretStoreError):
        return "secrets_migration_required"
    if isinstance(error, BaseException):
        return error_class(error)
    message = str(error or "").lower()
    if "legacy plaintext" in message or "master key" in message or "secret" in message:
        return "secrets_migration_required"
    return error_class(error)


def _last_good_scheduled_check(health: Health) -> int | None:
    """When the last scheduled check completed (state written by an older TOW: its last
    scheduled attempt, unless that attempt was itself a recorded failure)."""
    if "auto_ok_at_ts" in health:
        value = health.get("auto_ok_at_ts")
        return int(value) if isinstance(value, (int, float)) else None
    value = health.get("auto_at_ts")
    return int(value) if isinstance(value, (int, float)) and health.get("check_ok") is not False else None


def record_check_failure(error: str | BaseException, *, how: str = "auto") -> Health:
    """Persist a failed apply attempt so the UI clock has a fresh attempt time."""
    code = _check_failure_code(error)
    now = machine_now()
    with persistence_lock():  # read-modify-write: never lose a concurrent edit
        state = load_state()
        previous = health_of(state)
        # A blocked check learned nothing about qBit or the bot: keep what the last real
        # check saw. Writing qbit_ok=False showed "связи нет" and swallowed the next
        # real "торрент-клиент недоступен" notification.
        health = Health()
        if "qbit" in previous:
            health["qbit"] = previous["qbit"]
        if "client" in previous:
            health["client"] = previous["client"]
        if "qbit_ok" in previous:
            health["qbit_ok"] = previous["qbit_ok"]
        if "clients_ok" in previous:
            health["clients_ok"] = previous["clients_ok"]
        health.update(
            {
                "check_ok": False,
                "check_error": code,
                "at": now.isoformat(timespec="seconds"),
                "at_ts": int(now.timestamp()),
                # The header countdown runs from the last scheduled *attempt*...
                "auto_at_ts": int(now.timestamp()) if how == "auto" else previous.get("auto_at_ts"),
                # ...the watchdog from the last scheduled check that really ran (M6): a check
                # failing on every run must not look like a healthy schedule.
                "auto_ok_at_ts": _last_good_scheduled_check(previous),
                "check_failures": int(previous.get("check_failures") or 0) + (1 if how == "auto" else 0),
            }
        )
        state["health"] = health
        save_state(state)
    log_event("check_blocked", reason=code, how=how)
    return health


def _relabel_source_hash(record: HistoryRecord | None, old: str, new: str) -> None:
    for item in ((record or {}).get("items") or {}).values():
        if isinstance(item, dict) and str(item.get("source_hash") or "").casefold() == old.casefold():
            item["source_hash"] = new


def _progress_only_row(topic: Topic) -> dict[str, Any]:
    return {
        "id": topic.get("id"),
        "title": topic.get("title"),
        "url": topic.get("url"),
        "ok": True,
        "hash": topic.get("hash"),
        "changed": False,
        "status": "skipped",
        "skipped": t("check.progress_only", owner_language()),
    }


def _without_scan_time(record: Any) -> Any:
    """A topic record as far as a change counts: ``last_scan_at`` changes on every pass and
    nothing reads it, so it is saved together with a real change, never alone."""
    return {k: v for k, v in record.items() if k != "last_scan_at"} if isinstance(record, dict) else record


def _merge_history(disk: DownloadHistory, seen: Mapping[str, Any], reconciled: Mapping[str, Any]) -> bool:
    """Put what the reconcile changed onto the history as it is on disk now, topic by topic;
    True when that changed the history (a new scan time alone does not).

    ``seen`` is the copy the reconcile started from: a topic record it did not change keeps
    whatever was written meanwhile (a restore, an import).
    """
    seen_topics: dict[str, Any] = as_dict(seen.get("topics"))
    new_topics: dict[str, Any] = as_dict(reconciled.get("topics"))
    disk_topics = disk.get("topics")
    changed = False
    if not isinstance(disk_topics, dict):
        disk_topics = disk["topics"] = {}
    for key in set(seen_topics) | set(new_topics):
        if key not in new_topics:
            changed = disk_topics.pop(key, _ABSENT) is not _ABSENT or changed
        elif new_topics[key] != seen_topics.get(key):
            before = disk_topics.get(key, _ABSENT)
            changed = changed or before is _ABSENT or _without_scan_time(before) != _without_scan_time(new_topics[key])
            disk_topics[key] = new_topics[key]
    others = {key: value for key, value in reconciled.items() if key != "topics" and seen.get(key) != value}
    changed = changed or any(key not in disk or disk.get(key) != value for key, value in others.items())
    cast(dict[str, Any], disk).update(others)
    return changed


_ABSENT: Any = object()


def _load_history(*, quarantine: bool) -> tuple[DownloadHistory, bool]:
    """The download history, and whether it had to start over.

    The history is rebuildable (the clients still have every torrent): a corrupt or
    quarantined download_history.json no longer blocks every check. The check goes on
    with an empty history, the quarantined file stays where it is, and the owner is told.
    A file that merely cannot be read right now (``StoreReadError``) still stops the run.
    """
    try:
        return cast(DownloadHistory, load_download_history(quarantine=quarantine)), False
    except StoreReadError:
        raise
    except StoreCorruptionError:
        return {"schema_version": 1, "topics": {}}, True


# How long the Home page says that the download history was started over.
HISTORY_REBUILT_NOTICE_SEC = 7 * 24 * 3600


def _note_history_rebuilt(health: Health, previous: Health, *, rebuilt: bool) -> None:
    if rebuilt:
        health["history_rebuilt_at"] = health["at_ts"]
        return
    earlier = previous.get("history_rebuilt_at")
    if isinstance(earlier, int) and health["at_ts"] - earlier < HISTORY_REBUILT_NOTICE_SEC:
        health["history_rebuilt_at"] = earlier


def _reconcile_and_commit(
    state: dict[str, Any],
    results: list[dict[str, Any]],
    run: CheckRun,
    records: list[tuple[str, dict[str, Any]]],
    want: set[str] | None,
    commit_state: Callable[[bool], None],
) -> bool:
    """Reconcile with the clients, then commit history and state; True when a journal
    cleanup is left pending.

    Reconcile asks the torrent clients and looks at files (a sleeping NAS can take minutes):
    it works on a copy of the history, OUTSIDE the data lock, so the web UI and the watchdog
    are never held up by it. The lock is taken again only to merge and commit (H1).
    """
    with persistence_lock():
        history, rebuilt = _load_history(quarantine=True)
    history_seen = copy.deepcopy(history)
    _reconcile_all(state, results, run, history, records, want)
    _queue_recoveries(state, results, run)
    keep_days, max_items = run.history_retention
    watched = {str(topic.get("id")) for topic in topics_of(state)}
    with persistence_lock():
        disk_history, disk_rebuilt = _load_history(quarantine=True)
        if rebuilt and not disk_rebuilt:
            history = history_seen  # restored meanwhile: an empty-history reconcile must not overwrite it
        changed = _merge_history(disk_history, history_seen, history)
        pruned = download_history.prune(disk_history, watched=watched, keep_days=keep_days, max_items=max_items)
        if disk_rebuilt:
            run.record("download_history_rebuilt", component="storage", status="rebuilt", how=run.how)
        if not disk_rebuilt and not changed and not pruned:
            # Nothing new in the history (most progress passes): one atomic state write is
            # enough - no two-file journal, no backup copy of a half-megabyte file.
            commit_state(False)
            return False
        # A rebuilt history is written even when empty: the next run reads it without a warning.
        with check_transaction.check_store_transaction() as transaction:
            save_download_history(disk_history)
            transaction.mark_history_committed()
            commit_state(disk_rebuilt)
            transaction.mark_committed()
        return transaction.cleanup_pending


def _reconcile_all(
    state: dict[str, Any],
    results: list[dict[str, Any]],
    run: Any,
    history: DownloadHistory,
    records: list[tuple[str, dict[str, Any]]],
    want: set[str] | None,
) -> None:
    result_by_id = {str(row.get("id")): row for row in results}
    for topic in topics_of(state):
        if want is not None and str(topic.get("id")) not in want:
            continue
        row = result_by_id.get(str(topic.get("id"))) or {}
        if not topic.get("hash"):
            continue
        _reconcile_one(topic, row, run, history, records)


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


def run_check(
    *,
    apply: bool,
    notify: bool,
    ids: list[str] | None = None,
    ignore_cool: bool = False,
    how: str = "auto",
    progress_only: bool = False,
    wait: bool = True,
    scheduled_scope: str = "",
) -> dict[str, Any]:
    """Run one check; applying checks never overlap (scheduled, progress, web, CLI).

    ``wait=False`` (web requests): raise ``CheckBusyError`` at once when another applying
    check runs, instead of holding the request - and every page behind it - for minutes.

    ``check_run_lock`` serializes applying checks across processes for the whole run. The
    shared persistence lock is taken only to read the state and to commit the result, so
    edits in the web UI, the watchdog and the night copy are not held up by network time;
    edits made meanwhile are kept (``merge_check_results``).
    """
    if scheduled_scope not in {"", "global", "timer"} or (progress_only and scheduled_scope):
        raise ValueError("invalid scheduled check scope")
    if not apply:
        return _run_check(
            apply=False,
            notify=notify,
            ids=ids,
            ignore_cool=ignore_cool,
            how=how,
            progress_only=progress_only,
            scheduled_scope=scheduled_scope,
        )
    with check_run_lock(wait=wait):
        return _run_check(
            apply=True,
            notify=notify,
            ids=ids,
            ignore_cool=ignore_cool,
            how=how,
            progress_only=progress_only,
            scheduled_scope=scheduled_scope,
        )


def _reconcile_one(
    topic: Topic,
    row: dict[str, Any],
    run: CheckRun,
    history: DownloadHistory,
    pending_records: list[tuple[str, dict[str, Any]]],
) -> None:
    """Reconcile one topic with its client: progress events, client presence (B4)."""
    client_id, topic_client = run.get_client_for(topic)
    checked_ok = bool(row.get("ok")) and row.get("status") != "skipped"
    if topic_client is None:
        if checked_ok:
            # B4: without its client the topic is not "ok" - TOW cannot see its torrent.
            fail_row(topic, row, client_unreachable(run, client_id))
        return
    history_topics = history.setdefault("topics", {})
    topic_key = str(topic.get("id"))
    if row.get("hash_identity_from"):
        # Same torrent, new hash label: its files are not a new revision. Kept even if
        # the reconcile below fails, because the topic already carries the new label.
        _relabel_source_hash(history_topics.get(topic_key), str(row["hash_identity_from"]), str(topic.get("hash")))
    had_history = topic_key in history_topics
    topic_history_before = copy.deepcopy(history_topics.get(topic_key))
    try:
        reconcile = reconcile_topic(topic, topic_client, history, now=iso_now())
    except Exception as exc:  # noqa: BLE001 - a failed reconcile is the topic's result (recorded), never the run's end
        if had_history:
            history_topics[topic_key] = cast(HistoryRecord, topic_history_before)
        else:
            history_topics.pop(topic_key, None)
        mark_reconcile_failure(row, exc)
        stamp_result(topic, row)
        topic["last_error_class"] = str(row["reconcile_error"] or "error")
        run.queue_notification(
            topic,
            kind="error",
            operation_id=new_operation_id("reconcile-error"),
            tracker=str(row.get("tracker") or ""),
            error=row_error(row),
            error_cls=topic["last_error_class"],
        )
        run.record(
            "reconcile_failed",
            operation_id=new_operation_id("reconcile"),
            component="client/filesystem",
            integration_id=client_id,
            client_id=client_id,
            client_kind=topic_client.client_kind,
            topic_id=topic.get("id"),
            topic=topic.get("id"),
            hash=topic.get("hash"),
            status="failed",
            **error_fields(exc),
            how=run.how,
        )
        return
    for event_kind in reconcile.get("events") or []:
        operation_id = new_operation_id("reconcile")
        pending_records.append(
            (
                event_kind,
                {
                    "operation_id": operation_id,
                    "component": "client/filesystem",
                    "integration_id": client_id,
                    "client_id": client_id,
                    "client_kind": topic_client.client_kind,
                    "topic_id": topic.get("id"),
                    "topic": topic.get("id"),
                    "hash": topic.get("hash"),
                    "status": "succeeded",
                    "how": run.how,
                },
            )
        )
        notification = reconcile_notification(event_kind, reconcile)
        if run.notify and notification:
            notify_kind, episodes = notification
            run.queue_notification(
                topic,
                kind=notify_kind,
                operation_id=operation_id,
                tracker=str(row.get("tracker") or ""),
                episodes=episodes,
            )
    _note_season_complete(topic, row, run, history_topics.get(topic_key), reconcile)
    if checked_ok and (history_topics.get(topic_key) or {}).get("client_present") is False:
        # B4: the torrent was removed from the client; "раздача исчезла" was sent once.
        fail_row(topic, row, TowError("check.removed_from_client"))


def _note_season_complete(
    topic: Topic, row: dict[str, Any], run: CheckRun, record: HistoryRecord | None, reconcile: dict[str, Any]
) -> None:
    """G5: "сезон собран N/N" once, when the last expected episode completes in this run.

    A topic that was already complete (e.g. on the first run after an upgrade) is marked
    silently; a season that becomes incomplete again (new episodes announced) is re-armed.
    """
    if not isinstance(record, dict):
        return
    summary = reconcile.get("summary") or {}
    if not (summary.get("is_complete") and summary.get("expected")):
        record.pop("season_complete_at", None)
        return
    if record.get("season_complete_at"):
        return
    record["season_complete_at"] = iso_now()
    events = set(reconcile.get("events") or [])
    if events & {"episode_completed", "file_completed"}:
        run.queue_notification(
            topic,
            kind="season_complete",
            operation_id=new_operation_id("bot"),
            tracker=str(row.get("tracker") or ""),
            episodes=f"{summary['completed']}/{summary['expected']}",
        )


def _queue_recoveries(state: dict[str, Any], results: list[dict[str, Any]], run: CheckRun) -> None:
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


def _today() -> str:
    """The local date (YYYY-MM-DD) a daily download limit belongs to."""
    return iso_now()[:10]


def _daily_limits(
    limited_today: set[str], quota: set[str], results: list[dict[str, Any]], today: str
) -> dict[str, str]:
    """{site: today} for the sites under their daily download limit after this run.

    A manual check that reached a limited site without hitting the limit ends it early.
    """
    tried = {str(row.get("tracker")) for row in results if row.get("tracker")}
    kept = {name for name in limited_today if name in quota or name not in tried}
    return dict.fromkeys(sorted(kept | quota), today)


def _store_daily_limits(state: dict[str, Any], limits: dict[str, str]) -> None:
    if limits:
        state["daily_limit"] = limits  # yesterday's entries are dropped
    else:
        state.pop("daily_limit", None)


# Checks in a row a site's transport trouble (an amber class) lasts before the owner hears of it.
TRACKER_ERROR_NOTIFY_AFTER = 3


def _streak(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


@dataclass
class _RunNotifications:
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


def _load_run_state(*, apply: bool) -> dict[str, Any]:
    """The state a run works on: an apply first finishes an interrupted commit; a dry run
    works on a copy (it never writes)."""
    if apply:
        with persistence_lock():
            check_transaction.recover_check_transaction()
            return load_state(quarantine=True)
    return copy.deepcopy(load_state(quarantine=False))


def _flush_notifications(cfg: dict[str, Any], secrets: dict[str, Any], *, how: str, notify: bool) -> None:
    """G4: grouped per tracker, with links, held during quiet hours, plus the digest. The
    messages were staged in the commit; here they are handed to the messengers."""
    from tow import delivery

    def send(*, text: str, operation_id: str, topic: Topic | None) -> bool:
        return _audited_send(secrets, text=text, operation_id=operation_id, topic=topic, how=how)

    delivery.dispatch(send)
    if notify:
        delivery.maybe_digest(cfg=cfg, send=send)


@dataclass
class _RunResult:
    """What a run hands to its commit."""

    state: dict[str, Any]
    results: list[CheckRow]
    started: dict[str, tuple[Any, ...]]  # owner fields per topic when the run started
    pool: ClientPool
    notifications: NotificationBatch
    cfg: dict[str, Any]
    how: str
    limited_today: set[str]
    quota: set[str]
    today: str


def _commit_run_state(result: _RunResult, history_rebuilt: bool = False) -> None:
    """Write the run's result onto the state as it is on disk now (the owner's edits made
    meanwhile are kept), with the health record and the staged messages, in one write."""
    state, pool, how = result.state, result.pool, result.how
    health = Health(
        qbit=pool.ping,
        client=pool.ping,
        qbit_ok=pool.default_ok,
        at=rows.now(),
        at_ts=int(machine_now().timestamp()),
    )
    state["health"] = health
    # The header countdown runs from the last *scheduled* check; a manual check of
    # one topic must not move it.
    previous_health = health_of(load_state(quarantine=False))
    _note_history_rebuilt(health, previous_health, rebuilt=history_rebuilt)
    health["auto_at_ts"] = health["at_ts"] if how == "auto" else previous_health.get("auto_at_ts")
    health["auto_ok_at_ts"] = health["at_ts"] if how == "auto" else _last_good_scheduled_check(previous_health)
    health["clients_ok"] = pool.health()
    disk = load_state()
    edited = {
        str(topic.get("id")): changed
        for topic in topics_of(disk)
        if (changed := changed_owner_fields(result.started.get(str(topic.get("id"))), topic))
    }
    merge_check_results(disk, state, edited=edited)
    disk["health"] = health
    _store_daily_limits(disk, _daily_limits(result.limited_today, result.quota, result.results, result.today))
    # Staged in the same write as the result: a TOW stopped before sending loses nothing.
    from tow import delivery

    delivery.stage(disk, list(result.notifications), cfg=result.cfg)
    save_state(disk)


def _run_check(
    *,
    apply: bool,
    notify: bool,
    ids: list[str] | None = None,
    ignore_cool: bool = False,
    how: str = "auto",
    progress_only: bool = False,
    scheduled_scope: str = "",
) -> dict[str, Any]:
    cfg = load_config()
    secrets, secrets_stamp = read_secrets()
    state = _load_run_state(apply=apply)
    started = {str(topic.get("id")): owner_fields(topic) for topic in topics_of(state)}
    notify = bool(notify and apply)
    trackers = load_trackers(cfg)
    ua = cfg.get("user_agent")
    batch = NotificationBatch()
    pending_reconcile_records: list[tuple[str, dict[str, Any]]] = []
    previous_health = health_of(state)

    def _record(kind: str, **fields: Any) -> None:
        if apply:
            log_event(kind, **fields)

    default_id = client_factory.default_client_id(cfg)
    pool = ClientPool(
        cfg=cfg,
        apply=apply,
        notify=notify,
        how=how,
        record=_record,
        batch=batch,
        default_id=default_id,
        previous_qbit_ok=previous_health.get("qbit_ok"),
        previous_clients_ok=dict(previous_health.get("clients_ok") or {}),
        expect_torrents=frozenset(
            str(topic.get("client_id") or default_id)
            for topic in topics_of(state)
            if topic.get("hash") and topic.get("last_ok") is True and not topic.get("paused")
        ),
    )
    notifications = _RunNotifications(
        enabled=notify,
        batch=batch,
        previous_error_class={
            str(topic.get("id")): (str(topic.get("last_error_class") or "error") if topic.get("last_error") else "")
            for topic in topics_of(state)
        },
        client_errors=pool.errors,
        default_client_id=pool.default_id,
    )
    pool.open_default(secrets)
    want = {str(x) for x in ids} if ids is not None else None
    if scheduled_scope:
        from tow.supervisor import layout
        from tow.topic_timers import batch_ids, interval_of

        selected = (
            set(batch_ids(state, layout.read_json(layout.schedule_path())))
            if scheduled_scope == "timer"
            else {str(topic.get("id")) for topic in topics_of(state) if interval_of(topic) is None}
        )
        want = selected if want is None else want & selected
    # A site that said "download limit for today" is left alone until the next local day: the
    # limit is kept in the state, so the next scheduled runs do not ask it again. A manual
    # check (the owner pressed the button) still tries.
    today = _today()
    limited_today = {str(name) for name, day in as_dict(state.get("daily_limit")).items() if day == today}
    quota: set[str] = set(limited_today) if how in {"auto", "timer"} else set()
    run = CheckRun(
        apply=apply,
        notify=notify,
        how=how,
        ignore_cool=ignore_cool,
        ua=ua,
        state=state,
        trackers=trackers,
        quota=quota,
        secrets=secrets,
        client_errors=pool.errors,
        get_client_for=lambda topic: pool.get(topic, run.secrets),
        record=_record,
        queue_notification=notifications.queue,
        history_retention=download_history.retention(cfg),
        remote_clients=remote_clients(cfg, secrets),
        secrets_stamp=secrets_stamp,
    )
    results: list[CheckRow] = []
    for topic in topics_of(state):
        if want is not None and str(topic.get("id")) not in want:
            continue
        # G2: a progress-only pass asks qBittorrent alone - no tracker traffic at all.
        results.append(_progress_only_row(topic) if progress_only else check_topic(topic, run))
    secrets = run.secrets  # the per-topic step may have re-read tracker cookies
    pool.queue_recovered()
    if not apply:
        history, _rebuilt = _load_history(quarantine=False)
        _reconcile_all(state, results, run, history, pending_reconcile_records, want)
        _queue_recoveries(state, results, run)
        return {"qbit": pool.ping, "results": results, "preview": True}

    outcome = _RunResult(state, results, started, pool, batch, cfg, how, limited_today, quota, today)
    cleanup_pending = _reconcile_and_commit(
        state,
        results,
        run,
        pending_reconcile_records,
        want,
        lambda rebuilt: _commit_run_state(outcome, rebuilt),
    )
    for event_kind, fields in pending_reconcile_records:
        _record(event_kind, **fields)
    _flush_notifications(cfg, secrets, how=how, notify=notify)
    if cleanup_pending:
        _record("check_persistence_cleanup_pending", how=how)
    _record("check", apply=apply, ok=sum(1 for r in results if r.get("ok")), n=len(results), how=how)
    return {"qbit": pool.ping, "results": results}
