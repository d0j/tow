"""Routes: checks started by hand - one topic, a revision that blocks it, or every topic in the background."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Form
from fastapi.responses import JSONResponse, Response

from tow import i18n
from tow.check import blocked_by_previous_revision
from tow.log import error_fields, owner_language
from tow.records import CheckRow, shown_title
from tow.store import CheckBusyError, SecretStoreError
from tow.trackers import load_trackers, match_tracker
from tow.web import check_jobs, services
from tow.web.text import t
from tow.web.views import (
    check_feedback,
    check_job_message,
    check_progress,
    check_report,
    feedback_redirect,
    flash_location,
    flash_redirect,
    topic_check_row,
)

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
        return _enqueue(topic_id=tid, apply=True, adopted=adopted)
    try:
        out = services.run_check(apply=True, notify=True, ids=[tid], ignore_cool=True, how="manual", wait=False)
    except CheckBusyError:
        return _enqueue(topic_id=tid, apply=True, adopted=adopted)
    except SecretStoreError as exc:
        services.record_check_failure(exc, how="manual")
        return flash_redirect("/", "web.check.blocked", "err")
    return feedback_redirect(_topic_feedback(tid, topic_check_row(tid, out), adopted=adopted))


def _topic_feedback(tid: str, row: CheckRow | None, *, adopted: bool) -> dict[str, str]:
    """``check_feedback`` with what a failed row needs: the topic's title, and its site when the
    site signs in (its sign-in prompt)."""
    tracker_name = title = ""
    if row is not None and not row.get("ok") and not row.get("note"):
        state = services.load_state()
        topic = next((item for item in state.get("topics") or [] if str(item.get("id")) == str(tid)), None)
        tracker = match_tracker(load_trackers(services.load_config()), str((topic or {}).get("url") or ""))
        tracker_name = tracker.name if tracker and tracker.spec.get("login_path") else ""
        title = shown_title(topic or {})
    return check_feedback(row, topic_id=tid, adopted=adopted, tracker_name=tracker_name, topic_title=title)


def _perform_check(job: dict[str, Any]) -> None:
    # A plain thread does not inherit the request's language: the job carries it.
    lang = str(job.get("lang") or owner_language())
    i18n.use(lang)
    topic_id = str(job.get("topic_id") or "")
    feedback: dict[str, str] = {}
    try:
        out = services.run_check(
            apply=job["apply"],
            notify=True,
            ignore_cool=True,
            how="manual",
            wait=False,
            ids=[topic_id] if topic_id else None,
        )
        report = check_report(out, apply=job["apply"])
        if topic_id:
            # A topic's check says what a check of it at once says; deleted meanwhile, it was
            # never checked ("not found"), and there is no result of it to show.
            row = topic_check_row(topic_id, out)
            feedback = _topic_feedback(topic_id, row, adopted=bool(job.get("adopted")))
            flash, status, kind = feedback["text"], "done" if row is not None else "failed", feedback["kind"]
        else:
            waiting_or_failed = report["counts"]["fail"] or report["counts"]["pending"]
            flash, status, kind = report["summary"], "done", "warn" if waiting_or_failed else "ok"
        if status == "done":
            with check_jobs.LOCK:
                job["report"] = report
    except CheckBusyError:
        raise  # the FIFO retries the head without duplicating work or consuming a failure
    except SecretStoreError as exc:
        services.record_check_failure(exc, how="manual")
        flash, status, kind = i18n.t("web.check.blocked", lang), "failed", "err"
    except Exception as exc:  # noqa: BLE001 - the background job's boundary: the page polls for its result
        services.log_event("check_blocked", how="manual", reason="result_unconfirmed", **error_fields(exc))
        flash, status, kind = i18n.t("web.check_all.not_run", lang, error=type(exc).__name__), "failed", "err"
    # The page follows the job and then opens Home with its result (a message kept on the server).
    credential = feedback.get("credential_topic")
    redirect = flash_location(
        "/?credential_topic=" + quote(credential) if credential else "/",
        flash,
        kind,
        problem_topic=feedback.get("problem_topic", ""),
        result_job=job["id"],
    )
    with check_jobs.LOCK:
        job.update({"status": status, "flash": flash, "kind": kind, "feedback": feedback, "redirect": redirect})


@router.post("/check")
def check(mode: str = Form("dry")) -> Response:
    return _enqueue(topic_id="", apply=mode == "apply")


def _enqueue(*, topic_id: str, apply: bool, adopted: bool = False) -> Response:
    job, added = check_jobs.submit(
        topic_id=topic_id, apply=apply, lang=owner_language(), perform=_perform_check, adopted=adopted
    )
    if job is None:
        return flash_redirect("/", "web.check_all.queue_full", "warn")
    # A topic's check comes here only when it has to wait; one of every topic may start at once.
    starts_now = added and not topic_id and job["position"] == 1
    return flash_redirect(
        "/?check_job=" + quote(job["id"]), "web.check_all.started" if starts_now else "web.check_all.queued"
    )


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
    if current.get("redirect"):
        body["redirect"] = current["redirect"]
    elif current["status"] == "failed":
        body["redirect"] = flash_location("/", check_job_message(current), "warn", result_job=job)
    return JSONResponse(body)


@router.post("/check/{job}/cancel")
def cancel_check(job: str) -> Response:
    if check_jobs.cancel(job):
        return flash_redirect("/", "web.check_all.cancelled")
    return flash_redirect("/", "web.check_all.cannot_cancel", "warn")
