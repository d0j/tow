"""Routes: checks started by hand - one topic, a revision that blocks it, or every topic in the background."""

from __future__ import annotations

from fastapi import APIRouter, Form
from fastapi.responses import JSONResponse, Response

from tow.check import blocked_by_previous_revision
from tow.store import CheckBusyError, SecretStoreError
from tow.web import check_jobs, services
from tow.web.check_actions import enqueue_check, topic_feedback
from tow.web.messages import flash_redirect
from tow.web.text import t
from tow.web.views import check_job_message, check_progress, feedback_redirect, result_location, topic_check_row

router = APIRouter()


@router.post("/topics/{tid}/replace-revision")
def topics_replace_revision(tid: str) -> Response:
    """G1: stop the topic's previous revision(s) TOW added, then check it again.

    The automatic check never stops a seeding revision; this is the owner's one click.
    """
    state = services.load_state()
    topic = next((item for item in state.get("topics") or [] if str(item.get("id")) == tid), None)
    if topic is None:
        return flash_redirect("/", "web.topics.not_found", "err")
    if not blocked_by_previous_revision(topic):
        return flash_redirect("/", "web.topics.replace_not_needed", "warn")
    if topic.get("paused"):
        # The check after the stop skips a paused topic: the previous torrent would stop and
        # nothing would replace it.
        return flash_redirect("/", "web.topics.replace_paused", "warn")
    try:
        client = services.client_from_secrets(
            services.load_config(), services.load_secrets(), topic.get("client_id") or None
        )
        stop = getattr(client, "stop_owned_torrent", None)
        if not callable(stop):
            raise RuntimeError(t("web.topics.cannot_stop"))  # noqa: TRY004
        stopped = []
        for revision in dict.fromkeys([str(topic.get("hash") or ""), *map(str, topic.get("previous_hashes") or [])]):
            if revision and client.has_hash(revision):
                stop(revision)
                stopped.append(revision)
    except Exception as exc:  # noqa: BLE001 - any client failure is logged and shown; nothing was stopped by TOW
        services.log_event("client_stop_failed", topic=tid, error=str(exc), how="manual")
        return flash_redirect("/", "web.topics.replace_failed", "err", error=exc)
    services.log_event("client_stopped", topic=tid, hashes=stopped, reason="replace_revision", how="manual")
    return _check_topic(tid)


@router.post("/topics/{tid}/adopt")
def topics_adopt(tid: str) -> Response:
    """Adopt into TOW (tow.adopt): mark the topic's torrent, already in the client without TOW's
    mark, as TOW's - on the owner's confirmed click only - then check the topic again."""
    try:
        services.adopt_topic(tid, how="manual")
    except CheckBusyError:
        return flash_redirect("/", "web.check.busy", "warn")
    except SecretStoreError as exc:
        services.record_check_failure(exc, how="manual")
        return flash_redirect("/", "web.check.blocked", "err")
    except Exception as exc:  # noqa: BLE001 - any client failure is logged (tow.adopt) and shown, never a 500
        return flash_redirect("/", "web.topics.adopt_failed", "err", error=exc)
    return _check_topic(tid, adopted=True)


@router.post("/topics/{tid}/check")
def topics_check(tid: str) -> Response:
    return _check_topic(tid)


def _check_topic(tid: str, *, adopted: bool = False) -> Response:
    """Check one topic now and say what it found; ``adopted``: the owner has just adopted its
    torrent into TOW, which a check that changes nothing else confirms."""
    if check_jobs.has_pending(apply=True):  # never ahead of an applying check that waits
        return enqueue_check(topic_id=tid, apply=True, adopted=adopted)
    try:
        out = services.run_check(apply=True, notify=True, ids=[tid], ignore_cool=True, how="manual", wait=False)
    except CheckBusyError:
        return enqueue_check(topic_id=tid, apply=True, adopted=adopted)
    except SecretStoreError as exc:
        services.record_check_failure(exc, how="manual")
        return flash_redirect("/", "web.check.blocked", "err")
    return feedback_redirect(topic_feedback(tid, topic_check_row(tid, out), adopted=adopted))


@router.post("/check")
def check(mode: str = Form("dry")) -> Response:
    return enqueue_check(topic_id="", apply=mode == "apply")


@router.get("/check/status")
def check_status(job: str = "") -> Response:
    current = check_jobs.get(job)
    if current is None:
        return JSONResponse({"status": "unknown"})
    waiting = current["status"] in {"queued", "running"}
    body = {"status": current["status"], "flash": "" if waiting else check_job_message(current)}
    if waiting:
        body["progress"] = check_progress(current)
    if current["status"] == "queued":
        body["position"] = current["position"]
    if not waiting:  # an address, not a message: polling writes nothing
        body["redirect"] = result_location(current)
    return JSONResponse(body)


@router.post("/check/{job}/cancel")
def cancel_check(job: str) -> Response:
    if check_jobs.cancel(job):
        return flash_redirect("/", "web.check_all.cancelled")
    if check_jobs.get(job) is None:  # never started here, or its record is gone: nothing to say it did
        return flash_redirect("/", "web.check_all.cancel_unknown", "warn")
    return flash_redirect("/", "web.check_all.cannot_cancel", "warn")
