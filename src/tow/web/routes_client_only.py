"""Revoke an unfinished one-time handoff without changing the native torrent."""

from __future__ import annotations

from contextlib import suppress

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from tow.errors import TowError
from tow.web import _context, client_only_actions, services
from tow.web.messages import flash_redirect

router = APIRouter()


@router.get("/client-only/{operation_id}")
def client_only_status(request: Request, operation_id: str) -> Response:
    return client_only_actions.status_page(request, operation_id)


@router.post("/client-only/{operation_id}/cancel")
def cancel_client_only(operation_id: str) -> RedirectResponse:
    try:
        previous = services.get_client_only_result(operation_id)
        if previous is None:
            return flash_redirect("/", "web.client_only.expired", "warn")
        if previous.get("status") != "pending" and (context := _context.current()) is not None:
            context.feedback_preview = True
        result = services.cancel_client_only(operation_id)
    except TowError as exc:
        return flash_redirect("/", exc, "err")
    # The producer retries a missed result from the durable terminal record.
    with suppress(OSError, ValueError, RuntimeError):
        services.reconcile_client_only_result(result)
    key = "cancelled" if result.get("status") == "cancelled" else "already_finished"
    return flash_redirect("/", f"web.client_only.{key}", "info")
