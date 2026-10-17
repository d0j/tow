"""Routes: what a monitor and the page header poll - ``/healthz``, ``/health.json`` - and the favicon."""

from __future__ import annotations

from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, Response

from tow import __version__, access
from tow.clock import format_ui_timestamp
from tow.web import _context, services
from tow.web.templating import header_health
from tow.web.text import t, time_parts, zone_minutes
from tow.web_update import WebUpdateError

router = APIRouter()


def _with_time_labels(result: dict[str, Any], *fields: str) -> dict[str, Any]:
    """Each time (epoch seconds) also as the page's one time rule writes it (``<field>_time``:
    ``time_parts``, the Updates card's <time>), and written out in full for a page of an older
    version (``<field>_label``)."""
    out = dict(result)
    for field in fields:
        value = out.get(field)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            with suppress(OSError, OverflowError, ValueError):
                out[f"{field}_label"] = format_ui_timestamp(datetime.fromtimestamp(value, UTC))
                out[f"{field}_time"] = time_parts(value)
    return out


@router.get("/updates.json", response_model=None)
def updates_json() -> dict[str, Any]:
    return _with_time_labels(services.release_status(), "checked_at")


@router.post("/updates/check", response_model=None)
def updates_check() -> dict[str, Any]:
    return _with_time_labels(services.release_status(force=True), "checked_at")


def _said(exc: WebUpdateError) -> str:
    """The refusal in the owner's words: its key and its parameters are all catalog keys."""
    return t(str(exc), **{name: t(key) for name, key in exc.params.items()})


@router.get("/updates/status")
def updates_status() -> Response:
    try:
        result = services.web_update_status()
    except WebUpdateError as exc:
        return JSONResponse({"ok": False, "error": _said(exc)}, status_code=503)
    if result.get("reason"):
        result["message"] = t(result["reason"])
    error = result.get("error")
    if isinstance(error, str) and error in {
        "releases.launch_failed",
        "releases.broker_failed",
        "releases.inherited_job",
        "releases.parent_wait_failed",
        "releases.interrupted",
        "releases.update_failed",
        "releases.job_unreadable",
        "releases.worker_unverified",
    }:
        result["error_message"] = t(error)
    result = _with_time_labels(result, "started_at", "finished_at")
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.post("/updates/install")
def updates_install(version: str = Form("")) -> Response:
    try:
        result = services.start_web_update(version)
    except WebUpdateError as exc:
        return JSONResponse({"ok": False, "error": _said(exc)}, status_code=409)
    # The job's id is the operation id its result is logged with (tow.messages_feed.reconcile_update).
    services.log_event(
        "settings_update_started", operation_id=result.get("id"), target_version=result["target"], how="manual"
    )
    return JSONResponse(result, status_code=202, headers={"Cache-Control": "no-store"})


def _terminal_result(log: dict[str, Any]) -> str:
    """How an update from a terminal ended, from its exit code (scripts/update.py ``update``)."""
    code = log.get("code")
    if log.get("in_job"):
        return t("releases.log_result_in_job")
    if code is None:
        return t("releases.log_result_none")
    keys = {0: "releases.log_result_ok", 1: "releases.log_result_failed", 2: "releases.log_result_refused"}
    return t(keys[code]) if code in keys else t("releases.log_result_code", code=code)


def _log_time(value: object) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        with suppress(OSError, OverflowError, ValueError):
            return format_ui_timestamp(datetime.fromtimestamp(value, UTC))
    return "—"


@router.get("/updates/log")
def updates_log() -> Response:
    try:
        logs = services.web_update_logs()
    except WebUpdateError as exc:
        return JSONResponse({"ok": False, "error": _said(exc)}, status_code=503)
    if not any(log["kind"] == "terminal" for log in logs):
        text = "".join(log["text"] for log in logs)  # this page's update alone: its log as before
    else:
        # The latest update from a terminal too (its own process, X41), newest first, each
        # under a line that says where it was started, when, and (a terminal one) how it ended.
        parts = []
        for log in logs:
            if log["kind"] == "terminal":
                head = t("releases.log_terminal", time=_log_time(log["started_at"]), result=_terminal_result(log))
            else:
                head = t("releases.log_web", time=_log_time(log["started_at"]))
            parts.append(f"{head}\n{log['text'].strip(chr(10)) or t('js.releases.log_empty')}")
        text = "\n\n".join(parts)
    return JSONResponse({"text": text}, headers={"Cache-Control": "no-store"})


@router.get("/healthz", response_model=None)
def healthz(request: Request) -> dict[str, Any]:
    answer: dict[str, Any] = {"ok": True, "version": __version__}
    if access.is_local(request):  # which install answers: for tow start / setup / run on this computer
        answer["install"] = services.install_id()
    return answer


@router.get("/health.json", response_model=None)
def health_json(request: Request) -> dict[str, Any]:
    import time

    return {
        "ok": True,
        **header_health(local=access.is_local(request)),  # a network device's words are scrubbed
        "now_ts": time.time(),
        # TOW's UTC offset now, in minutes: a page open across a summer/winter time change keeps
        # "today" right (app.js towTime; it asks when the tab is shown again and at midnight).
        "tz_now": zone_minutes(),
        "topic_timers": services.topic_timer_status(_context.state()),
    }


@router.get("/favicon.ico")
def favicon() -> Response:
    return Response(status_code=204)
