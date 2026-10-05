"""Per-topic tracker cadence; manual and client-progress checks do not move it."""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Mapping
from typing import Any

from tow.diagnostic_json import epoch
from tow.errors import TowError

MAX_INTERVAL_MIN = 7 * 24 * 60


def parse_interval(raw: str) -> int | None:
    value = raw.strip()
    if not value:
        return None
    if not re.fullmatch(r"[0-9]{1,5}", value) or not 1 <= int(value) <= MAX_INTERVAL_MIN:
        raise TowError("timer.invalid_minutes", maximum=MAX_INTERVAL_MIN)
    return int(value)


def interval_of(topic: Mapping[str, Any]) -> int | None:
    value = topic.get("check_interval_min")
    if value is None:
        return None
    if type(value) is not int or not 1 <= value <= MAX_INTERVAL_MIN:
        raise TowError("timer.invalid_minutes", maximum=MAX_INTERVAL_MIN)
    revision = topic.get("check_timer_revision")
    if revision is not None and (
        not isinstance(revision, str) or not re.fullmatch(r"[a-zA-Z0-9:_\.\-]{1,128}", revision)
    ):
        raise ValueError("invalid timer revision")
    at = topic.get("check_timer_set_at_ts")
    if at is not None and (isinstance(at, str) or epoch(at) is None):
        raise ValueError("invalid timer timestamp")
    return value


def set_interval(topic: dict[str, Any], minutes: int | None) -> bool:
    """A changed policy starts a new cadence; unrelated edits keep its deadline."""
    if interval_of(topic) == minutes:
        return False
    interval_of({"check_interval_min": minutes})
    topic["check_interval_min"] = minutes
    topic["check_timer_revision"] = uuid.uuid4().hex
    topic["check_timer_set_at_ts"] = time.time()
    return True


def policy(topic: Mapping[str, Any]) -> dict[str, Any] | None:
    minutes = interval_of(topic)
    if minutes is None:
        return None
    at = epoch(topic.get("check_timer_set_at_ts")) or 0.0
    revision = str(topic.get("check_timer_revision") or f"legacy:{minutes}:{at}")
    return {"minutes": minutes, "revision": revision, "set_at": at}


def all_policies(state: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    result = {}
    for topic in state.get("topics") or []:
        item = policy(topic)
        if item is not None and topic.get("id"):
            result[str(topic["id"])] = item
    return result


def active_policies(state: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    active_ids = {
        str(topic.get("id"))
        for topic in state.get("topics") or []
        if not topic.get("paused") and not topic.get("once_done")
    }
    return {tid: item for tid, item in all_policies(state).items() if tid in active_ids}


def reserved_at(item: Mapping[str, Any], reservation: Any) -> float:
    if not isinstance(reservation, dict) or reservation.get("revision") != item["revision"]:
        return 0.0
    if type(reservation.get("minutes")) is not int or reservation["minutes"] != item["minutes"]:
        return 0.0
    return epoch(reservation.get("started_at")) or 0.0


def batch_ids(state: Mapping[str, Any], schedule: Mapping[str, Any]) -> list[str]:
    """Re-read policy after the check lock: old batches cannot run edited/deleted topics."""
    active = active_policies(state)
    batch = schedule.get("timer_batch")
    if not isinstance(batch, dict):
        return []
    return [tid for tid, item in active.items() if reserved_at(item, batch.get(tid))]
