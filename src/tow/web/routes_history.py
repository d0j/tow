"""Routes: the history page and the event log the settings page polls."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from tow import access
from tow.web import _context, page_sync, services
from tow.web.messages import request_flash
from tow.web.templating import TEMPLATES
from tow.web.text import t, time_parts
from tow.web.views import issue_notes, technical_log_rows

router = APIRouter()

# The newest events History shows; the page says so only when the list stops there.
HISTORY_LIMIT = page_sync.HISTORY_LIMIT


@router.get("/history", response_class=HTMLResponse)
def history_page(request: Request, group: str = "", q: str = "") -> Response:
    from tow.log import HISTORY_GROUPS, index_event_titles

    group = group if group in HISTORY_GROUPS else ""
    state = _context.state()
    title_index = index_event_titles(state.get("topics") or [])
    events = services.history_events(group=group, text=q, limit=HISTORY_LIMIT, title_index=title_index)
    # Each line keyed by its event (page_sync.history_rows): the page merges the newest in by it.
    rows = page_sync.history_rows(events, state, _context.config(), local=access.is_local(request))
    return TEMPLATES.TemplateResponse(
        request,
        "history.html",
        {
            "title": t("web.title.history"),
            "rows": rows,
            "group": group,
            "q": q,
            "history_limit": HISTORY_LIMIT,
            "flash": request_flash(request),
            # What needs the owner and is explained here, in a note of its own.
            "notes": issue_notes(state, _context.config(), "history", local=access.is_local(request)),
        },
    )


@router.get("/log.json")
def log_json(request: Request) -> Response:
    rows = technical_log_rows(_context.state(), _context.config(), local=access.is_local(request))
    for row in rows:
        row["time"] = time_parts(row.get("iso"))
    return JSONResponse({"ok": True, "rows": rows})
