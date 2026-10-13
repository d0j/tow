"""Routes: the history page and the event log the settings page polls."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from tow.web import _context, services
from tow.web.templating import TEMPLATES
from tow.web.text import t
from tow.web.views import issue_notes, request_flash

router = APIRouter()


@router.get("/history", response_class=HTMLResponse)
def history_page(request: Request, group: str = "", q: str = "") -> Response:
    from tow.log import HISTORY_GROUPS, format_event, index_event_titles, index_site_titles

    group = group if group in HISTORY_GROUPS else ""
    state = _context.state()
    title_index = index_event_titles(state.get("topics") or [])
    site_titles = index_site_titles(_context.config())
    rows = []
    seen: set[str] = set()
    for event in services.history_events(group=group, text=q, title_index=title_index):
        row = format_event(event, title_index=title_index, site_titles=site_titles)
        # The newest line of each kind has an address ("Needs attention" links to the newest
        # failed check: #latest-check_blocked).
        kind = str(event.get("kind") or "")
        if kind and kind not in seen:
            seen.add(kind)
            row["anchor"] = f"latest-{kind}"
        rows.append(row)
    return TEMPLATES.TemplateResponse(
        request,
        "history.html",
        {
            "title": t("web.title.history"),
            "rows": rows,
            "group": group,
            "q": q,
            "flash": request_flash(request),
            # What needs the owner and is explained here, in a note of its own.
            "notes": issue_notes(state, _context.config(), "history"),
        },
    )


@router.get("/log.json")
def log_json() -> Response:
    from tow.log import format_event, index_event_titles, index_site_titles

    title_index = index_event_titles(_context.state().get("topics") or [])
    site_titles = index_site_titles(_context.config())
    rows = [format_event(e, title_index=title_index, site_titles=site_titles) for e in services.read_events(limit=200)]
    return JSONResponse({"ok": True, "rows": rows})
