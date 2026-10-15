"""One mutually exclusive outcome for web results and diagnostic run totals."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


def outcome_of(row: Mapping[str, Any]) -> str:
    if row.get("note"):
        return "skipped"
    if row.get("status") in {"verification_pending", "waiting_space"}:
        return "pending"
    if not row.get("ok"):
        return "fail"
    if row.get("status") == "skipped" or row.get("skipped"):
        return "skipped"
    if any(row.get(key) for key in ("changed", "added", "started", "selection_updated")):
        return "new"
    return "same"


def counts_of(rows: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    counts = dict.fromkeys(("new", "same", "pending", "skipped", "fail"), 0)
    for row in rows:
        counts[outcome_of(row)] += 1
    return counts
