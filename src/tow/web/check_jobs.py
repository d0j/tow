"""Bounded FIFO lanes for manual checks in this web process, never overlapping client writes.

Applying checks and previews wait in lanes of their own, each with one consumer: a preview takes
no check lock and never delays an applying check, and two applying checks never overlap. Like
flash messages, these jobs live for this server's lifetime. A restart never replays a manual
re-add; a missing job is explicitly reported as unconfirmed.
"""

from __future__ import annotations

import contextvars
import copy
import logging
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from typing import Any

from tow import i18n
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
# job's feedback (``views.check_feedback``'s shape), or None to keep the check's own words. A
# follow-up that replaces another one of a waiting job is first handed it (``succeeds(earlier)``,
# when it has that method): a second sign-in keeps the first one's password to put back. A
# follow-up whose check never runs (cancelled, never given the check lock, its job not made) is
# handed that (``dropped()``, when it has that method): a sign-in puts back the password it saved
# unchecked, and its words become the job's.
type FollowUp = Callable[[dict[str, Any] | None, BaseException | None], dict[str, str] | None]
FOLLOW_UPS: dict[str, FollowUp] = {}
# Sign-ins saved but not checked yet (``routes_topic_login._SignIn``), one generation per site:
# ``baseline``, the login to put back - set only by the sign-in that starts the generation (what
# was saved before it) or by a sign-in the site then confirmed (a new generation, with its login);
# ``members``, the sign-ins of it that still wait or run (only they read or change it); ``since``.
# A generation ends with its last member; it outlives no job (UNCHECKED_TTL) and the table holds
# at most MAX_UNCHECKED sites. Changed under LOCK; nothing is written to the stores there.
UNCHECKED: dict[str, dict[str, Any]] = {}
MAX_UNCHECKED = 64
# The consumer of each lane, by the lane's ``apply``.
_WORKERS: dict[bool, threading.Thread] = {}
MAX_PENDING = 32  # per lane
# Page action ids kept: one per click, and the clicks that share a waiting check all point at it
# while it lives (an hour after it ends) - the oldest go first.
MAX_ACTIONS = 256
RESULT_TTL = 3600
# How long a manual check waits for the check lock: a scheduled check's 3600-second maximum and
# five minutes for its cleanup. Then it ends as not run; it is tried once a second until then.
LOCK_WAIT_SEC = 3900
LOCK_RETRY_SEC = 1.0
UNCHECKED_TTL = LOCK_WAIT_SEC + RESULT_TTL  # longer than any job waits and keeps its follow-up
# Counts the changes of the jobs every page may show (tow.web.sync): a job made, started, put
# back to wait for the check lock, cancelled, ended or forgotten - a check of every topic too.
_GENERATION = 0


def _changed() -> None:
    """A job changed in a way pages show (the caller holds LOCK)."""
    global _GENERATION
    _GENERATION += 1


def generation() -> int:
    """The number of job changes pages show so far (``_changed``): equal numbers, same jobs."""
    with LOCK:
        return _GENERATION


def snapshot() -> dict[str, Any]:
    """Every job pages show, as data without the follow-ups: the ones that wait or run (each
    lane's order: ``position``) and the ended ones still kept, with the ``generation`` they belong
    to. A check of every topic has the ``topic_id`` "". Status words: queued, running, done,
    failed; a job that has its result but has not ended yet still says running."""
    with LOCK:
        waiting = []
        for key in PENDING:
            job = JOBS[key]
            lane = _lane(job["apply"])
            waiting.append(
                {
                    "id": key,
                    "topic_id": str(job.get("topic_id") or ""),
                    "apply": bool(job["apply"]),
                    "status": "queued" if job["status"] == "queued" else "running",
                    "position": lane.index(key) + 1,
                }
            )
        ended = [
            {
                "id": key,
                "topic_id": str(job.get("topic_id") or ""),
                "apply": bool(job["apply"]),
                "status": "failed" if job["status"] == "failed" else "done",
                "cancelled": bool(job.get("cancelled")),
            }
            for key, job in JOBS.items()
            if key not in PENDING
        ]
        return {"generation": _GENERATION, "waiting": waiting, "ended": ended}


def join_unchecked(site: str, member: object, saved_before: Any) -> None:
    """``member``, a sign-in just saved for ``site``, joins the site's generation of unchecked
    sign-ins, or starts one with ``saved_before`` as its baseline."""
    with LOCK:
        now = time.monotonic()
        for key in [key for key, generation in UNCHECKED.items() if now - generation["since"] > UNCHECKED_TTL]:
            del UNCHECKED[key]
        generation = UNCHECKED.get(site)
        if generation is None:
            while len(UNCHECKED) >= MAX_UNCHECKED:
                del UNCHECKED[next(iter(UNCHECKED))]  # the oldest site's
            generation = UNCHECKED[site] = {"baseline": copy.deepcopy(saved_before), "members": [], "since": now}
        generation["members"].append(member)


def unchecked_baseline(site: str, member: object, own: Any) -> Any:
    """The login ``member`` puts back: its generation's baseline, or ``own`` (what was saved before
    it) once it is no longer in the site's generation (it expired)."""
    with LOCK:
        generation = UNCHECKED.get(site)
        if generation is not None and any(item is member for item in generation["members"]):
            return copy.deepcopy(generation["baseline"])
        return copy.deepcopy(own)


def leave_unchecked(site: str, member: object, *, confirmed: Any = None) -> None:
    """``member`` was checked (or never will be): it leaves its generation, which ends with its last
    member. ``confirmed``: the login the site took - the remaining members start a new generation
    with it as their baseline. A sign-in no longer in the site's generation changes nothing."""
    with LOCK:
        generation = UNCHECKED.get(site)
        if generation is None or not any(item is member for item in generation["members"]):
            return
        members = [item for item in generation["members"] if item is not member]
        if not members:
            del UNCHECKED[site]
        elif confirmed is not None:
            UNCHECKED[site] = {"baseline": copy.deepcopy(confirmed), "members": members, "since": time.monotonic()}
        else:
            generation["members"] = members


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
    ``follow_up``: what the caller does with the result (``FOLLOW_UPS``; the latest click's wins).
    No job made: a full lane leaves ``follow_up`` to the caller (``drop``, its words said with the
    refusal); no consumer drops it here, before the error goes on."""
    try:
        job, added = _submit(
            topic_id=topic_id,
            apply=apply,
            lang=lang,
            perform=perform,
            adopted=adopted,
            subject=subject,
            action=action,
            follow_up=follow_up,
        )
    except RuntimeError:
        drop(follow_up, lang)
        raise
    return job, added


def _submit(
    *,
    topic_id: str,
    apply: bool,
    lang: str,
    perform: Callable[[dict[str, Any]], None],
    adopted: bool,
    subject: str,
    action: str,
    follow_up: FollowUp | None,
) -> tuple[dict[str, Any] | None, bool]:
    with LOCK:
        now = time.monotonic()
        for key, job in list(JOBS.items()):
            if job.get("finished_at", now) + RESULT_TTL < now:
                del JOBS[key]
                FOLLOW_UPS.pop(key, None)
                _changed()
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
                    _register(action, job["id"])
                if follow_up is not None:
                    earlier = FOLLOW_UPS.get(job["id"])
                    succeeds = getattr(follow_up, "succeeds", None)
                    if earlier is not None and callable(succeeds):
                        succeeds(earlier)
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
            _changed()
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
        _changed()
        if action:
            _register(action, job["id"])
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
                _changed()
                raise
        return get(job["id"]), True


def _register(action: str, job_id: str) -> None:
    """Remember the job of a page's action id (the caller holds LOCK); bounded by MAX_ACTIONS."""
    ACTIONS.pop(action, None)
    ACTIONS[action] = job_id
    while len(ACTIONS) > MAX_ACTIONS:
        del ACTIONS[next(iter(ACTIONS))]


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
            _changed()
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
            job["lock_timeout"] = True  # failed by _finish, once its follow-up has said its words
            return True
        job["status"] = "queued"
        _changed()
        return False


def _pause(seconds: float) -> None:
    """The wait between two attempts to take the check lock (the seam tests replace)."""
    threading.Event().wait(seconds)


def take_follow_up(job_id: str) -> FollowUp | None:
    """The job's follow-up, once (its check has run and ended): None when it has none."""
    with LOCK:
        return FOLLOW_UPS.pop(job_id, None)


def drop(follow_up: FollowUp | None, lang: str) -> dict[str, str] | None:
    """A follow-up whose check never ran, handed that (``dropped()``, when it has that method):
    its words for the job (in the job's language ``lang``, as its check would have said them),
    or None. Called without LOCK held: a sign-in writes the stores."""
    dropped = getattr(follow_up, "dropped", None)
    if not callable(dropped):
        return None

    def in_its_language() -> Any:
        i18n.use(lang)
        return dropped()

    try:
        words = contextvars.copy_context().run(in_its_language)
    except Exception as exc:  # noqa: BLE001 - the job ends all the same; what failed is logged
        logging.getLogger("tow.web").warning("a dropped check's follow-up failed: %s", type(exc).__name__)
        return None
    return words if isinstance(words, dict) and words.get("text") else None


def _said(job: dict[str, Any], words: dict[str, str] | None) -> None:
    """A dropped follow-up's words as the job's own (the caller holds LOCK)."""
    if words is not None:
        job["flash"] = words["text"]
        job.update(kind=words.get("kind") or "warn", feedback=words)


def _finish(job: dict[str, Any]) -> None:
    with LOCK:
        follow_up = FOLLOW_UPS.pop(job["id"], None)  # a job that ended without its check keeps no password
    words = drop(follow_up, str(job.get("lang") or ""))
    with LOCK:
        _said(job, words)
        if job["status"] not in {"done", "failed"}:
            job["status"] = "failed"
        # finished_at times the record's hour; the result page is dated by ended_at.
        job.update(finished_at=time.monotonic(), ended_at=time.time())
        PENDING.remove(job["id"])
        _changed()


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
        follow_up = FOLLOW_UPS.pop(job_id, None)
        job.update(status="failed", finished_at=time.monotonic(), ended_at=time.time(), cancelled=True)
        _changed()
    words = drop(follow_up, str(job.get("lang") or ""))
    with LOCK:
        _said(job, words)
    return True
