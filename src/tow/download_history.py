"""How long the download history keeps what nobody looks at any more.

download_history.json grew forever: every older revision's files and every deleted topic's
record stayed. The check prunes it when it commits:

- a topic TOW no longer watches keeps its record ``history_keep_days`` after its last scan
  (an undone delete within that time finds its history intact);
- files of older revisions, and files gone from the torrent (``superseded``), go after
  ``history_keep_days``; beyond ``history_max_items`` per topic the oldest of them go first.

What the topic's current torrent has is never dropped by them. Both limits are config.yaml
keys; 0 turns a limit off. The defaults are generous: two years, 5000 files per topic.

The history also travels in every restore point (and so in every update from Settings) and
export, whose bundle holds at most 64 MiB (``tow.bundle.io.MAX_BUNDLE_BYTES``). Past
``MAX_BYTES`` the check makes room (``fit``): first the records of topics no longer watched,
then files of older versions, the oldest first, whatever their age; if the current files alone
are still too many, the largest watched topic's record starts over (never single current
files: one dropped would come back as new on the next check and be announced again), and the
Log and Needs attention say so.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from tow.clock import parse_timestamp
from tow.records import DownloadHistory, HistoryItem, HistoryRecord

DEFAULT_KEEP_DAYS = 730
DEFAULT_MAX_ITEMS = 5000
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
# The most the history may take (compact JSON, as the store writes it) inside a bundle of
# 64 MiB: the rest is room for state.json, config.yaml, the secrets and the event log.
MAX_BYTES = 40 * 1024 * 1024


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


def _size(value: Any) -> int:
    """Bytes of ``value`` as compact JSON, as the store writes it."""
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _member_size(key: str, value: Any) -> int:
    """At least the bytes dropping ``key`` from a mapping saves (``"key":value``; its comma not counted)."""
    return _size({key: value}) - 2


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, dict) else {}


def _oldest_first(seen: datetime | None) -> tuple[bool, datetime]:
    return seen is not None, seen or _EPOCH  # an unknown time counts as the oldest


def fit(history: DownloadHistory, *, watched: Collection[str], limit: int | None = None) -> tuple[bool, list[str]]:
    """Make the history fit in ``limit`` bytes (``MAX_BYTES``; see the module's text for the
    order). Returns whether anything was dropped and the ids of watched topics whose record
    started over."""
    limit = MAX_BYTES if limit is None else limit
    topics = history.get("topics")
    if not isinstance(topics, dict):
        return False, []
    over = _size(history) + 1 - limit  # and the store's final line break
    if over <= 0:
        return False, []
    gone = sorted(
        (topic_id for topic_id in topics if topic_id not in watched),
        key=lambda topic_id: _oldest_first(_moment(_mapping(topics[topic_id]).get("last_scan_at"))),
    )
    for topic_id in gone:
        if over <= 0:
            break
        over -= _member_size(topic_id, topics.pop(topic_id))
    if over > 0:
        old = sorted(
            (
                (_oldest_first(_last_seen(item)), topic_id, identity)
                for topic_id, record in topics.items()
                if isinstance(record, dict) and isinstance(record.get("items"), dict)
                for identity, item in record["items"].items()
                if isinstance(item, dict) and item.get("superseded") is True
            ),
            key=lambda entry: entry[0],
        )
        for _seen, topic_id, identity in old:
            if over <= 0:
                break
            over -= _member_size(identity, topics[topic_id]["items"].pop(identity))
    started_over: list[str] = []
    for topic_id in sorted(topics, key=lambda key: _size(topics[key]), reverse=True):
        if over <= 0:
            break
        over -= _member_size(topic_id, topics.pop(topic_id))
        started_over.append(topic_id)
    return True, started_over


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
