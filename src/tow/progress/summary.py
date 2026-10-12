"""The summary of a topic's history: completion counts, the last completed item and the
episodes complete in every file that covers them."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tow.clock import parse_timestamp
from tow.episodes import parse_episode_label, summarize_completion
from tow.records import HistoryItem, HistoryRecord


def _completion_key(item: HistoryItem) -> tuple[float, int, int, str]:
    try:
        timestamp = parse_timestamp(str(item.get("completed_observed_at"))).timestamp()
    except TypeError, ValueError, OverflowError, OSError:
        timestamp = 0.0
    label = parse_episode_label(str(item.get("label") or item.get("identity") or ""))
    return (
        timestamp,
        label.season if label and label.season is not None else -1,
        label.episode if label else -1,
        str(item.get("label") or item.get("identity") or ""),
    )


def summary_record(topic_record: HistoryRecord, expected: dict[str, Any] | None) -> dict[str, Any]:
    summary = summarize_completion(topic_record.get("items") or {}, expected)
    topic_record["summary"] = summary
    completed = [
        item
        for item in (topic_record.get("items") or {}).values()
        if not item.get("superseded") and item.get("status") == "completed" and item.get("completed_observed_at")
    ]
    if completed:
        topic_record["last_completed"] = max(completed, key=_completion_key)
    else:
        topic_record.pop("last_completed", None)
    return summary


def completed_episode_keys(items: dict[str, HistoryItem]) -> set[str]:
    groups: dict[str, list[bool]] = {}
    for item in items.values():
        if item.get("superseded"):
            continue
        keys = item.get("episode_keys") or ([str(item.get("episode_key"))] if item.get("episode_key") else [])
        if not keys and item.get("kind") != "file":
            label = parse_episode_label(str(item.get("label") or item.get("identity") or ""))
            keys = [label.key] if label else []
        for key in keys:
            groups.setdefault(str(key), []).append(item.get("status") == "completed")
    return {key for key, statuses in groups.items() if statuses and all(statuses)}


def progress_summary(topic_id: str, history: Mapping[str, Any]) -> dict[str, Any]:
    record = (history.get("topics") or {}).get(str(topic_id)) or {}
    return summarize_completion(record.get("items") or {}, record.get("expected"))
