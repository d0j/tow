"""Routes: the history page and the event log the settings page polls."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from tow.web.templating import TEMPLATES
from tow.web.text import t
from tow.web.views import request_flash

router = APIRouter()


@router.get("/history", response_class=HTMLResponse)
def history_page(request: Request, group: str = "", q: str = "") -> Response:
    from tow.log import HISTORY_GROUPS, format_event, history_events

    group = group if group in HISTORY_GROUPS else ""
    rows = [format_event(event) for event in history_events(group=group, text=q)]
    return TEMPLATES.TemplateResponse(
        request,
        "history.html",
        {"title": t("web.title.history"), "rows": rows, "group": group, "q": q, "flash": request_flash(request)},
    )


@router.get("/log.json")
def log_json() -> Response:
    from tow.log import format_event, read_events

    return JSONResponse({"ok": True, "rows": [format_event(e) for e in read_events(limit=200)]})
