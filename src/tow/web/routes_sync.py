"""Routes: one state on every open page (``tow.web.sync``) - the revisions a page polls,
``GET /sync.json``, Home's rows drawn again in batches, ``GET``/``POST /topics/rows``, and what
Sites, Settings and History draw again, ``GET /page/fragments`` (``tow.web.page_sync``).

Both only read: no recovery, cleanup, quarantine or folder is made on their way (the middleware
gives them a read-only path), and none waits for the persistence, check or log lock. Who may ask
is decided before any of it (the middleware: the host, the network, the session) - a 304 is
never an answer to a request that may not see the page. A refusal is JSON, never the sign-in
page: a script asked, not a browser tab.
"""

from __future__ import annotations

import json
from email.utils import formatdate
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from tow.web import page_sync, sync

router = APIRouter()

_CONDITIONAL = {"Cache-Control": "private, no-cache", "Vary": "Cookie, Accept-Language"}
_NO_STORE = {"Cache-Control": "no-store", "Vary": "Cookie, Accept-Language"}
MAX_ROWS_BODY = 64 * 1024


def _date() -> dict[str, str]:
    """The server's time in every answer, 304 too: a page sets its clock by it."""
    return {"Date": formatdate(usegmt=True)}


def _refused(error: str, status: int, *, retry_after: float | None = None) -> JSONResponse:
    headers = {**_NO_STORE, **_date()}
    if retry_after is not None:
        headers["Retry-After"] = str(int(retry_after))
    return JSONResponse({"ok": False, "error": error}, status_code=status, headers=headers)


def _limited(request: Request) -> JSONResponse | None:
    wait = sync.take_token(sync.rate_key(request))
    return _refused("rate_limited", 429, retry_after=wait) if wait else None


def _unavailable(exc: sync.Unavailable) -> JSONResponse:
    if exc.reason == "recovery":
        sync.recover_later()
    return _refused(
        {"recovery": "recovering", "busy": "busy", "config": "config_unreadable"}.get(exc.reason, "data_unavailable"),
        503,
        retry_after=sync.RETRY_AFTER_SEC,
    )


@router.get("/sync.json", response_model=None)
def sync_json(request: Request, have: str = "", page: str = "", group: str = "", q: str = "") -> Response:
    """The revision of every area this page shows and, for those that differ from ``have``, what
    they are now. The ETag names this exact request (its ``have``, page, History's filters
    ``group`` and ``q``, language, access and session); ``If-None-Match`` with it answers 304."""
    if (limited := _limited(request)) is not None:
        return limited
    try:
        wanted = sync.parse_have(have)
        page = sync.parse_page(page)
        filters = page_sync.filters(group, q) if page == "/history" else None
    except ValueError:
        return _refused("bad_request", 400)
    try:
        found = sync.answer(request, wanted, page, filters=filters)
    except sync.Unavailable as exc:
        return _unavailable(exc)
    headers = {**_CONDITIONAL, **_date(), "ETag": found.etag}
    if sync.etag_matches(request.headers.get("if-none-match"), found.etag):
        return Response(status_code=304, headers=headers)
    return JSONResponse(found.body(), headers=headers)


def _rows(request: Request, ids: list[Any], rev: object) -> Response:
    if (limited := _limited(request)) is not None:
        return limited
    if rev not in (None, "") and (not isinstance(rev, str) or not sync.REVISION.fullmatch(rev)):
        return _refused("bad_request", 400)
    try:
        wanted = sync.parse_ids(ids)
    except ValueError:
        return _refused("bad_request", 400)
    try:
        found = sync.rows(request, wanted)
    except sync.Unavailable as exc:
        return _unavailable(exc)
    # Drawn from another revision than the page asked for: it applies them knowing that.
    found["stale"] = bool(rev) and rev != found["rev"]
    return JSONResponse(found, headers={**_NO_STORE, **_date()})


@router.get("/topics/rows", response_model=None)
def topics_rows(request: Request, ids: str = "", rev: str = "") -> Response:
    """Home rows of ``ids`` (comma-separated, at most ``sync.MAX_IDS``), drawn from one sample:
    ``{"ok", "rev", "order", "rows": [{"id", "html", "hash"}], "missing", "deferred", "stale"}``."""
    if len(ids) > sync.MAX_IDS * 130:
        return _refused("bad_request", 400)
    return _rows(request, [part for part in ids.split(",") if part], rev)


@router.get("/page/fragments", response_model=None)
def page_fragments(request: Request, page: str = "", rev: str = "", group: str = "", q: str = "") -> Response:
    """What Sites, Settings or History draws again when its page area changed, drawn from one
    sample: Sites and Settings ``{"ok", "page", "rev", "order", "fragments": [{"id", "hash",
    "html"}], "stale"}`` (every fragment, in its order), History ``{"ok", "page", "rev",
    "entries": [{"id", "hash", "html"}], "limit", "stale"}`` (the newest lines its filters
    choose). ``stale``: drawn from another revision than ``rev`` - the page applies the whole of
    it and takes its revision."""
    if (limited := _limited(request)) is not None:
        return limited
    if page not in (*page_sync.FRAGMENT_PAGES, "/history") or (rev and not sync.REVISION.fullmatch(rev)):
        return _refused("bad_request", 400)
    try:
        filters = page_sync.filters(group, q) if page == "/history" else {}
    except ValueError:
        return _refused("bad_request", 400)
    try:
        found = sync.history_fragments(request, filters) if page == "/history" else sync.page_fragments(request, page)
    except sync.Unavailable as exc:
        return _unavailable(exc)
    found["stale"] = bool(rev) and rev != found["rev"]
    return JSONResponse(found, headers={**_NO_STORE, **_date()})


@router.post("/topics/rows", response_model=None)
async def topics_rows_post(request: Request) -> Response:
    """The same as GET for a long list: a JSON body ``{"ids": [...], "rev": "..."}``."""
    media = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if media != "application/json":
        return _refused("unsupported_media", 415)
    raw = await request.body()
    if len(raw) > MAX_ROWS_BODY:
        return _refused("too_large", 413)
    try:
        body = json.loads(raw)
    except ValueError:
        return _refused("bad_request", 400)
    if not isinstance(body, dict) or not isinstance(body.get("ids"), list):
        return _refused("bad_request", 400)
    from starlette.concurrency import run_in_threadpool

    return await run_in_threadpool(_rows, request, body["ids"], body.get("rev"))
