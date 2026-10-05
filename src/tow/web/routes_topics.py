"""Routes: one watched topic - add, edit (with a client-side move of its folder), pause, delete,
its edit panel and its download list."""

from __future__ import annotations

import copy
import heapq
import json
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from tow import undo
from tow.check import await_relocation, client_owned_by_tow
from tow.clock import iso_now
from tow.config import as_bool
from tow.errors import TowError
from tow.folders import paths_equal, recent_save_roots, remember_save_root, resolve_save_path, save_path_problem
from tow.log import error_fields
from tow.selection import normalize_policy, stored_policy
from tow.store import CheckBusyError, SecretStoreError
from tow.topic_timers import parse_interval, set_interval
from tow.torrent import parse_torrent_metadata
from tow.trackers import load_trackers, match_tracker
from tow.web import _context, services
from tow.web.templating import TEMPLATES
from tow.web.text import t
from tow.web.views import (
    add_refused_redirect,
    event_output,
    flash_redirect,
    home_redirect,
    manual_check_flash,
    request_flash,
    topic_check_row,
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


def _save_path_refusal(dest: str, *, current: str = "") -> str | None:
    """Every owner device may choose a new valid folder; protected folders remain refused."""
    from tow.folders import save_path_policy_problem

    cfg = services.load_config()
    if problem := save_path_problem(dest, allow_unc=as_bool(cfg.get("allow_unc_save_paths"))):
        return problem
    if current and paths_equal(dest, current):
        return None
    return save_path_policy_problem(dest)


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
    from tow.clients.factory import client_configurations, default_client_id

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


_DRAFT_FIELDS = (
    "url",
    "title",
    "save_path",
    "client_id",
    "selection_mode",
    "selection_value",
    "tracking_mode",
    "check_interval_min",
    "content_token",
    "selection_indices",
)


def _selection_form(
    mode: str,
    value: str,
    tracking: str,
    token: str,
    indices: str,
    url: str,
    client_id: str,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if mode != "exact":
        return normalize_policy(mode, value, tracking)
    if not token and not indices and previous and previous.get("mode") == "exact":
        return normalize_policy(
            mode, tracking_mode=tracking, files=previous.get("files"), source_hash=previous.get("source_hash")
        )
    if len(indices) > 200_000:
        raise TowError("selection.exact_invalid")
    try:
        parsed = json.loads(indices)
    except (ValueError, RecursionError) as exc:
        raise TowError("selection.exact_invalid") from exc
    return services.content_selection(token, url, client_id, parsed, tracking)


def _add_refused(problem: str, draft: dict[str, str], kind: str = "") -> RedirectResponse:
    """D2: a refused add reopens the form with what the owner typed and the reason."""

    return add_refused_redirect(problem, {k: v for k, v in draft.items() if k in _DRAFT_FIELDS}, kind=kind)


@router.post("/topics/add")
def topics_add(
    request: Request,
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
    from tow.clients.factory import client_configuration
    from tow.title import guess_topic_title, title_is_placeholder

    url = url.strip()
    from tow.guess import canon_watch_url

    draft = {
        "url": url,
        "title": title,
        "save_path": save_path,
        "client_id": client_id,
        "selection_mode": selection_mode,
        "selection_value": selection_value,
        "tracking_mode": tracking_mode,
        "check_interval_min": check_interval_min,
        "content_token": content_token,
        "selection_indices": selection_indices,
    }
    try:
        interval = parse_interval(check_interval_min)
    except TowError as exc:
        return _add_refused(str(exc), draft, "timer")
    url = canon_watch_url(url)
    state = services.load_state()
    cfg = services.load_config()
    trs = load_trackers(cfg)
    tracker = match_tracker(trs, url)
    if not tracker:
        unknown = TowError("web.topics.unknown_tracker", cls="no_tracker")
        services.log_event("check_fail", url=url, **error_fields(unknown), how="manual")
        return _add_refused(t("web.topics.unknown_link"), draft, "no_site")
    topics = state.setdefault("topics", [])
    if any(item.get("url") == url for item in topics):
        return flash_redirect("/", "web.topics.already_watched", "warn")
    name = title.strip()
    if title_is_placeholder(name, url):
        name = guess_topic_title(url) or name or url
    dest = resolve_save_path(save_path, state)
    if not dest:
        no_folder = TowError("web.topics.no_folder", cls="no_path")
        services.log_event("check_fail", title=name, url=url, **error_fields(no_folder), how="manual")
        return _add_refused(t("web.topics.need_folder"), draft, "folder")
    if problem := _save_path_refusal(dest):
        services.log_event("check_fail", title=name, url=url, error=problem, cls="no_path", how="manual")
        return _add_refused(problem, draft, "folder")
    try:
        selected_client = client_configuration(cfg, client_id or None)
    except RuntimeError, ValueError:
        return _add_refused(t("web.topics.choose_client"), draft, "client")
    if not selected_client.get("enabled", True):
        return _add_refused(t("web.topics.client_disabled"), draft, "client")
    try:
        policy = _selection_form(
            selection_mode,
            selection_value,
            tracking_mode,
            content_token,
            selection_indices,
            url,
            str(selected_client["id"]),
        )
        if content_token:
            prepared_hash = parse_torrent_metadata(
                services.read_content(content_token, url, str(selected_client["id"]))
            ).infohash
    except (ValueError, RuntimeError) as exc:
        return _add_refused(str(exc), draft, "selection")
    new: dict[str, Any] = {
        "id": uuid.uuid4().hex[:12],
        "title": name,
        "url": url,
        "save_path": dest,
        "hash": None,
        "client_id": str(selected_client["id"]),
        "selection": stored_policy(policy),
        "tracking_mode": policy["tracking_mode"],
    }
    if content_token:
        new["content_token"] = content_token
        new["content_hash"] = prepared_hash
    set_interval(new, interval)
    with services.persistence_lock():
        state = services.load_state()
        topics = state.setdefault("topics", [])
        if any(item.get("url") == url for item in topics):
            return flash_redirect("/", "web.common.already_exists", "warn")
        topics.append(new)
        remember_save_root(state, dest)
        undo.stamp(state, "topic_add", id=new["id"])
        services.save_state(state)
        undo.cleanup()
    services.log_event(
        "topic_add",
        topic=new["id"],
        title=name,
        url=url,
        path=dest,
        client_id=new["client_id"],
        selection_mode=policy["mode"],
        selection_value=policy["value"],
        tracking_mode=policy["tracking_mode"],
        check_interval_min=interval,
        how="manual",
    )
    try:
        out = services.run_check(apply=True, notify=True, ids=[new["id"]], ignore_cool=True, how="manual", wait=False)
        row = topic_check_row(new["id"], out)
        if row is None:
            return flash_redirect("/", "web.topics.added_no_result", "warn")
        if row and not row.get("ok"):
            refused = manual_check_flash(
                row,
                topic_id=new["id"],
                tracker_name=tracker.name if tracker.spec.get("login_path") else "",
            )
            if refused is not None:  # always, for a row that is not ok
                return refused
    except CheckBusyError:
        # Waiting here held this request - and every page behind it - for the whole other check.
        return flash_redirect("/", "web.topics.added_check_busy", "warn")
    except SecretStoreError as e:
        services.record_check_failure(e, how="manual")
        services.log_event(
            "add_check_blocked",
            topic=new["id"],
            title=name,
            url=url,
            path=dest,
            error="secrets_migration_required",
            cls="secret_gate",
            how="manual",
        )
        return flash_redirect("/", "web.topics.added_blocked", "warn")
    except Exception as e:  # noqa: BLE001 - the topic is saved; its first check's failure is logged and notified
        services.log_event(
            "add_check_fail",
            topic=new["id"],
            title=name,
            url=url,
            path=dest,
            **error_fields(e),
            how="manual",
        )
        from tow.notify import event_text
        from tow.notify import send as notify_send

        try:
            notify_send(services.load_secrets(), event_text(title=name, kind="error", error=e))
        except Exception as notify_exc:  # noqa: BLE001 - a lost message about the failure is logged, never a 500
            services.log_event("add_check_notify_fail", topic=new["id"], error=str(notify_exc), how="manual")
        return flash_redirect("/", "web.topics.added_check_failed", "warn")
    return flash_redirect("/", "web.topics.added_confirmed" if row.get("added") else "web.topics.added_nothing_new")


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
    return flash_redirect("/", "web.topics.deleted", "ok")


def _move_in_client(
    state: dict[str, Any], topic: dict[str, Any], tid: str, old_hash: str, old_dest: str, dest: str
) -> tuple[str, str]:
    """The topic's torrent moves to ``dest`` in its client (only one TOW added; the owner's edit
    asked for it): the save message's suffix and its kind. A failure is logged and said, never
    raised - the edit itself is kept."""
    move_client_id = str(topic.get("client_id") or "") or None
    try:
        from tow.clients.factory import from_secrets as client_from_secrets

        cfg = services.load_config()
        adapter = client_from_secrets(cfg, services.load_secrets(), move_client_id)
        client_kind = str(getattr(adapter, "client_kind", getattr(adapter, "kind", "client")))
        if not client_owned_by_tow(adapter, old_hash):
            raise RuntimeError(t("web.topics.not_owned"))
        adapter.set_location(old_hash, dest)
        outcome = await_relocation(adapter, old_hash, dest)
        topic["save_path"] = dest
        if outcome in ("moving", "failed"):
            # The client took the command but has not shown the new folder yet (a long move,
            # or a client that reports it late): reconcile accepts the old folder until the
            # client agrees, instead of failing every check with "path differs".
            topic["move_pending"] = {
                "from": old_dest,
                "to": dest,
                "since": iso_now(),
                **({"unconfirmed": True} if outcome == "failed" else {}),
            }
        else:
            topic.pop("move_pending", None)
        remember_save_root(state, dest)
        services.log_event(
            "qbit_move",
            topic=tid,
            title=topic.get("title"),
            path=dest,
            hash=old_hash,
            client_id=move_client_id,
            client_kind=client_kind,
            status={"done": "succeeded", "moving": "moving"}.get(outcome, "unconfirmed"),
            how="manual",
        )
        moved = {
            "done": t("web.topics.moved_done"),
            "moving": t("web.topics.moved_moving"),
        }.get(outcome, t("web.topics.moved_unconfirmed"))
        return moved, "ok" if outcome in ("done", "moving") else "warn"
    except Exception as e:  # noqa: BLE001 - any client failure of the move is logged and said; the edit is kept
        services.log_event(
            "qbit_move_fail",
            topic=tid,
            title=topic.get("title"),
            path=dest,
            hash=old_hash,
            client_id=move_client_id,
            **error_fields(e),
            how="manual",
        )
        return t("web.topics.move_failed"), "warn"


def _bind_content_edit(topic: dict[str, Any], candidate: dict[str, Any], client_id: str, token: str, mode: str) -> None:
    changed = candidate["url"] != topic.get("url") or client_id != topic.get("client_id", client_id)
    if mode == "exact" and not token and changed:
        raise TowError("content.changed")
    if topic.get("hash"):
        return
    if token:
        metadata = parse_torrent_metadata(services.read_content(token, str(candidate["url"]), client_id))
        candidate["content_token"] = token
        candidate["content_hash"] = metadata.infohash
    elif changed:
        candidate.pop("content_token", None)
        candidate.pop("content_hash", None)


@router.post("/topics/{tid}/edit")
@services.locked_state_mutation
def topics_edit(
    request: Request,
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
    state = services.load_state()
    moved, moved_kind = "", "ok"
    for topic in state.get("topics") or []:
        if str(topic.get("id")) != tid:
            continue
        candidate = copy.deepcopy(topic)
        try:
            if check_interval_min is not None:
                set_interval(candidate, parse_interval(check_interval_min))
        except TowError as exc:
            return flash_redirect("/", exc, "err")
        if title.strip():
            candidate["title"] = title.strip()
        if url.strip():
            from tow.guess import canon_watch_url

            url = canon_watch_url(url.strip())
            trs = load_trackers(services.load_config())
            if not match_tracker(trs, url):
                return flash_redirect("/", "web.topics.unknown_link_short", "err")
            if topic.get("hash") and url != str(topic.get("url") or ""):
                return flash_redirect("/", "web.topics.new_link", "warn")
            candidate["url"] = url
        try:
            from tow.clients.factory import client_configuration

            selected_client = client_configuration(services.load_config(), client_id or None)
        except RuntimeError, ValueError:
            return flash_redirect("/", "web.topics.choose_client_short", "err")
        new_client_id = str(selected_client["id"])
        if topic.get("hash") and new_client_id != str(topic.get("client_id") or new_client_id):
            return flash_redirect("/", "web.topics.client_locked", "err")
        old_selection = (
            topic.get("selection") if isinstance(topic.get("selection"), dict) else {"mode": "all", "value": ""}
        )
        try:
            policy = _selection_form(
                selection_mode,
                selection_value,
                tracking_mode,
                content_token,
                selection_indices,
                str(candidate["url"]),
                new_client_id,
                old_selection,
            )
            _bind_content_edit(topic, candidate, new_client_id, content_token, selection_mode)
        except (ValueError, RuntimeError) as exc:
            return flash_redirect("/", exc, "err")
        new_selection = stored_policy(policy)
        selection_changed = old_selection != new_selection
        tracking_changed = str(topic.get("tracking_mode") or "watch") != policy["tracking_mode"]
        candidate["client_id"] = new_client_id
        candidate["selection"] = new_selection
        candidate["tracking_mode"] = policy["tracking_mode"]
        if selection_changed and topic.get("hash"):
            candidate["selection_dirty"] = True
        if selection_changed or tracking_changed:
            candidate["once_done"] = False
        dest = resolve_save_path(save_path, state)
        if not dest:
            return flash_redirect("/", "web.topics.need_folder_short", "err")
        if problem := _save_path_refusal(dest, current=str(topic.get("save_path") or "")):
            return flash_redirect("/", problem, "err")
        undo.stamp(state, "topic_put", item=copy.deepcopy(topic))
        topic.update(candidate)
        old_dest = str(topic.get("save_path") or "")
        old_hash = str(topic.get("hash") or "")
        if not old_hash or paths_equal(old_dest, dest):
            topic["save_path"] = dest
            remember_save_root(state, dest)
        services.log_event(
            "topic_edit",
            topic=tid,
            title=topic.get("title"),
            url=topic.get("url"),
            path=dest,
            client_id=new_client_id,
            selection_mode=policy["mode"],
            selection_value=policy["value"],
            tracking_mode=policy["tracking_mode"],
            selection_changed=selection_changed,
            check_interval_min=topic.get("check_interval_min"),
            how="manual",
        )
        moved = ""
        if old_hash and not paths_equal(old_dest, dest):
            moved, moved_kind = _move_in_client(state, topic, tid, old_hash, old_dest, dest)
        break
    services.save_state(state)
    undo.cleanup()
    return flash_redirect("/", t("web.common.saved") + moved, moved_kind)


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
