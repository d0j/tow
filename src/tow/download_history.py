"""How long the download history keeps what nobody looks at any more.

download_history.json grew forever: every older revision's files and every deleted topic's
record stayed. The check prunes it when it commits:

- a topic TOW no longer watches keeps its record ``history_keep_days`` after its last scan
  (an undone delete within that time finds its history intact);
- files of older revisions, and files gone from the torrent (``superseded``), go after
  ``history_keep_days``; beyond ``history_max_items`` per topic the oldest of them go first.

What the topic's current torrent has is never dropped. Both limits are config.yaml keys;
0 turns a limit off. The defaults are generous: two years, 5000 files per topic.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from tow.clock import parse_timestamp
from tow.records import DownloadHistory, HistoryItem, HistoryRecord

DEFAULT_KEEP_DAYS = 730
DEFAULT_MAX_ITEMS = 5000
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _limit(cfg: Mapping[str, Any], key: str, default: int) -> int:
    value = cfg.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return default
    return value


def retention(cfg: Mapping[str, Any]) -> tuple[int, int]:
    """(days, items per topic) from config.yaml; a missing or wrong value takes the default."""
    return _limit(cfg, "history_keep_days", DEFAULT_KEEP_DAYS), _limit(cfg, "history_max_items", DEFAULT_MAX_ITEMS)


def _moment(value: Any) -> datetime | None:
    try:
        return parse_timestamp(str(value)) if value else None
    except TypeError, ValueError, OverflowError, OSError:
        return None


def _last_seen(item: HistoryItem) -> datetime | None:
    """When the item last meant something: completed, else first seen (None: unknown, kept)."""
    moments = [m for m in (_moment(item.get("completed_observed_at")), _moment(item.get("first_seen_at"))) if m]
    return max(moments) if moments else None


def _prune_items(record: HistoryRecord, *, cutoff: datetime | None, max_items: int) -> bool:
    items = record.get("items")
    if not isinstance(items, dict):
        return False
    old = [
        (identity, seen)
        for identity, item in items.items()
        if isinstance(item, dict) and item.get("superseded") is True
        for seen in (_last_seen(item),)
    ]
    drop = {identity for identity, seen in old if cutoff is not None and seen is not None and seen < cutoff}
    if max_items and len(items) - len(drop) > max_items:
        # The oldest superseded first (an unknown age counts as the oldest).
        rest = sorted(
            ((identity, seen) for identity, seen in old if identity not in drop),
            key=lambda pair: (pair[1] is not None, pair[1] or _EPOCH),
        )
        excess = len(items) - len(drop) - max_items
        drop.update(identity for identity, _seen in rest[:excess])
    for identity in drop:
        del items[identity]
    return bool(drop)


def prune(
    history: DownloadHistory,
    *,
    watched: Collection[str],
    now: datetime | None = None,
    keep_days: int = DEFAULT_KEEP_DAYS,
    max_items: int = DEFAULT_MAX_ITEMS,
) -> bool:
    """Drop the records and items the policy above lets go; True when anything was dropped.

    ``watched``: the ids of the topics TOW watches now (their records always stay)."""
    topics = history.get("topics")
    if not isinstance(topics, dict):
        return False
    cutoff = (now or datetime.now(UTC)) - timedelta(days=keep_days) if keep_days else None
    changed = False
    for topic_id in list(topics):
        record = topics[topic_id]
        if not isinstance(record, dict):
            continue
        if topic_id not in watched and cutoff is not None:
            scanned = _moment(record.get("last_scan_at"))
            if scanned is not None and scanned < cutoff:
                del topics[topic_id]
                changed = True
                continue
        changed = _prune_items(record, cutoff=cutoff, max_items=max_items) or changed
    return changed
