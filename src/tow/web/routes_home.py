"""Route: Home, the list of watched topics."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from tow import access
from tow.folders import recent_save_roots
from tow.web import _context, check_jobs
from tow.web.messages import request_flash
from tow.web.middleware import is_browser_navigation
from tow.web.templating import TEMPLATES, header_health
from tow.web.text import t
from tow.web.views import (
    add_draft,
    check_job_message,
    check_progress,
    check_result_flash,
    credential_prompt,
    first_run,
    global_interval_minutes,
    topic_rows,
)

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
    # The same three states as the header's client name (AGENTS.md): green when it answers,
    # red when it did not, grey until a check or the diagnostics asked it.
    tone = header_health()["qbit_tone"]
    return {
        "client": {"ok": "ok", "bad": "failed"}.get(tone, "saved") if client_set else "todo",
        "messenger": notify_health(secrets, state) is not None,
    }


def _activity_text(job: dict[str, Any] | None) -> str:
    """The note of the page that follows a manual check: where it is, that it finished (opened
    again after it ended: reload, Back, no script), or that this server no longer knows it."""
    if job is None:
        return t("web.check_all.unconfirmed")
    if job["status"] in {"queued", "running"}:
        return check_progress(job)
    return t("web.check_all.finished")


def _without(request: Request, name: str) -> str:
    """This page's address without the parameter ``name`` (and without a fragment): what closing
    the card it opened leaves - the filters and the open row stay."""
    from urllib.parse import urlencode

    query = [(key, value) for key, value in request.query_params.multi_items() if key not in {name, "flash"}]
    return request.url.path + ("?" + urlencode(query) if query else "")


@router.get("/", response_class=HTMLResponse)
def index(request: Request) -> Response:
    from tow.clients.factory import client_configurations, default_client_id

    cfg = _context.config()  # read-only snapshots of this request (tow.web._context)
    if is_browser_navigation(request) and access.is_local(request) and first_run(cfg):
        return RedirectResponse("/setup", status_code=303)
    state = _context.state()
    rows = topic_rows(state, local=access.is_local(request))
    for row in rows:
        row["manual_job"] = check_jobs.pending_topic(str(row["id"]))
    # YAML accepts scalar keys; HTML/URL filter values are always text. Preserve
    # these legacy names without sorting mixed types or duplicating the All choice.
    tracker_options = sorted(
        {str(name) for name in cfg.get("trackers") or {} if str(name)}
        | {str(row["tracker"]) for row in rows if row.get("tracker")}
    )
    client_options = [row for row in client_configurations(cfg) if row.get("enabled", True)]
    result_id = str(request.query_params.get("check_result") or "")
    result = check_jobs.get(result_id) if result_id else None
    active_id = str(request.query_params.get("check_job") or "")
    active = check_jobs.get(active_id) if active_id else None
    # A topic's check ends in one message, as a check at once does - never a result card. A check
    # that still waits or runs is followed as on the page that follows it, not shown as a card
    # that only says "waiting".
    topic_result = bool(result and result.get("topic_id"))
    pending_result = bool(result and result["status"] in {"queued", "running"})
    if pending_result and not active_id:
        active_id, active = result_id, result
    said = check_result_flash(result) if result else None
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
            # What an empty timer field means, said under it (the add form and an opened row).
            "global_interval_min": global_interval_minutes(cfg),
            "credential_prompt": credential_prompt(request),
            "add_draft": add_draft(request),
            "flash": request_flash(request) or said,
            "check_result": result,
            # When the result was observed: its check's end, not when it was asked for.
            "check_result_at": datetime.fromtimestamp(result["ended_at"], UTC)
            if result and result.get("ended_at")
            else None,
            "check_result_message": check_job_message(result) if result and not said else "",
            "check_result_requested": bool(result_id) and not topic_result and not pending_result,
            "check_result_close": _without(request, "check_result"),
            "check_activity": active,
            "check_activity_id": active_id,
            # The page follows a result that is not ready yet (checks.js takes it over).
            "check_activity_result": bool(result_id) and active_id == result_id,
            "check_activity_text": _activity_text(active),
            "check_activity_title": next(
                (row["series"] for row in rows if active and str(row["id"]) == active["topic_id"]),
                t("base.check_all.title"),
            ),
            "check_activity_requested": bool(active_id),
            # A "Needs attention" link opens this topic's row (?open=<id>).
            "landing_topic": str(request.query_params.get("open") or ""),
            "first_steps": None if state.get("topics") else _first_steps(cfg, state),
        },
    )
