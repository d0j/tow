"""Bounded FIFO lanes for manual checks in this web process, never overlapping client writes.

Applying checks and previews wait in lanes of their own, each with one consumer: a preview takes
no check lock and never delays an applying check, and two applying checks never overlap. Like
flash messages, these jobs live for this server's lifetime. A restart never replays a manual
re-add; a missing job is explicitly reported as unconfirmed.
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
PENDING: deque[str] = deque()  # the jobs of both lanes that wait or run, oldest first
# A page's action id -> its job (D10): registered with the job, before any work, so a page that
# lost the answer finds its check by the id it kept. Forgotten with the job.
ACTIONS: dict[str, str] = {}
# What a job's caller does with its check's result, by job id: a sign-in confirms or restores the
# password, a new topic's first check says "added to TOW, ...". Kept beside the job, never in it:
# a job is deep-copied for every page that asks about it, and a follow-up may hold a password.
# ``follow_up(result, error)`` gets the check's result or the error that ended it and returns the
# job's feedback (``views.check_feedback``'s shape), or None to keep the check's own words.
type FollowUp = Callable[[dict[str, Any] | None, BaseException | None], dict[str, str] | None]
FOLLOW_UPS: dict[str, FollowUp] = {}
# The consumer of each lane, by the lane's ``apply``.
_WORKERS: dict[bool, threading.Thread] = {}
MAX_PENDING = 32  # per lane
RESULT_TTL = 3600
# How long a manual check waits for the check lock: a scheduled check's 3600-second maximum and
# five minutes for its cleanup. Then it ends as not run; it is tried once a second until then.
LOCK_WAIT_SEC = 3900
LOCK_RETRY_SEC = 1.0


def _lane(apply: bool) -> list[str]:
    """The jobs of one lane that wait or run, oldest first (the caller holds LOCK)."""
    return [key for key in PENDING if JOBS[key]["apply"] == apply]


def submit(
    *,
    topic_id: str,
    apply: bool,
    lang: str,
    perform: Callable[[dict[str, Any]], None],
    adopted: bool = False,
    subject: str = "",
    action: str = "",
    follow_up: FollowUp | None = None,
) -> tuple[dict[str, Any] | None, bool]:
    """Share a request that still waits, enqueue anything else once in its lane, and keep exactly
    one FIFO consumer per lane. The returned job carries its ``position`` in its lane (1: next or
    running); ``subject`` is what its message names (the topic, as it was when asked for);
    ``action``: the page's id of the click (one already registered is the same check again);
    ``follow_up``: what the caller does with the result (``FOLLOW_UPS``; the latest click's wins)."""
    with LOCK:
        now = time.monotonic()
        for key, job in list(JOBS.items()):
            if job.get("finished_at", now) + RESULT_TTL < now:
                del JOBS[key]
                FOLLOW_UPS.pop(key, None)
        for known in [key for key, job_id in ACTIONS.items() if job_id not in JOBS]:
            del ACTIONS[known]
        if action and action in ACTIONS:
            return get(ACTIONS[action]), False
        for job in JOBS.values():
            # Only work that has not started is shared: a running check may have read the topic
            # before the change the owner clicks for, so one follow-up waits behind it.
            if job["status"] == "queued" and job["topic_id"] == topic_id and job["apply"] == apply:
                job["adopted"] = bool(job.get("adopted")) or adopted
                # The latest click is the owner's request: what it may re-add (``run_check``).
                job["requested_at"] = time.time()
                if action:
                    ACTIONS[action] = job["id"]
                if follow_up is not None:
                    FOLLOW_UPS[job["id"]] = follow_up
                return get(job["id"]), False
        if len(_lane(apply)) >= MAX_PENDING:
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
            FOLLOW_UPS.pop(finished, None)
        job = {
            "id": uuid.uuid4().hex[:16],
            "topic_id": topic_id,
            "apply": apply,
            "adopted": adopted,
            "subject": subject,
            "lang": lang,
            "status": "queued",
            "requested_at": time.time(),
        }
        JOBS[job["id"]] = job
        PENDING.append(job["id"])
        if action:
            ACTIONS[action] = job["id"]
        if follow_up is not None:
            FOLLOW_UPS[job["id"]] = follow_up
        worker = _WORKERS.get(apply)
        if worker is None or not worker.is_alive():
            try:
                _WORKERS[apply] = _start("tow-check-apply" if apply else "tow-check-preview", _consume, apply, perform)
            except RuntimeError:
                PENDING.remove(job["id"])
                del JOBS[job["id"]]
                ACTIONS.pop(action, None)
                FOLLOW_UPS.pop(job["id"], None)
                raise
        return get(job["id"]), True


def _start(name: str, target: Callable[..., None], *args: Any) -> threading.Thread:
    """Start a consumer thread: the seam tests replace to run the consumer in their own thread."""
    worker = threading.Thread(target=target, args=args, name=name, daemon=True)
    worker.start()
    return worker


def _consume(apply: bool, perform: Callable[[dict[str, Any]], None]) -> None:
    while True:
        with LOCK:
            lane = _lane(apply)
            if not lane:
                _WORKERS.pop(apply, None)
                return
            job = JOBS[lane[0]]
            # Nothing stops a check that hangs (a second one would overlap its client writes):
            # the page says since when it runs instead, and the lane waits for it.
            job.update(status="running", running_since=time.time())
        try:
            perform(job)
        except CheckBusyError:
            # Scheduled/CLI checks share the same cross-process lock. Waiting is
            # work, not a tracker failure, and never holds an HTTP request open.
            if _waited_too_long(job):
                _finish(job)
            else:
                _pause(LOCK_RETRY_SEC)
            continue
        except Exception:  # noqa: BLE001 - never strand the FIFO if result rendering itself fails
            with LOCK:
                job["status"] = "failed"
        except BaseException:
            # The thread ends (SystemExit, KeyboardInterrupt): the head never stays "running",
            # and the next consumer never runs it - a manual re-add - again.
            _finish(job)
            raise
        _finish(job)


def _waited_too_long(job: dict[str, Any]) -> bool:
    """The check lock refused ``job`` again: it waits on (True only after LOCK_WAIT_SEC by the
    monotonic clock; then it ends as not run, which stamps no site or topic failure). When it
    started waiting is kept as a wall-clock time too, for the page alone."""
    with LOCK:
        now = time.monotonic()
        started = job.setdefault("wait_started", now)
        job.setdefault("waiting_since", time.time())
        if now - started >= LOCK_WAIT_SEC:
            job.update(status="failed", lock_timeout=True)
            return True
        job["status"] = "queued"
        return False


def _pause(seconds: float) -> None:
    """The wait between two attempts to take the check lock (the seam tests replace)."""
    threading.Event().wait(seconds)


def take_follow_up(job_id: str) -> FollowUp | None:
    """The job's follow-up, once (its check has run and ended): None when it has none."""
    with LOCK:
        return FOLLOW_UPS.pop(job_id, None)


def _finish(job: dict[str, Any]) -> None:
    with LOCK:
        FOLLOW_UPS.pop(job["id"], None)  # a job that ended without its check keeps no password
        if job["status"] not in {"done", "failed"}:
            job["status"] = "failed"
        # finished_at times the record's hour; the result page is dated by ended_at.
        job.update(finished_at=time.monotonic(), ended_at=time.time())
        PENDING.remove(job["id"])


def get(job_id: str) -> dict[str, Any] | None:
    with LOCK:
        job = JOBS.get(job_id)
        if job is None or job.get("finished_at", time.monotonic()) + RESULT_TTL < time.monotonic():
            return None
        result = copy.deepcopy(job)
        lane = _lane(job["apply"]) if job_id in PENDING else []
        result["position"] = lane.index(job_id) + 1 if lane else 0
        return result


def by_action(action: str) -> dict[str, Any] | None:
    """The job a page's action id was registered for (``submit``); None for none or an expired one."""
    with LOCK:
        job_id = ACTIONS.get(action) if action else None
        return get(job_id) if job_id else None


def pending_topic(topic_id: str) -> dict[str, Any] | None:
    with LOCK:
        return next((get(key) for key in PENDING if JOBS[key]["topic_id"] == topic_id), None)


def has_pending(*, apply: bool) -> bool:
    """Work waits or runs in the lane of applying checks (``apply``) or of previews."""
    with LOCK:
        return bool(_lane(apply))


def cancel(job_id: str) -> bool:
    """Cancel only work that has not started; never pretend to abort a client operation."""
    with LOCK:
        job = JOBS.get(job_id)
        if job is None or job["status"] != "queued":
            return False
        PENDING.remove(job_id)
        FOLLOW_UPS.pop(job_id, None)
        job.update(status="failed", finished_at=time.monotonic(), ended_at=time.time(), cancelled=True)
        return True
