"""Routes: Recent messages, one list for every signed-in device (tow.messages_feed).

``GET /messages.json`` is the feed - conditional (ETag of its revision, 304), ``private,
no-cache``, after the middleware's sign-in and Host checks - and reads nothing but the log
reader's snapshot and data/ui.json. ``POST /messages/read`` and ``POST /messages/clear`` take
the ``cursor`` of the newest displayed entry (the middleware checks their Origin): a stale
cursor is 409, markers that cannot be saved 503, both with the words why.

All three take a token from ``/sync.json``'s rate limit, the same bucket per session of a device
on the network (per tab on this computer): beyond it they answer 429 with ``Retry-After``
before anything is read or written.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, Response

from tow.web import services, sync
from tow.web.messages import feed_payload, mark_messages
from tow.web.text import t

router = APIRouter()

_NO_STORE = {"Cache-Control": "no-store"}


def _matches(header: str | None, etag: str) -> bool:
    if not header:
        return False
    tags = {tag.strip().removeprefix("W/") for tag in header.split(",")[:16]}
    return "*" in tags or etag in tags


def _limited(request: Request, error: str) -> JSONResponse | None:
    """429 with ``Retry-After`` once this session's (or tab's) bucket is empty, else None."""
    wait = sync.take_token(sync.rate_key(request))
    if not wait:
        return None
    headers = {**_NO_STORE, "Retry-After": str(int(wait))}
    return JSONResponse({"ok": False, "error": error}, status_code=429, headers=headers)


@router.get("/messages.json")
def messages_json(request: Request) -> Response:
    if (limited := _limited(request, "rate_limited")) is not None:  # a script asked: the code, as /sync.json
        return limited
    payload = feed_payload()
    etag = f'"{payload["revision"]}"'
    headers = {"Cache-Control": "private, no-cache", "ETag": etag, "Vary": "Cookie, Accept-Language"}
    if _matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)
    return JSONResponse(payload, headers=headers)


def _mark(request: Request, cursor: str, *, clear: bool) -> Response:
    if (limited := _limited(request, t("web.message.rate_limited"))) is not None:  # the page says why
        return limited
    try:
        payload = mark_messages(cursor, clear=clear)
    except services.MessageMarkerRefused:
        return JSONResponse({"ok": False, "error": t("web.message.stale")}, status_code=409, headers=_NO_STORE)
    except OSError:
        return JSONResponse({"ok": False, "error": t("web.message.not_saved")}, status_code=503, headers=_NO_STORE)
    return JSONResponse({"ok": True, **payload}, headers=_NO_STORE)


@router.post("/messages/read")
def messages_read(request: Request, cursor: str = Form("", max_length=128)) -> Response:
    return _mark(request, cursor, clear=False)


@router.post("/messages/clear")
def messages_clear(request: Request, cursor: str = Form("", max_length=128)) -> Response:
    return _mark(request, cursor, clear=True)
