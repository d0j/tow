"""Bounded FIFO for manual checks in this web process, never overlapping client writes.

Like flash messages, these jobs live for this server's lifetime. A restart never
replays a manual re-add; a missing job is explicitly reported as unconfirmed.
"""

from __future__ import annotations

import copy
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from typing import Any

from tow.store import CheckBusyError

LOCK = threading.RLock()
JOBS: dict[str, dict[str, Any]] = {}
PENDING: deque[str] = deque()
_worker: threading.Thread | None = None
MAX_PENDING = 32
RESULT_TTL = 3600


def submit(
    *, topic_id: str, apply: bool, lang: str, perform: Callable[[dict[str, Any]], None], adopted: bool = False
) -> tuple[dict[str, Any] | None, bool]:
    """Deduplicate pending requests, enqueue once, and keep exactly one FIFO consumer."""
    global _worker
    with LOCK:
        now = time.monotonic()
        for key, job in list(JOBS.items()):
            if job.get("finished_at", now) + RESULT_TTL < now:
                del JOBS[key]
        for job in JOBS.values():
            if job["status"] in {"queued", "running"} and job["topic_id"] == topic_id and job["apply"] == apply:
                return copy.deepcopy(job), False
        if len(PENDING) >= MAX_PENDING:
            return None, False
        # Bound completed records independently of the pending-work limit.
        while len(JOBS) >= 64:
            finished = next(
                (key for key, value in JOBS.items() if key not in PENDING and value["status"] in {"done", "failed"}),
                None,
            )
            if finished is None:
                return None, False
            del JOBS[finished]
        job = {
            "id": uuid.uuid4().hex[:16],
            "topic_id": topic_id,
            "apply": apply,
            "adopted": adopted,
            "lang": lang,
            "status": "queued",
            "started_at": time.time(),
        }
        JOBS[job["id"]] = job
        PENDING.append(job["id"])
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_consume, args=(perform,), name="tow-check-all", daemon=True)
            try:
                _worker.start()
            except RuntimeError:
                PENDING.remove(job["id"])
                del JOBS[job["id"]]
                raise
        return copy.deepcopy(job), True


def _consume(perform: Callable[[dict[str, Any]], None]) -> None:
    global _worker
    while True:
        with LOCK:
            if not PENDING:
                _worker = None
                return
            job = JOBS[PENDING[0]]
            job["status"] = "running"
        try:
            perform(job)
        except CheckBusyError:
            # Scheduled/CLI checks share the same cross-process lock. Waiting is
            # work, not a tracker failure, and never holds an HTTP request open.
            with LOCK:
                job["status"] = "queued"
            threading.Event().wait(0.25)
            continue
        except Exception:  # noqa: BLE001 - never strand the FIFO if result rendering itself fails
            with LOCK:
                job["status"] = "failed"
        with LOCK:
            if job["status"] not in {"done", "failed"}:
                job["status"] = "failed"
            job["finished_at"] = time.monotonic()
            PENDING.popleft()


def get(job_id: str) -> dict[str, Any] | None:
    with LOCK:
        job = JOBS.get(job_id)
        if job is None or job.get("finished_at", time.monotonic()) + RESULT_TTL < time.monotonic():
            return None
        result = copy.deepcopy(job)
        result["position"] = list(PENDING).index(job_id) + 1 if job_id in PENDING else 0
        return result


def pending_topic(topic_id: str) -> dict[str, Any] | None:
    with LOCK:
        return next((get(key) for key in PENDING if JOBS[key]["topic_id"] == topic_id), None)


def has_pending() -> bool:
    with LOCK:
        return bool(PENDING)


def cancel(job_id: str) -> bool:
    """Cancel only work that has not started; never pretend to abort a client operation."""
    with LOCK:
        job = JOBS.get(job_id)
        if job is None or job["status"] != "queued":
            return False
        PENDING.remove(job_id)
        job.update(status="failed", finished_at=time.monotonic(), cancelled=True)
        return True
