"""Manual checks the web hands to the queue (``tow.web.check_jobs``) - one topic's while another
check applies, every topic's, a new topic's first, the check after a sign-in - and what one of a
topic says, the same as a check at once (``views.check_feedback``)."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from fastapi.responses import JSONResponse, RedirectResponse, Response

from tow import i18n
from tow.log import error_fields, owner_language
from tow.records import CheckRow
from tow.store import CheckBusyError, SecretStoreError
from tow.trackers import load_trackers, match_tracker
from tow.web import _context, check_jobs, services
from tow.web.messages import flash_redirect
from tow.web.views import check_feedback, check_report, check_status_message, topic_check_row


def topic_feedback(tid: str, row: CheckRow | None, *, adopted: bool) -> dict[str, str]:
    """``check_feedback`` with what a failed row needs: the topic's title, and its site when the
    site signs in (its sign-in prompt)."""
    tracker_name = ""
    if row is not None and not row.get("ok") and not row.get("note"):
        state = services.load_state()
        topic = next((item for item in state.get("topics") or [] if str(item.get("id")) == str(tid)), None)
        tracker = match_tracker(load_trackers(services.load_config()), str((topic or {}).get("url") or ""))
        tracker_name = tracker.name if tracker and tracker.spec.get("login_path") else ""
    return check_feedback(row, topic_id=tid, adopted=adopted, tracker_name=tracker_name)


def enqueue_check(
    *, topic_id: str, apply: bool, adopted: bool = False, follow_up: check_jobs.FollowUp | None = None
) -> RedirectResponse:
    """Queue a manual check (``topic_id`` "": every topic) and open the page that follows it.
    The job keeps what its message is about as the request named it (a topic deleted while the
    check waits is still named); ``follow_up``: what the caller would have done with the result
    of a check at once (``check_jobs.FOLLOW_UPS``)."""
    job, added = check_jobs.submit(
        topic_id=topic_id,
        apply=apply,
        lang=owner_language(),
        perform=perform_check,
        adopted=adopted,
        subject=_context.subject() if topic_id else "",
        follow_up=follow_up,
    )
    if job is None:
        return flash_redirect("/", "web.check_all.queue_full", "warn")
    # A topic's check comes here only when it has to wait; one of every topic may start at once.
    starts_now = added and not topic_id and job["position"] == 1
    return flash_redirect(
        "/?check_job=" + quote(job["id"]),
        "web.check_all.started" if starts_now else "web.check_all.queued",
        "info",
    )


def action_of(value: str) -> str:
    """A page's action id as the script makes it, else "" (then the request is a native one)."""
    return value if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value or "") else ""


def accept_check(*, topic_id: str, apply: bool, action: str, adopted: bool = False) -> Response:
    """D10: a Check the page's script sent with its action id. The check is registered under that
    id before any work - the queue's worker starts it at once when nothing else runs - and the
    answer is a short acknowledgement, not a page: the page follows the check by its id
    (``/check/status``) and shows its result where it is. The same id again is the same check.
    A full queue answers as before (a redirect with its message)."""
    job, _added = check_jobs.submit(
        topic_id=topic_id,
        apply=apply,
        lang=owner_language(),
        perform=perform_check,
        adopted=adopted,
        subject=_context.subject() if topic_id else "",
        action=action,
    )
    if job is None:
        return flash_redirect("/", "web.check_all.queue_full", "warn")
    # The head of its lane runs or starts next - also when this is the same click again (the
    # action id already registered) or a click that joined a waiting check: "started", not "queued".
    starts_now = job["position"] == 1 and job["status"] in {"queued", "running"}
    message = check_status_message(job)
    if job["status"] in {"queued", "running"}:
        message["text"] = i18n.t("web.check_all.started" if starts_now else "web.check_all.queued")
        message.pop("named", None)  # catalog words now: a sentence when said on their own
    body = {
        "accepted": True,
        "action": action,
        "job": job["id"],
        "status": job["status"],
        "topic_id": topic_id,
        "message": message,
    }
    return JSONResponse(body, headers={"Cache-Control": "no-store"})


def perform_check(job: dict[str, Any]) -> None:
    """Run one queued check (in the queue's thread) and keep what it says with the job."""
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
            # A torrent removed after the click is not added back by a check that waited for it.
            readd_since=job.get("requested_at") if topic_id else None,
        )
    except CheckBusyError:
        raise  # the FIFO retries the head without duplicating work or consuming a failure
    except SecretStoreError as exc:
        if job["apply"]:  # a preview writes nothing, its failures included (AGENTS.md)
            services.record_check_failure(exc, how="manual", whole=not topic_id)
        flash, status, kind = i18n.t("web.check.blocked", lang), "failed", "err"
        if followed := _followed(job, None, exc):
            flash, kind, feedback = followed["text"], followed["kind"], followed
    except Exception as exc:  # noqa: BLE001 - the background job's boundary: the page polls for its result
        if job["apply"]:
            services.log_event("check_blocked", how="manual", reason="result_unconfirmed", **error_fields(exc))
        flash, status, kind = i18n.t("web.check_all.not_run", lang, error=type(exc).__name__), "failed", "err"
        if followed := _followed(job, None, exc):
            flash, kind, feedback = followed["text"], followed["kind"], followed
    else:
        try:
            flash, status, kind, feedback = _job_result(job, out)
        except Exception as exc:  # noqa: BLE001 - the check ran and saved what it found; only its words failed
            if job["apply"]:
                services.log_event("check_report_failed", how="manual", **error_fields(exc))
            flash, status, kind = i18n.t("web.check_all.report_failed", lang, error=type(exc).__name__), "done", "warn"
        if followed := _followed(job, out, None):
            flash, kind, feedback = followed["text"], followed["kind"], followed
    # The page follows the job, then opens its result (views.result_location), shown from the job.
    with check_jobs.LOCK:
        job.update({"status": status, "flash": flash, "kind": kind, "feedback": feedback})


def _followed(job: dict[str, Any], out: dict[str, Any] | None, error: BaseException | None) -> dict[str, str] | None:
    """What the job's caller does with its check's result (``check_jobs.FOLLOW_UPS``), once:
    its words for the job, or None. A follow-up that fails leaves the check's own words."""
    follow_up = check_jobs.take_follow_up(str(job.get("id") or ""))
    if follow_up is None:
        return None
    try:
        return follow_up(out, error)
    except Exception as exc:  # noqa: BLE001 - the check ran; only what follows it failed (logged)
        services.log_event("check_report_failed", how="manual", **error_fields(exc))
        return None


def _job_result(job: dict[str, Any], out: dict[str, Any]) -> tuple[str, str, str, dict[str, str]]:
    """(message, status, kind, a topic's feedback) of a check that ran; its report goes with the job."""
    topic_id = str(job.get("topic_id") or "")
    report = check_report(out, apply=job["apply"])
    feedback: dict[str, str] = {}
    if topic_id:
        # A topic's check says what a check of it at once says; deleted meanwhile, it was never
        # checked ("not found"), and there is no result of it to show.
        row = topic_check_row(topic_id, out)
        feedback = topic_feedback(topic_id, row, adopted=bool(job.get("adopted")))
        flash, status, kind = feedback["text"], "done" if row is not None else "failed", feedback["kind"]
    else:
        # The worst result's colour: a red failure is not made amber by the rows that passed.
        flash, status, kind = report["summary"], "done", report["kind"]
    if status == "done":
        with check_jobs.LOCK:
            job["report"] = report
    return flash, status, kind, feedback
