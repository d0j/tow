"""Routes: one watched topic - add, edit (with a client-side move of its folder), pause, delete,
its edit panel and its download list."""

from __future__ import annotations

import heapq
from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from tow import undo
from tow.clients.factory import client_configurations, default_client_id
from tow.errors import TowError
from tow.folders import recent_save_roots
from tow.topic_form import TopicForm
from tow.web import _context, services, topic_actions
from tow.web.templating import TEMPLATES
from tow.web.text import t
from tow.web.views import (
    event_output,
    flash_redirect,
    home_redirect,
    request_flash,
    topic_progress_summary,
    topic_rows,
    ui_time,
)

router = APIRouter()


async def _interval_form(request: Request) -> str | None:
    # FastAPI treats an empty optional Form value like omission. An empty field
    # intentionally restores the global timer; older callers omitting it keep theirs.
    form = await request.form()
    return str(form["check_interval_min"]) if "check_interval_min" in form else None


@router.get("/topics/{tid}/downloads.json", response_model=None)
def topic_downloads(tid: str, limit: int = 100, offset: int = 0) -> dict[str, Any] | Response:
    state = services.load_state()
    topic = next((item for item in state.get("topics") or [] if str(item.get("id")) == tid), None)
    if topic is None:
        return JSONResponse({"ok": False, "error": "topic not found"}, status_code=404)
    history = services.load_download_history()
    record = (history.get("topics") or {}).get(tid) or {}
    limit = max(1, min(500, int(limit or 100)))
    offset = max(0, int(offset or 0))
    raw_items = (record.get("items") or {}).values()
    total = len(record.get("items") or {})
    selected = heapq.nsmallest(
        min(total, offset + limit),
        raw_items,
        key=lambda row: str(row.get("label") or row.get("identity") or ""),
    )[offset:]
    items = []
    for item in selected:
        row = dict(item)
        if row.get("completed_observed_at"):
            row["completed_at"] = ui_time(row["completed_observed_at"])
        items.append(row)
    last = record.get("last_completed") or {}
    last_out = dict(last) if last else None
    if last_out and last_out.get("completed_observed_at"):
        last_out["completed_at"] = ui_time(last_out["completed_observed_at"])
    last_event = record.get("last_event") or None
    return {
        "ok": True,
        "topic": {"id": tid, "title": topic.get("tracker_title") or topic.get("title") or ""},
        "summary": topic_progress_summary(topic, history),
        "last_event": event_output(last_event) if last_event else None,
        "last_completed": last_out,
        "items": items,
        "pagination": {"offset": offset, "limit": limit, "total": total, "has_more": offset + len(items) < total},
    }


def _topic_edit_context(tid: str) -> dict[str, Any] | None:
    """M2: one topic's edit panel, rendered on demand instead of 200 forms inside Home."""
    state = _context.state()
    topic = next((item for item in state.get("topics") or [] if str(item.get("id")) == tid), None)
    if topic is None:
        return None
    cfg = _context.config()
    return {
        "topic": topic_rows({**state, "topics": [topic]})[0],
        "client_options": [row for row in client_configurations(cfg) if row.get("enabled", True)],
        "default_client_id": default_client_id(cfg),
        "save_roots": recent_save_roots(state),
    }


@router.get("/topics/{tid}/edit-panel", response_class=HTMLResponse)
def topics_edit_panel(request: Request, tid: str) -> Response:
    context = _topic_edit_context(tid)
    if context is None:
        from markupsafe import escape

        return HTMLResponse(f'<p class="form-error">{escape(t("web.topics.not_found"))}</p>', status_code=404)
    return TEMPLATES.TemplateResponse(request, "_topic_edit.html", context)


@router.get("/topics/{tid}/edit", response_class=HTMLResponse)
def topics_edit_page(request: Request, tid: str) -> Response:
    """The edit panel as a page of its own: the no-script way to open it."""
    context = _topic_edit_context(tid)
    if context is None:
        return home_redirect("web.topics.not_found", "err")
    return TEMPLATES.TemplateResponse(
        request,
        "topic_edit.html",
        {**context, "title": t("home.edit.page_title"), "flash": request_flash(request), "standalone": True},
    )


@router.post("/topics/add")
def topics_add(
    url: str = Form(),
    title: str = Form(""),
    save_path: str = Form(""),
    client_id: str = Form(""),
    selection_mode: str = Form("all"),
    selection_value: str = Form(""),
    tracking_mode: str = Form("watch"),
    check_interval_min: str = Form(""),
    content_token: str = Form(""),
    selection_indices: str = Form(""),
) -> Response:
    form = TopicForm(
        url=url.strip(),
        title=title,
        save_path=save_path,
        client_id=client_id,
        selection_mode=selection_mode,
        selection_value=selection_value,
        tracking_mode=tracking_mode,
        check_interval_min=check_interval_min,
        content_token=content_token,
        selection_indices=selection_indices,
    )
    return topic_actions.add_topic(form)


@router.post("/topics/{tid}/delete")
@services.locked_state_mutation
def topics_delete(tid: str) -> Response:
    state = services.load_state()
    topics = list(state.get("topics") or [])
    gone = next((item for item in topics if str(item.get("id")) == tid), None)
    if gone is None:
        return flash_redirect("/", "web.topics.delete_not_found", "warn")
    if gone:
        # M5: undo puts the topic back where it was, not at the top.
        undo.stamp(state, "topic", item=gone, index=topics.index(gone))
        services.log_event(
            "topic_delete",
            topic=tid,
            title=gone.get("title"),
            url=gone.get("url"),
            path=gone.get("save_path"),
            history="retained",
            client_torrent="untouched",
            how="manual",
        )
    state["topics"] = [item for item in topics if str(item.get("id")) != tid]
    services.save_state(state)
    undo.cleanup()  # the secrets of an undo this one replaced, if their removal was postponed
    try:
        services.forget_cached_content(str(gone.get("url") or ""))
    except TowError, OSError, ValueError, RuntimeError:
        return flash_redirect("/", "content.cache_delete_failed", "warn")
    return flash_redirect("/", "web.topics.deleted", "ok")


@router.post("/topics/{tid}/edit")
@services.locked_state_mutation
def topics_edit(
    tid: str,
    title: str = Form(""),
    url: str = Form(""),
    save_path: str = Form(""),
    client_id: str = Form(""),
    selection_mode: str = Form("all"),
    selection_value: str = Form(""),
    tracking_mode: str = Form("watch"),
    check_interval_min: str | None = Depends(_interval_form),
    content_token: str = Form(""),
    selection_indices: str = Form(""),
) -> Response:
    form = TopicForm(
        url=url,
        title=title,
        save_path=save_path,
        client_id=client_id,
        selection_mode=selection_mode,
        selection_value=selection_value,
        tracking_mode=tracking_mode,
        check_interval_min=check_interval_min,
        content_token=content_token,
        selection_indices=selection_indices,
    )
    return topic_actions.edit_topic(tid, form)


@router.post("/topics/{tid}/pause")
@services.locked_state_mutation  # read-modify-write: a check finishing meanwhile must not be lost
def topics_pause(tid: str) -> Response:
    state = services.load_state()
    topic = next((item for item in state.get("topics") or [] if str(item.get("id")) == tid), None)
    if topic is None:
        return flash_redirect("/", "web.topics.not_found", "err")
    topic["paused"] = not bool(topic.get("paused"))
    services.save_state(state)
    services.log_event("topic_pause", topic=tid, title=topic.get("title"), paused=topic["paused"], how="manual")
    return flash_redirect("/", "web.topics.paused" if topic["paused"] else "web.topics.resumed")
