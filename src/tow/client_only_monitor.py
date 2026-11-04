"""Durable visibility of a client handoff journal that cannot currently be read.

This observes the monitor, never a native add's outcome. An unreadable receipt is not
permission to replay it, repair it or declare its torrent failed.
"""

from __future__ import annotations

import copy
import re
import time
import uuid
from collections.abc import Mapping
from typing import Any

from tow import messages_feed
from tow.errors import TowError
from tow.records import health_of
from tow.store import load_state, persistence_lock, save_state


def _error_code(error: BaseException) -> str:
    # Exception strings and arbitrary params may contain paths or client credentials.
    return (
        "client_only.store_invalid"
        if isinstance(error, TowError) and error.code == "client_only.store_invalid"
        else "client_only.monitor_unavailable"
    )


def _flush(marker: dict[str, Any]) -> None:
    pending = marker.get("pending")
    # This optional diagnostic is not the authoritative encrypted receipt journal. An
    # invalid shape must not poison the producer or keep a recovered monitor red forever.
    pending = marker["pending"] = [
        {
            "operation_id": row["operation_id"],
            "ok": row["ok"],
            "error_code": (
                "client_only.store_invalid"
                if row.get("error_code") == "client_only.store_invalid"
                else "client_only.monitor_unavailable"
            ),
        }
        for row in (pending[:2] if isinstance(pending, list) else [])
        if isinstance(row, Mapping)
        and isinstance(row.get("operation_id"), str)
        and re.fullmatch(r"[0-9a-f]{32}", row["operation_id"])
        and type(row.get("ok")) is bool
    ]
    while pending and messages_feed.reconcile_client_only_monitor(pending[0]):
        pending.pop(0)


def observe(error: BaseException | None) -> bool:
    """Record a failed/recovered monitor and retry its unpublished shared messages.

    Healthy without a previous failure is an exact no-op. Persistence errors propagate to
    the producer's logger; no caller may turn them into a native success or replay an add.
    """
    with persistence_lock():
        state = load_state(quarantine=False)
        health = health_of(state)
        raw = health.get("client_only_monitor")
        if error is None and not isinstance(raw, Mapping):
            return False
        previous = copy.deepcopy(dict(raw)) if isinstance(raw, Mapping) else {}
        marker = copy.deepcopy(previous)
        _flush(marker)
        now = time.time()
        if error is not None:
            code = _error_code(error)
            if marker.get("ok") is not False:
                pending = marker.get("pending")
                # A recovery not durably published is still the same reporting outage.
                # Keep its first error pending, with at most one error and one recovery.
                if isinstance(pending, list) and any(row.get("ok") is True for row in pending):
                    marker["pending"] = [row for row in pending if row.get("ok") is False]
                else:
                    operation_id = uuid.uuid4().hex
                    marker = {
                        "operation_id": operation_id,
                        "pending": [{"operation_id": operation_id, "ok": False, "at": now, "error_code": code}],
                    }
                marker.update(ok=False, at=now, error={"code": code, "params": {}})
            elif not isinstance(marker.get("error"), Mapping) or marker["error"].get("code") != code:
                marker.update(at=now, error={"code": code, "params": {}})
        elif marker.get("ok") is not True:
            marker.update(ok=True, at=now)
            marker.setdefault("pending", []).append({"operation_id": uuid.uuid4().hex, "ok": True, "at": now})

        changed = marker != previous
        if changed:
            health["client_only_monitor"] = marker
            state["health"] = health
            save_state(state)
        before = copy.deepcopy(marker)
        _flush(marker)
        if marker != before:
            health["client_only_monitor"] = marker
            state["health"] = health
            save_state(state)
            changed = True
        return changed
