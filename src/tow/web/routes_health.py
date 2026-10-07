"""Routes: what a monitor and the page header poll - ``/healthz``, ``/health.json`` - and the favicon."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, Response

from tow import __version__, access
from tow.web import _context, services
from tow.web.templating import header_health
from tow.web.text import t
from tow.web_update import WebUpdateError

router = APIRouter()


@router.get("/updates.json", response_model=None)
def updates_json() -> dict[str, Any]:
    return services.release_status()


@router.post("/updates/check", response_model=None)
def updates_check() -> dict[str, Any]:
    return services.release_status(force=True)


@router.get("/updates/status")
def updates_status() -> Response:
    try:
        result = services.web_update_status()
    except WebUpdateError as exc:
        return JSONResponse({"ok": False, "error": t(str(exc))}, status_code=503)
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
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.post("/updates/install")
def updates_install(version: str = Form("")) -> Response:
    try:
        result = services.start_web_update(version)
    except WebUpdateError as exc:
        return JSONResponse({"ok": False, "error": t(str(exc))}, status_code=409)
    services.log_event("settings_update_started", target_version=result["target"], how="manual")
    return JSONResponse(result, status_code=202, headers={"Cache-Control": "no-store"})


@router.get("/updates/log")
def updates_log() -> Response:
    try:
        result = services.web_update_log()
    except WebUpdateError as exc:
        return JSONResponse({"ok": False, "error": t(str(exc))}, status_code=503)
    return JSONResponse({"text": result}, headers={"Cache-Control": "no-store"})


@router.get("/healthz", response_model=None)
def healthz(request: Request) -> dict[str, Any]:
    answer: dict[str, Any] = {"ok": True, "version": __version__}
    if access.is_local(request):  # which install answers: for tow start / setup / run on this computer
        answer["install"] = services.install_id()
    return answer


@router.get("/health.json", response_model=None)
def health_json() -> dict[str, Any]:
    import time

    return {
        "ok": True,
        **header_health(),
        "now_ts": time.time(),
        "topic_timers": services.topic_timer_status(_context.state()),
    }


@router.get("/favicon.ico")
def favicon() -> Response:
    return Response(status_code=204)
