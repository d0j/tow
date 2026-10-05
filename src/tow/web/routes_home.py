"""Route: Home, the list of watched topics."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from tow import access
from tow.folders import recent_save_roots
from tow.web import _context
from tow.web.middleware import is_browser_navigation
from tow.web.routes_password import first_run
from tow.web.templating import TEMPLATES
from tow.web.text import t
from tow.web.views import add_draft, attention, credential_prompt, request_flash, topic_rows

router = APIRouter()


def _first_steps(cfg: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """The empty Home's checklist: is a torrent client set up (and answering), is a messenger on."""
    from tow.clients.factory import client_configurations, client_secret_block
    from tow.notifiers import health as notify_health

    secrets = _context.secrets_or_none() or {}
    try:
        client_set = any(
            row.get("enabled", True) and client_secret_block(cfg, secrets, str(row["id"])).get("host")
            for row in client_configurations(cfg)
        )
    except RuntimeError, TypeError, ValueError, KeyError:
        client_set = False
    answers = bool((state.get("health") or {}).get("qbit_ok"))
    return {
        "client": ("ok" if answers else "saved") if client_set else "todo",
        "messenger": notify_health(secrets, state) is not None,
    }


@router.get("/", response_class=HTMLResponse)
def index(request: Request) -> Response:
    from tow.clients.factory import client_configurations, default_client_id

    cfg = _context.config()  # read-only snapshots of this request (tow.web._context)
    if is_browser_navigation(request) and access.is_local(request) and first_run(cfg):
        return RedirectResponse("/setup", status_code=303)
    state = _context.state()
    rows = topic_rows(state)
    # YAML accepts scalar keys; HTML/URL filter values are always text. Preserve
    # these legacy names without sorting mixed types or duplicating the All choice.
    tracker_options = sorted(
        {str(name) for name in cfg.get("trackers") or {} if str(name)}
        | {str(row["tracker"]) for row in rows if row.get("tracker")}
    )
    client_options = [row for row in client_configurations(cfg) if row.get("enabled", True)]
    return TEMPLATES.TemplateResponse(
        request,
        "index.html",
        {
            "title": t("web.title.home"),
            "topics": rows,
            "tracker_options": tracker_options,
            "save_roots": recent_save_roots(state),
            "client_options": client_options,
            "default_client_id": default_client_id(cfg),
            "credential_prompt": credential_prompt(request),
            "add_draft": add_draft(request),
            "flash": request_flash(request),
            "attention": attention(state, cfg),
            "first_steps": None if state.get("topics") else _first_steps(cfg, state),
        },
    )
