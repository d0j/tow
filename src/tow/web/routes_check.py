"""Routes: checks started by hand - one topic, a revision that blocks it, or every topic in the background."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Form
from fastapi.responses import JSONResponse, Response

from tow import i18n
from tow.check import blocked_by_previous_revision
from tow.log import error_fields, owner_language
from tow.records import shown_title
from tow.store import CheckBusyError, SecretStoreError
from tow.trackers import load_trackers, match_tracker
from tow.web import check_jobs, services
from tow.web.text import t
from tow.web.views import (
    check_job_message,
    check_progress,
    check_report,
    flash_location,
    flash_redirect,
    manual_check_flash,
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
    row = topic_check_row(tid, out)
    if row is None:
        return flash_redirect("/", "web.topics.not_found", "err")
    if row.get("note"):  # its link or client changed meanwhile: nothing of this check was saved
        return flash_redirect("/", "web.check.no_changes_reason", "warn", reason=row["note"])
    if row and not row.get("ok"):
        state = services.load_state()
        topic = next((item for item in state.get("topics") or [] if str(item.get("id")) == str(tid)), None)
        tracker = match_tracker(load_trackers(services.load_config()), str((topic or {}).get("url") or ""))
        tracker_name = tracker.name if tracker and tracker.spec.get("login_path") else ""
        redirect = manual_check_flash(
            row, topic_id=tid, tracker_name=tracker_name, topic_title=shown_title(topic or {})
        )
        if redirect is not None:
            return redirect
    if row and row.get("added"):
        return flash_redirect("/", "web.check.added")
    if row and row.get("started"):  # a torrent that waited for disk space, started now
        return flash_redirect("/", "web.check.started")
    if row and row.get("selection_updated"):
        return flash_redirect("/", "web.check.selection_updated")
    if row and row.get("changed"):
        return flash_redirect("/", "web.check.changed")
    if row and row.get("ok") and adopted:
        return flash_redirect("/", "web.check.adopted")
    if row and row.get("ok") and row.get("skipped"):
        return flash_redirect("/", "web.check.no_changes_reason", "ok", reason=row["skipped"])
    if row and row.get("ok"):
        return flash_redirect("/", "web.common.no_changes")
    return flash_redirect("/", "web.check.failed", "err")


def _perform_check(job: dict[str, Any]) -> None:
    # A plain thread does not inherit the request's language: the job carries it.
    lang = str(job.get("lang") or owner_language())
    i18n.use(lang)
    topic_id = str(job.get("topic_id") or "")
    try:
        out = services.run_check(
            apply=job["apply"],
            notify=True,
            ignore_cool=True,
            how="manual",
            wait=False,
            ids=[topic_id] if topic_id else None,
        )
        if topic_id and not topic_check_row(topic_id, out):
            out = {"results": [{"id": topic_id, "ok": True, "status": "skipped", "skipped": t("web.topics.not_found")}]}
        report = check_report(out, apply=job["apply"])
        flash, status, kind = (
            report["summary"],
            "done",
            "warn" if report["counts"]["fail"] or report["counts"]["pending"] else "ok",
        )
        with check_jobs.LOCK:
            job["report"] = report
        if topic_id and report["rows"] and report["rows"][0]["title"]:
            flash = t(
                "web.check.topic_result",
                title=services.short_series_title(report["rows"][0]["title"], lang),
                result=flash,
            )
    except CheckBusyError:
        raise  # the FIFO retries the head without duplicating work or consuming a failure
    except SecretStoreError as exc:
        services.record_check_failure(exc, how="manual")
        flash, status, kind = i18n.t("web.check.blocked", lang), "failed", "err"
    except Exception as exc:  # noqa: BLE001 - the background job's boundary: the page polls for its result
        services.log_event("check_blocked", how="manual", reason="result_unconfirmed", **error_fields(exc))
        flash, status, kind = i18n.t("web.check_all.not_run", lang, error=type(exc).__name__), "failed", "err"
    # The page follows the job and then opens Home with its result (a message kept on the server).
    redirect = flash_location("/", flash, kind, result_job=job["id"])
    with check_jobs.LOCK:
        job.update({"status": status, "flash": flash, "redirect": redirect})


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
