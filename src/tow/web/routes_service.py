"""Routes: the TOW service in Settings - its status, autostart and restart (``tow.lifecycle``)."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Form
from fastapi.responses import JSONResponse, Response

from tow.clock import format_ui_timestamp
from tow.config import as_bool
from tow.web import services
from tow.web.text import t
from tow.web.views import flash_redirect

router = APIRouter()


def service_view() -> dict[str, Any]:
    service = services.service_status()
    from tow.pulse import clock
    from tow.watchdog import last_problem

    problem = last_problem()
    service["watchdog_problem"] = (
        {"at": clock(float(problem.get("at") or 0)), "lines": str(problem["text"]).splitlines()} if problem else None
    )
    restart = service.get("restart")
    if not isinstance(restart, dict):
        service["restart_view"] = None
        return service
    status = str(restart.get("status") or "unknown")
    reason = {
        "settings": t("web.service.reason.settings"),
        "restore": t("web.service.reason.restore"),
        "manual": t("web.service.reason.manual"),
    }.get(str(restart.get("reason") or "settings"), t("web.service.reason.unknown"))
    state = {
        "ready": t("web.service.state.ready"),
        "queued": t("web.service.state.queued"),
        "stopping": t("web.service.state.stopping"),
        "starting": t("web.service.state.starting"),
        "started": t("web.service.state.started"),
        "failed": t("web.service.state.failed"),
    }.get(status, t("web.service.state.unknown"))
    created_at = restart.get("created_at")
    try:
        at = format_ui_timestamp(str(created_at)) if created_at else t("web.service.date_unknown")
    except TypeError, ValueError:
        at = t("web.service.date_unknown")
    service["restart_view"] = {
        "at": at,
        "reason": reason,
        "state": state,
        "failed": status == "failed",
    }
    return service


@router.get("/settings/service/status.json")
def settings_service_status() -> Response:
    return JSONResponse(services.service_status())


@router.post("/settings/service/autostart")
def settings_service_autostart(enabled: str = Form(""), without_login: str = Form("")) -> Response:
    requested = as_bool(enabled)
    unattended = requested and as_bool(without_login)
    result = services.set_autostart(requested, without_login=unattended)
    read_back = services.service_status()
    confirmed = bool(read_back.get("autostart")) == requested
    hint = str(result.get("hint") or "")
    # "Without signing in" counts only when read back too - or when the OS needs one more step
    # from the owner (Linux lingering), which the hint then names.
    if requested and read_back.get("supports_without_login") and not hint:
        confirmed = confirmed and bool(read_back.get("without_login")) == unattended
    if not result.get("ok") or not confirmed:
        services.log_event("settings_service_autostart_fail", enabled=requested, result=result, how="manual")
        error = str(result.get("error") or "")
        flash = t("web.settings.autostart_unconfirmed") + (f": {error}" if error else "")
        return flash_redirect("/settings?open=service", flash, "err")
    services.log_event(
        "settings_service_autostart", enabled=requested, without_login=unattended, result=result, how="manual"
    )
    flash = t("web.settings.autostart_on") if requested else t("web.settings.autostart_off")
    if hint:
        flash += ". " + hint
    return flash_redirect("/settings?open=service", flash, "warn" if hint else "ok")


@router.post("/settings/service/restart")
def settings_service_restart() -> Response:
    result = services.request_restart()
    if not result.get("ok"):
        services.log_event("settings_service_restart_fail", result=result, how="manual")
        return flash_redirect("/settings?open=service", "web.settings.restart_not_started", "err")
    operation_id = str(result.get("operation_id") or "unknown")
    services.log_event("settings_service_restart", operation_id=operation_id, how="manual")
    return flash_redirect("/settings?open=service&operation=" + quote(operation_id), "web.settings.restart_started")
