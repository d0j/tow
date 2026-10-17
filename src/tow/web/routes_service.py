"""Routes: the TOW service in Settings - its status, autostart and restart (``tow.lifecycle``)."""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Form
from fastapi.responses import JSONResponse, RedirectResponse, Response

from tow.config import as_bool
from tow.web import edit_base, services, settings_merge
from tow.web.messages import flash_redirect
from tow.web.text import t

router = APIRouter()
_TARGET = "/settings?open=service"


@router.get("/settings/service/status.json")
def settings_service_status() -> Response:
    return JSONResponse(services.service_status())


@router.post("/settings/service/autostart")
def settings_service_autostart(
    enabled: str = Form(""),
    without_login: str = Form(""),
    base: str = Form(""),
    resolutions: dict[str, str] = edit_base.RESOLUTIONS,
) -> Response:
    # Each box on its own: a page opened before "without signing in" was changed on another
    # device sends that box back as it showed it, which must not put the old value back.
    merged = settings_merge.merged(
        settings_merge.AUTOSTART,
        _TARGET,
        base,
        settings_merge.autostart_now(services.service_status()),
        {"enabled": as_bool(enabled), "without_login": as_bool(without_login)},
        resolutions,
        {"autostart": t("settings.service.autostart"), "without_login": t("settings.service.without_login")},
        {},
    )
    if isinstance(merged, RedirectResponse):
        return merged
    requested = bool(merged.values["enabled"])
    unattended = requested and bool(merged.values["without_login"])
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
