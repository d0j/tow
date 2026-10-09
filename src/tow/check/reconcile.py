"""After the topics are checked: reconcile them with their clients (progress, presence) and
commit the download history with the state."""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from typing import Any, cast

from tow import check_transaction, download_history
from tow.check.client_ops import client_unreachable
from tow.check.notices import queue_recoveries
from tow.check.rows import fail_row, row_error, stamp_result
from tow.check.topic import CheckRun
from tow.check_steps import mark_reconcile_failure, reconcile_notification
from tow.clock import iso_now
from tow.errors import TowError
from tow.events import new_operation_id
from tow.jsonish import as_dict
from tow.log import error_fields
from tow.progress import reconcile_topic
from tow.records import DownloadHistory, HistoryRecord, Topic, topics_of
from tow.store import (
    StoreCorruptionError,
    StoreReadError,
    load_download_history,
    persistence_lock,
    save_download_history,
)

# The error of a failed progress reconcile (tow.check_steps.mark_reconcile_failure).
RECONCILE_FAILED = "check.reconcile_failed"


def _relabel_source_hash(record: HistoryRecord | None, old: str, new: str) -> None:
    for item in ((record or {}).get("items") or {}).values():
        if isinstance(item, dict) and str(item.get("source_hash") or "").casefold() == old.casefold():
            item["source_hash"] = new


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


def reconcile_and_commit(
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
    queue_recoveries(state, results, run)
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


def reconcile_preview(
    state: dict[str, Any],
    results: list[dict[str, Any]],
    run: CheckRun,
    records: list[tuple[str, dict[str, Any]]],
    want: set[str] | None,
) -> None:
    """A dry run reconciles with the history as it is on disk and writes nothing."""
    history, _rebuilt = _load_history(quarantine=False)
    _reconcile_all(state, results, run, history, records, want)
    queue_recoveries(state, results, run)


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
    present = (history_topics.get(topic_key) or {}).get("client_present") is not False
    if checked_ok and not present:
        # B4: the torrent was removed from the client; "раздача исчезла" was sent once.
        fail_row(topic, row, TowError("check.removed_from_client"))
    elif not checked_ok and row.get("ok") and present and topic.get("last_error_code") == RECONCILE_FAILED:
        # A reconcile failure that a progress pass (or a check that skipped the topic) set is
        # over once a reconcile works again: no real check may come for hours to clear it.
        stamp_result(topic, row)


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
