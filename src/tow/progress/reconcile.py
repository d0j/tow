"""The reconcile pass: one topic's torrent in its client and its files on disk, recorded in the
download history as items and events."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tow.clients.spec import TorrentClientAdapter
from tow.clock import iso_from_epoch
from tow.episodes import expected_for_topic, item_identity
from tow.errors import Msg, TowError
from tow.folders import paths_equal
from tow.i18n import t
from tow.log import owner_language
from tow.progress.confirmation import CONFIRMATION_REUSE_SEC, filesystem_confirmation, safe_relative_path
from tow.progress.expected import preferred_season_for, selected_files, selection_fingerprint, selection_is_current
from tow.progress.history import HistoryIndex, adopt_previous_item, carry_completion, path_key, revision_identity
from tow.progress.labels import (
    episode_keys_label,
    episode_keys_label_msg,
    item_label,
    label_fields,
    latest_item_label,
    repair_last_event_semantics,
    set_label,
    set_latest_event,
)
from tow.progress.presence import mark_superseded, track_client_presence
from tow.progress.summary import completed_episode_keys, summary_record
from tow.records import DownloadHistory, HistoryItem, HistoryRecord, Topic


def _get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _client_event(info: Any, client_adapter: TorrentClientAdapter) -> dict[str, Any] | None:
    completion_at = iso_from_epoch(_get(info, "completion_on"))
    added_at = iso_from_epoch(_get(info, "added_on"))
    if completion_at:
        kind = "torrent_completed"
        at = completion_at
    elif added_at:
        kind = "client_added"
        at = added_at
    else:
        return None
    return {
        "kind": kind,
        "label": None,
        "at": at,
        "source": "client",
        "client_id": client_adapter.client_id,
        "client_kind": client_adapter.client_kind,
    }


def _repair_synthetic_baseline_completion(record: HistoryRecord) -> None:
    repaired = set()
    for item in (record.get("items") or {}).values():
        if item.get("new_after_baseline") is not False:
            continue
        observed_at = item.get("completed_observed_at")
        if observed_at and observed_at == item.get("first_seen_at"):
            repaired.add(item.get("identity"))
            item.pop("completed_observed_at", None)
            item["completion_seen"] = True
    last = record.get("last_completed")
    if isinstance(last, dict) and last.get("identity") in repaired:
        record.pop("last_completed", None)


def _check_save_path(topic: Topic, info: Any) -> str:
    """The folder whose files are evidence; the client must agree with the topic.

    A move the owner started (move_pending) is accepted while the client still
    moves or still reports the old folder; the marker is dropped once they agree.
    """
    topic_save_path = str(topic.get("save_path") or "")
    client_save_path = str(_get(info, "save_path", "") or "")
    move_pending = topic.get("move_pending") if isinstance(topic.get("move_pending"), dict) else None
    if client_save_path and topic_save_path and not paths_equal(client_save_path, topic_save_path):
        client_state = str(_get(info, "state", "") or "").casefold()
        still_moving = bool(
            move_pending
            and paths_equal(str(move_pending.get("to") or ""), topic_save_path)
            and (client_state == "moving" or paths_equal(client_save_path, str(move_pending.get("from") or "")))
        )
        if not still_moving:
            raise TowError("progress.path_differs")
    elif move_pending and client_save_path and paths_equal(client_save_path, str(move_pending.get("to") or "")):
        # Only what this check saw ends the move: the client now reports the folder it was sent to.
        topic.pop("move_pending", None)
    return client_save_path or topic_save_path


def _client_files(files: list[Any], evidence_save_path: str) -> list[tuple[Any, str, int | None, bool]]:
    """(row, safe relative path, size, selected) for every usable client file row."""
    prepared: list[tuple[Any, str, int | None, bool]] = []
    for row in files:
        priority = _get(row, "priority")
        try:
            selected = priority is None or int(priority) != 0
        except TypeError, ValueError:
            continue
        rel = safe_relative_path(evidence_save_path, str(_get(row, "name", "") or ""))
        if not rel:
            continue
        size = _get(row, "size")
        try:
            size = int(size) if size is not None else None
        except TypeError, ValueError:
            size = None
        prepared.append((row, rel, size, selected))
    return prepared


def _file_progress(row: Any, info: Any) -> float:
    """Per-file progress (0..1), falling back to the torrent's progress."""
    progress = _get(row, "progress", _get(info, "progress", 0.0))
    if isinstance(progress, bool) or not isinstance(progress, (int, float, str)):
        return 0.0
    try:
        value = float(progress)
        return value if math.isfinite(value) and 0.0 <= value <= 1.0 else 0.0
    except TypeError, ValueError, OverflowError:
        return 0.0


@dataclass
class _ReconcilePass:
    """One topic's reconcile in progress: what its per-file steps share and collect."""

    topic: Topic
    client: TorrentClientAdapter
    record: HistoryRecord
    items: dict[str, HistoryItem]
    info: Any
    now: str
    initial_baseline: bool
    evidence_save_path: str
    current_client_paths: set[str]
    events: list[str]
    index: HistoryIndex = field(init=False)
    orphans_by_size: dict[Any, list[str]] = field(init=False)
    observed_completed_episode_keys: set[str] = field(default_factory=set)
    completed_file_labels: list[str] = field(default_factory=list)
    seen_identities: set[str] = field(default_factory=set)
    _evidence_base: tuple[Path | None] | None = field(default=None, init=False)

    def evidence_base(self) -> Path | None:
        """The save folder resolved once per pass, and only when a file is looked at on disk."""
        if self._evidence_base is None:
            try:
                self._evidence_base = (Path(self.evidence_save_path).resolve(),)
            except OSError, ValueError:
                self._evidence_base = (None,)
        return self._evidence_base[0]

    def __post_init__(self) -> None:
        self.index = HistoryIndex(self.items)
        self.orphans_by_size = {}
        for old_identity, old_item in self.items.items():
            if isinstance(old_item, dict) and path_key(old_item) not in self.current_client_paths:
                self.orphans_by_size.setdefault(old_item.get("size"), []).append(old_identity)

    @property
    def source_hash(self) -> str:
        return str(self.topic.get("hash") or "")

    def event(self, kind: str, label: Msg | str | None, **fields: Any) -> dict[str, Any]:
        """An event TOW observed now (``fields``: what it is about)."""
        return {
            "kind": kind,
            **label_fields(label),
            "at": self.now,
            "source": "tow",
            **fields,
            "client_id": self.client.client_id,
            "client_kind": self.client.client_kind,
        }


def _open_record(topic: Topic, history: DownloadHistory) -> tuple[HistoryRecord, bool, dict[str, Any] | None]:
    """The topic's history record (created on first sight), whether this is its first scan
    (the baseline: nothing seen then is "new"), and how many episodes are expected."""
    topics = history.setdefault("topics", {})
    record = topics.setdefault(str(topic.get("id") or ""), {"items": {}})
    initial_baseline = "baseline_at" not in record
    record.setdefault("items", {})
    _repair_synthetic_baseline_completion(record)
    expected = expected_for_topic(topic) if selection_is_current(topic) else None
    previous = record.get("expected")
    fingerprint = selection_fingerprint(topic)
    if fingerprint and previous and previous.get("selection_fingerprint") == fingerprint:
        expected = previous
    if expected:
        record["expected"] = expected
    else:
        record.pop("expected", None)
    return record, initial_baseline, expected


def _track_item(
    work: _ReconcilePass, rel: str, size: int | None, coverage: tuple[Any, ...]
) -> tuple[HistoryItem, HistoryItem | None]:
    """The history item of one client file (adopted, carried over or new), and for a new item
    the completed item of an older revision whose completion it carries."""
    items, index = work.items, work.index
    label = coverage[0] if coverage else None
    display_label = episode_keys_label({entry.key for entry in coverage}) if coverage else Path(rel).name
    base_identity = item_identity(rel, label, size)
    current_source_hash = work.source_hash.casefold()
    adopt_previous_item(
        items,
        base_identity,
        rel=rel,
        size=size,
        label=label,
        coverage=coverage,
        current_source_hash=current_source_hash,
        current_client_paths=work.current_client_paths,
        index=index,
        orphans_by_size=work.orphans_by_size,
    )
    source_hash = work.source_hash
    identity, is_revision = revision_identity(items, base_identity, rel, source_hash, index)
    is_new = identity not in items
    carried_completion = carry_completion(identity, rel, size, current_source_hash, index) if is_new else None
    item = items.setdefault(
        identity,
        {
            "identity": identity,
            "kind": "episode" if label else "file",
            "label": display_label,
            "episode_key": label.key if label else None,
            "episode_keys": [item.key for item in coverage],
            "relative_path": rel,
            "client_id": work.client.client_id,
            "client_kind": work.client.client_kind,
            "source_hash": str(work.topic.get("hash") or ""),
            "size": size,
            "first_seen_at": work.now,
            "status": "seen",
            "new_after_baseline": bool(work.record.get("baseline_at")) and not work.initial_baseline,
        },
    )
    work.seen_identities.add(identity)
    item.update(
        {
            "kind": "episode" if label else "file",
            "label": display_label,
            "episode_key": label.key if label else None,
            "episode_keys": [entry.key for entry in coverage],
            "relative_path": rel,
            "source_hash": source_hash,
            "size": size,
            "superseded": False,
        }
    )
    index.add(identity)
    if carried_completion is not None and is_new:
        item["completion_carried_from"] = carried_completion.get("identity")
        if carried_completion.get("completed_observed_at"):
            item["completed_observed_at"] = carried_completion["completed_observed_at"]
    if item.get("first_seen_at") is None:
        item["first_seen_at"] = work.now
    if is_new and item.get("new_after_baseline"):
        event_name = "revision_updated" if is_revision else "new_file"
        work.events.append(event_name)
        set_latest_event(
            work.record,
            work.event(event_name, item_label(item), item=item.get("identity"), relative_path=rel),
        )
    return item, carried_completion


def _file_confirmed(
    work: _ReconcilePass, item: HistoryItem, rel: str, size: int | None, progress: float
) -> bool | None:
    """Is the file on disk in full? A file confirmed within the day, still complete in the
    client, is not looked at again: every 30 minutes woke sleeping drives and a NAS for nothing."""
    confirmed_ts = item.get("confirmed_ts")
    fresh = (
        item.get("status") == "completed"
        and progress >= 1.0
        and isinstance(confirmed_ts, (int, float))
        and 0 <= time.time() - float(confirmed_ts) < CONFIRMATION_REUSE_SEC
    )
    confirmed = (
        True if fresh else filesystem_confirmation(work.evidence_save_path, rel, size, base=work.evidence_base())
    )
    if confirmed is True and not fresh:
        item["confirmed_ts"] = int(time.time())
    return confirmed


def _update_item_status(
    work: _ReconcilePass,
    item: HistoryItem,
    row: Any,
    rel: str,
    size: int | None,
    coverage: tuple[Any, ...],
    carried_completion: HistoryItem | None,
) -> None:
    """The item's progress and status from the client and the disk; a completion seen for
    the first time (after the baseline) becomes an event."""
    progress = _file_progress(row, work.info)
    item["progress"] = progress
    if item.get("status") == "completed":
        item["completion_seen"] = True
    completed_before = bool(
        item.get("completion_seen")
        or item.get("completed_observed_at")
        or item.get("status") == "completed"
        or carried_completion is not None
    )
    confirmed = _file_confirmed(work, item, rel, size, progress)
    item.pop("verification_unavailable", None)
    if progress >= 1.0 and confirmed is True:
        item["status"] = "completed"
        item["completion_seen"] = True
        if not completed_before and not work.initial_baseline:
            _note_completion(work, item, rel, coverage)
    elif confirmed is None:
        item["status"] = "unverified"
        item["verification_unavailable"] = True
    elif progress >= 1.0:
        item["status"] = "missing"
    else:
        item["status"] = "downloading" if progress > 0 else "seen"
    completion_on = _get(work.info, "completion_on")
    if completion_on:
        item["torrent_completed_at"] = completion_on


def _note_completion(work: _ReconcilePass, item: HistoryItem, rel: str, coverage: tuple[Any, ...]) -> None:
    """A file completed in this run: an episode counts towards "episodes completed", another
    file is its own event. A completion carried over from an older revision is silent."""
    if item.get("completion_carried_from"):
        return
    item["completed_observed_at"] = work.now
    label = coverage[0] if coverage else None
    if label:
        work.observed_completed_episode_keys.update(entry.key for entry in coverage)
        return
    work.events.append("file_completed")
    work.completed_file_labels.append(str(item.get("label") or t("progress.file", owner_language())))
    set_latest_event(
        work.record,
        work.event("file_completed", item_label(item), item=item.get("identity"), relative_path=rel),
    )


def _note_completed_episodes(work: _ReconcilePass) -> str:
    """The episodes completed in this run, as one event; returns their label ("" for none)."""
    newly_completed = work.observed_completed_episode_keys.intersection(completed_episode_keys(work.items))
    if not newly_completed or work.initial_baseline:
        return ""
    work.events.append("episode_completed")
    message = episode_keys_label_msg(newly_completed)
    set_latest_event(work.record, work.event("episode_completed", message))
    return str(message)


def reconcile_topic(
    topic: Topic,
    client_adapter: TorrentClientAdapter,
    history: DownloadHistory,
    now: str,
) -> dict[str, Any]:
    """Look at the topic's torrent in its client and its files on disk; record in ``history``
    what changed (new files, completions, the torrent gone or back) and return the events."""
    record, initial_baseline, expected = _open_record(topic, history)
    events: list[str] = []
    if (client_adapter.capabilities or {}).get("inspect") is False:
        record["last_scan_at"] = now
        return {"summary": summary_record(record, expected), "events": events}

    # An observation, not a read-back: a client that can answers it from one listing per run.
    observe = getattr(client_adapter, "observe_torrent", None) or client_adapter.inspect_torrent
    info = observe(str(topic.get("hash") or ""))
    record["last_scan_at"] = now
    if not track_client_presence(record, info, client_adapter, now, events):
        return {"summary": summary_record(record, expected), "events": events}
    client_event = _client_event(info, client_adapter)

    files = _get(info, "files", []) or []
    if files and initial_baseline:
        record["baseline_at"] = now
    items = record["items"]
    current_source_hash = str(topic.get("hash") or "").casefold()
    mark_superseded(items, current_source_hash, has_files=bool(files))
    evidence_save_path = _check_save_path(topic, info)
    selected_episode_keys = [str(value) for value in topic.get("selected_episode_keys") or []]
    preferred_season = preferred_season_for(topic, selected_episode_keys)
    prepared_all = _client_files(files, evidence_save_path)
    prepared, expected = selected_files(
        topic,
        record,
        expected,
        prepared_all,
        preferred_season,
        content_path=str(_get(info, "content_path", "") or ""),
        save_path=evidence_save_path,
    )
    work = _ReconcilePass(
        topic=topic,
        client=client_adapter,
        record=record,
        items=items,
        info=info,
        now=now,
        initial_baseline=initial_baseline,
        evidence_save_path=evidence_save_path,
        current_client_paths={rel.casefold() for _row, rel, _size, _selected in prepared_all},
        events=events,
    )
    for (row, rel, size), coverage in prepared:
        item, carried_completion = _track_item(work, rel, size, coverage)
        _update_item_status(work, item, row, rel, size, coverage, carried_completion)

    if files:
        for identity, historical_item in items.items():
            if (
                str(historical_item.get("source_hash") or "").casefold() == current_source_hash
                and identity not in work.seen_identities
            ):
                historical_item["superseded"] = True

    completed_episode_label = _note_completed_episodes(work)
    if client_event:
        set_label(client_event, latest_item_label(record, prefer_completed=client_event.get("kind") != "client_added"))
        set_latest_event(record, client_event)
    repair_last_event_semantics(record)
    summary = summary_record(record, expected)
    labels = work.completed_file_labels
    return {
        "summary": summary,
        "events": list(dict.fromkeys(events)),
        "completed_episode_label": completed_episode_label,
        "completed_file_label": (labels[0] if len(labels) == 1 else t("progress.files", owner_language()))
        if labels
        else "",
    }
