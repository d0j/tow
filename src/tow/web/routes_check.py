"""Routes: checks started by hand - one topic, a revision that blocks it, or every topic in the background."""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Form
from fastapi.responses import JSONResponse, Response

from tow import i18n
from tow.check import blocked_by_previous_revision
from tow.log import owner_language
from tow.store import CheckBusyError, SecretStoreError
from tow.trackers import load_trackers, match_tracker
from tow.web import services
from tow.web.text import t
from tow.web.views import flash_location, flash_redirect, manual_check_flash, topic_check_row

router = APIRouter()


@router.post("/topics/{tid}/replace-revision")
def topics_replace_revision(tid: str) -> Response:
    """G1: stop the topic's previous revision(s) TOW added, then check it again.

    The automatic check never stops a seeding revision; this is the owner's one click.
    """
    from tow.clients.factory import from_secrets

    state = services.load_state()
    topic = next((item for item in state.get("topics") or [] if str(item.get("id")) == tid), None)
    if topic is None:
        return flash_redirect("/", "web.topics.not_found", "err")
    if not blocked_by_previous_revision(topic):
        return flash_redirect("/", "web.topics.replace_not_needed", "warn")
    try:
        client = from_secrets(services.load_config(), services.load_secrets(), topic.get("client_id") or None)
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
    return topics_check(tid)


@router.post("/topics/{tid}/check")
def topics_check(tid: str) -> Response:
    try:
        out = services.run_check(apply=True, notify=True, ids=[tid], ignore_cool=True, how="manual", wait=False)
    except CheckBusyError:
        return flash_redirect("/", "web.check.busy", "warn")
    except SecretStoreError as exc:
        services.record_check_failure(exc, how="manual")
        return flash_redirect("/", "web.check.blocked", "err")
    row = topic_check_row(tid, out)
    if row is None:
        return flash_redirect("/", "web.topics.not_found", "err")
    if row and not row.get("ok"):
        state = services.load_state()
        topic = next((item for item in state.get("topics") or [] if str(item.get("id")) == str(tid)), None)
        tracker = match_tracker(load_trackers(services.load_config()), str((topic or {}).get("url") or ""))
        tracker_name = tracker.name if tracker and tracker.spec.get("login_path") else ""
        redirect = manual_check_flash(row, topic_id=tid, tracker_name=tracker_name)
        if redirect is not None:
            return redirect
    if row and row.get("added"):
        return flash_redirect("/", "web.check.added")
    if row and row.get("selection_updated"):
        return flash_redirect("/", "web.check.selection_updated")
    if row and row.get("changed"):
        return flash_redirect("/", "web.check.changed")
    if row and row.get("ok") and row.get("skipped"):
        return flash_redirect("/", "web.check.no_changes_reason", "ok", reason=row["skipped"])
    if row and row.get("ok"):
        return flash_redirect("/", "web.common.no_changes")
    return flash_redirect("/", "web.check.failed", "err")


# "Обновить все" runs in the background (D1): the request used to wait the whole check
# (half a minute and more) while holding the persistence lock for every other page.
_CHECK_JOB_LOCK = threading.Lock()
_check_job: dict[str, Any] = {}


def _check_all(job: dict[str, Any], apply: bool) -> None:
    # A plain thread does not inherit the request's language: the job carries it.
    lang = str(job.get("lang") or owner_language())
    try:
        out = services.run_check(apply=apply, notify=True, ignore_cool=True, how="manual")
        n = sum(1 for r in out["results"] if r.get("ok"))
        new = sum(1 for r in out["results"] if r.get("changed"))
        fail = sum(1 for r in out["results"] if not r.get("ok"))
        prefix = i18n.t("web.check_all.preview", lang) if not apply else i18n.t("web.check_all.applied", lang)
        summary = i18n.t("web.check_all.summary", lang, new=new, same=n - new, fail=fail)
        flash, status, kind = prefix + summary, "done", "warn" if fail else "ok"
    except SecretStoreError as exc:
        services.record_check_failure(exc, how="manual")
        flash, status, kind = i18n.t("web.check.blocked", lang), "failed", "err"
    except Exception as exc:  # noqa: BLE001 - the background job's boundary: the page polls for its result
        flash, status, kind = i18n.t("web.check_all.not_run", lang, error=type(exc).__name__), "failed", "err"
    # The page follows the job and then opens Home with its result (a message kept on the server).
    redirect = flash_location("/", flash, kind)
    with _CHECK_JOB_LOCK:
        job.update({"status": status, "flash": flash, "redirect": redirect, "finished_at": time.time()})


@router.post("/check")
def check(mode: str = Form("dry")) -> Response:
    with _CHECK_JOB_LOCK:
        if _check_job.get("status") == "running":
            return flash_redirect("/?check_job=" + quote(str(_check_job["id"])), "web.check_all.running", "warn")
        job: dict[str, Any] = {
            "id": uuid.uuid4().hex[:12],
            "status": "running",
            "started_at": time.time(),
            "lang": owner_language(),
        }
        _check_job.clear()
        _check_job.update(job)
    threading.Thread(target=_check_all, args=(_check_job, mode == "apply"), name="tow-check-all", daemon=True).start()
    return flash_redirect("/?check_job=" + job["id"], "web.check_all.started")


@router.get("/check/status")
def check_status(job: str = "") -> Response:
    with _CHECK_JOB_LOCK:
        current = dict(_check_job)
    if not job or current.get("id") != job:
        return JSONResponse({"status": "unknown"})
    body = {"status": current["status"], "flash": current.get("flash") or ""}
    if current.get("redirect"):
        body["redirect"] = current["redirect"]
    return JSONResponse(body)
