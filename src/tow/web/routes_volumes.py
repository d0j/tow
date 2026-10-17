"""Routes: folders off the disk TOW recorded for them (tow.issues ``volume_missing``) and the
owner's "it is the same disk - accept" (tow.volumes).

Every signed-in device sees the page, a network device in scrubbed words (no folder); only this
computer may accept: the POST is local-only (``access.require_local``) and, as every write, from
TOW's own origin (the middleware's Origin check)."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response

from tow import access
from tow.web import _context, services
from tow.web.messages import flash_redirect, request_flash
from tow.web.templating import TEMPLATES
from tow.web.text import t
from tow.web.views import issue_text, issues_now

router = APIRouter()
_PAGE = "/volumes"
_OUTCOMES = {
    services.ACCEPTED_VOLUME: ("volumes.accepted", "ok"),
    services.BUSY_VOLUME: ("volumes.busy", "warn"),
    services.REFUSED_VOLUME: ("volumes.refused", "err"),
}


def entries(*, local: bool) -> list[dict[str, str]]:
    """The folders off their recorded disk now, each with the subject its accept names."""
    return [
        {
            "id": issue.target.anchor,
            "issue": issue.id,
            "text": issue_text(issue, local=local),
            "subject": issue.fix.subject if issue.fix else "",
        }
        for issue in issues_now(_context.state(), _context.config())
        if issue.kind == "volume_missing"  # tow.issues.kinds.volume_missing
    ]


@router.get(_PAGE, response_class=HTMLResponse)
def volumes_page(request: Request) -> Response:
    local = access.is_local(request)
    return TEMPLATES.TemplateResponse(
        request,
        "volumes.html",
        {
            "title": t("volumes.title"),
            "entries": entries(local=local),
            "can_accept": local,
            "flash": request_flash(request),
        },
    )


@router.post(f"{_PAGE}/accept", dependencies=[access.require_local(_PAGE, "volumes.local_only")])
def accept_volume(subject: str = Form("")) -> Response:
    # A check of one topic may run here (client only): no state mutation decorator around it.
    outcome = services.accept_volume(subject)
    key, kind = _OUTCOMES.get(outcome, ("volumes.unknown", "err"))
    return flash_redirect(_PAGE, key, kind)
