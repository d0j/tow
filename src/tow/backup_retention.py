"""Retention selection, independent of files or deletion."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

MIB = 1024 * 1024
# Earlier copies kept whatever their age: after a gap longer than the retention (the
# machine was off, copies failed) the new copy alone would otherwise be all that is left.
MIN_OLDER_COPIES = 3


def copy_time(value: str) -> datetime:
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError("copy date must include its timezone")
    return stamp


def retention_settings(cfg: dict[str, Any]) -> dict[str, Any]:
    """Read legacy count policies; new installs keep seven days of night copies."""
    return {
        "mode": "count" if cfg.get("backup_keep") is not None and cfg.get("backup_days") is None else "days",
        "keep": int(cfg.get("backup_keep") or 14),
        "days": int(cfg.get("backup_days") or 7),
        "max_mib": int(cfg.get("backup_max_mib") or 0),
    }


def retained_copies(
    entries: list[tuple[str, datetime, int]],
    *,
    newest: str,
    now: datetime,
    policy: dict[str, Any],
) -> tuple[set[str], bool]:
    """Select only; a verified newest copy and future-dated copies are never removed.

    Age is measured from the verified new copy in UTC, not from a possibly changed
    filesystem date; the ``MIN_OLDER_COPIES`` newest earlier copies stay whatever their age.
    An explicit legacy count override remains compatible.
    A byte budget may reduce historical coverage, but never discards the newest copy.
    """
    ordered = sorted(entries, key=lambda row: (row[1], row[0]), reverse=True)
    future = {name for name, when, _size in ordered if when > now} if policy["mode"] != "count" else set()
    kept = {newest, *future}
    older = [row for row in ordered if row[0] not in future and row[0] != newest]
    if policy["mode"] == "count":
        kept.update(row[0] for row in older[: max(0, policy["keep"] - 1)])
    else:
        cutoff = now - timedelta(days=policy["days"])
        kept.update(name for name, when, _size in older if when > cutoff)
        kept.update(row[0] for row in older[:MIN_OLDER_COPIES])
    budget = policy["max_mib"] * MIB
    total = sum(size for name, _when, size in ordered if name in kept)
    if budget:
        for name, _when, size in reversed(ordered):
            if total <= budget:
                break
            if name in kept and name != newest and name not in future:
                kept.remove(name)
                total -= size
    return kept, bool(budget and total > budget)
