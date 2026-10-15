"""Manual checks the web hands to the queue (``tow.web.check_jobs``) - one topic's while another
check applies, every topic's, a new topic's first, the check after a sign-in - and what one of a
topic says, the same as a check at once (``views.check_feedback``)."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi.responses import RedirectResponse

from tow import i18n
from tow.log import error_fields, owner_language
from tow.records import CheckRow
from tow.store import CheckBusyError, SecretStoreError
from tow.trackers import load_trackers, match_tracker
from tow.web import _context, check_jobs, services
from tow.web.messages import flash_redirect
from tow.web.views import check_feedback, check_report, topic_check_row


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


def enqueue_check(*, topic_id: str, apply: bool, adopted: bool = False) -> RedirectResponse:
    """Queue a manual check (``topic_id`` "": every topic) and open the page that follows it.
    The job keeps what its message is about as the request named it (a topic deleted while the
    check waits is still named)."""
    job, added = check_jobs.submit(
        topic_id=topic_id,
        apply=apply,
        lang=owner_language(),
        perform=perform_check,
        adopted=adopted,
        subject=_context.subject() if topic_id else "",
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
            services.record_check_failure(exc, how="manual")
        flash, status, kind = i18n.t("web.check.blocked", lang), "failed", "err"
    except Exception as exc:  # noqa: BLE001 - the background job's boundary: the page polls for its result
        if job["apply"]:
            services.log_event("check_blocked", how="manual", reason="result_unconfirmed", **error_fields(exc))
        flash, status, kind = i18n.t("web.check_all.not_run", lang, error=type(exc).__name__), "failed", "err"
    else:
        try:
            flash, status, kind, feedback = _job_result(job, out)
        except Exception as exc:  # noqa: BLE001 - the check ran and saved what it found; only its words failed
            if job["apply"]:
                services.log_event("check_report_failed", how="manual", **error_fields(exc))
            flash, status, kind = i18n.t("web.check_all.report_failed", lang, error=type(exc).__name__), "done", "warn"
    # The page follows the job, then opens its result (views.result_location), shown from the job.
    with check_jobs.LOCK:
        job.update({"status": status, "flash": flash, "kind": kind, "feedback": feedback})


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
