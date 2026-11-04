"""Send the chosen torrent to a client without creating a watched topic."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import replace
from typing import Any
from uuid import uuid4

from fastapi import Request
from fastapi.responses import RedirectResponse, Response

from tow.clients.factory import client_configuration, client_name
from tow.episodes import parse_season_hint
from tow.errors import TowError
from tow.guess import canon_watch_url
from tow.topic_form import TopicForm
from tow.torrent import parse_torrent_metadata
from tow.web import _context, services
from tow.web.messages import flash_redirect, topic_subject, use_subject
from tow.web.templating import TEMPLATES
from tow.web.text import t
from tow.web.views import add_refused_redirect

_OPERATION = re.compile(r"[0-9a-f]{32}")


def _fingerprint(form: TopicForm) -> str:
    """Bind a replay to its original form without opening an expired preparation."""
    fields = {
        "url": canon_watch_url(form.url.strip()),
        "client": form.client_id.strip(),
        "folder": form.save_path.strip(),
        "mode": form.selection_mode.strip(),
        "value": form.selection_value.strip(),
        "content": form.content_token.strip(),
        "indices": form.selection_indices.strip(),
        "season": parse_season_hint(form.title),
    }
    return hashlib.sha256(json.dumps(fields, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def _refused(form: TopicForm, error: Mapping[str, Any] | None = None) -> RedirectResponse:
    reason = services.site_error_summary(services.without_paths(error or {}), fallback=t("web.client_only.unconfirmed"))
    # A corrected request is a new intent. A submitted operation keeps its own immutable receipt.
    return add_refused_redirect(reason, replace(form, submission_id="").draft(), kind="selection")


def _response(result: Mapping[str, Any], form: TopicForm, *, repeated: bool = False) -> RedirectResponse:
    title = str(result.get("title") or "")
    use_subject(topic_subject(title))
    if repeated and (context := _context.current()) is not None:
        context.feedback_preview = True  # an old result is observed, no new action was performed
    status = result.get("status")
    if status == "failed":
        error = result.get("error")
        return _refused(form, error if isinstance(error, Mapping) else None)
    if status in ("added", "existing") and result.get("confirmed") is True:
        key = "replayed" if repeated else str(status)
        return flash_redirect("/", f"web.client_only.{key}", "ok", href="/history")
    if status == "cancelled":
        return flash_redirect("/", "web.client_only.cancelled", "info", href="/history")
    if status == "pending":
        operation_id = str(result.get("operation_id") or "")
        cancel = f"/client-only/{operation_id}/cancel" if _OPERATION.fullmatch(operation_id) else ""
        href = f"/client-only/{operation_id}" if _OPERATION.fullmatch(operation_id) else "/history"
        return flash_redirect(
            "/", "web.client_only.pending", "info", href=href, link=t("web.client_only.details"), cancel=cancel
        )
    return _refused(form)


def replay(form: TopicForm) -> RedirectResponse | None:
    """A retry of an accepted POST needs neither the site nor its preparation again."""
    operation_id = form.submission_id.strip()
    if not operation_id:
        return None
    if not _OPERATION.fullmatch(operation_id):
        return _refused(form, TowError("web.client_only.expired").record())
    try:
        result = services.get_client_only_result(operation_id, request_fingerprint=_fingerprint(form))
    except TowError as exc:
        return _refused(form, exc.record())
    except OSError, ValueError, RuntimeError:
        return _refused(form, TowError("client_only.store_invalid").record())
    return _response(result, form, repeated=True) if result is not None else None


def send(
    form: TopicForm, topic: dict[str, Any], policy: dict[str, Any], *, prepared_content: bytes | None = None
) -> RedirectResponse:
    """Validate content and enqueue one finite handoff; never save a watch or run its check."""
    operation_id = form.submission_id.strip() or uuid4().hex
    if not _OPERATION.fullmatch(operation_id):
        return _refused(form, TowError("web.client_only.expired").record())
    request_fingerprint = _fingerprint(form)
    prepared_form = replace(form, submission_id=operation_id)
    url, client_id = str(topic["url"]), str(topic["client_id"])
    try:
        if prepared_content is not None:
            blob = prepared_content
        elif form.content_token:
            blob = services.read_content(form.content_token, url, client_id)
        else:
            prepared = services.prepare_content(url, client_id, None, False)
            prepared_form = replace(prepared_form, content_token=str(prepared["token"]))
            blob = services.read_content(prepared_form.content_token, url, client_id)
        metadata = parse_torrent_metadata(blob)
        season = parse_season_hint(form.title)
        if season is None:
            season = parse_season_hint(metadata.name)
        result = services.send_to_client(
            blob,
            str(topic["save_path"]),
            client_id,
            policy,
            operation_id,
            preferred_season=season,
            request_fingerprint=request_fingerprint,
        )
    except TowError as exc:
        return _refused(prepared_form, exc.record())
    except OSError, ValueError, RuntimeError:
        return _refused(prepared_form)
    if result.get("status") in ("added", "existing", "failed", "cancelled"):
        # A failed event append does not undo a confirmed native result; the producer retries it.
        with suppress(OSError, ValueError, RuntimeError):
            services.reconcile_client_only_result(result)
    return _response(result, prepared_form)


def reconcile_results() -> int:
    """Complete pending initial handoffs and share terminal results, including after restart."""
    services.advance_client_only(limit=4)
    return sum(bool(services.reconcile_client_only_result(result)) for result in services.finished_client_only())


def _client_title(client_id: str) -> str:
    try:
        return client_name(client_configuration(_context.config(), client_id))
    except OSError, ValueError, RuntimeError:
        return t("web.client_only.client_unknown")


def status_page(request: Request, operation_id: str) -> Response:
    """A durable link to the initial handoff, read without polling the native client."""
    result, problem, code = None, "", 200
    try:
        result = services.get_client_only_result(operation_id) if _OPERATION.fullmatch(operation_id) else None
        if result is None:
            problem, code = t("web.client_only.missing"), 404
    except TowError as exc:
        problem, code = services.site_error_summary(services.without_paths(exc.record())), 503
    except OSError, ValueError, RuntimeError:
        problem, code = t("client_only.store_invalid"), 503
    status, phase, reason = "", "", ""
    selected = None
    if result is not None:
        status = str(result.get("status") or "")
        if (status in ("added", "existing") and result.get("confirmed") is not True) or status not in (
            "pending",
            "added",
            "existing",
            "failed",
            "cancelled",
        ):
            problem, code = t("web.client_only.unconfirmed"), 503
        if status == "pending" and result.get("phase") in (
            "queued",
            "observing_add",
            "checking",
            "ready",
            "observing_start",
        ):
            phase = t(f"web.client_only.phase.{result['phase']}")
        if status == "failed":
            error = result.get("error")
            reason = services.site_error_summary(
                services.without_paths(error if isinstance(error, Mapping) else {}),
                fallback=t("web.client_only.unconfirmed"),
            )
        count = result.get("selected_file_count")
        if type(count) is int and count >= 0:
            selected = count
    return TEMPLATES.TemplateResponse(
        request,
        "client_only.html",
        {
            "title": t("web.client_only.page_title"),
            "operation_id": operation_id,
            "operation": result,
            "operation_status": status,
            "operation_phase": phase,
            "operation_reason": reason,
            "operation_client": _client_title(str(result.get("client_id") or "")) if result else "",
            "operation_selected": selected,
            "operation_error": problem,
        },
        status_code=code,
    )
