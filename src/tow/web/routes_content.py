"""Explicit metadata preparation, never a client add or a dry-run check."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from tow.errors import TowError
from tow.selection import normalize_policy, resolve_selection
from tow.torrent import MAX_TORRENT_BYTES, parse_torrent_metadata
from tow.web import services

router = APIRouter()


@router.post("/content/snapshot")
def content_snapshot(token: str = Form(), url: str = Form(), client_id: str = Form()) -> JSONResponse:
    """Restore a refused form without a second tracker request or a client operation."""
    try:
        from tow.content import describe
        from tow.guess import canon_watch_url

        blob = services.read_content(token, canon_watch_url(url.strip()), client_id)
        return JSONResponse(describe(blob, token), headers={"Cache-Control": "no-store"})
    except (ValueError, RuntimeError, OSError) as exc:
        message = str(exc) if isinstance(exc, TowError) else str(TowError("content.unavailable"))
        return JSONResponse({"error": message}, status_code=400, headers={"Cache-Control": "no-store"})


@router.post("/content/prepare")
async def content_prepare(
    url: str = Form(),
    client_id: str = Form(""),
    allow_limited: bool = Form(False),
    torrent: Annotated[UploadFile | None, File()] = None,
    source: str = Form("torrent"),
) -> JSONResponse:
    blob = None
    if torrent is not None:
        blob = await torrent.read(MAX_TORRENT_BYTES + 1)
        await torrent.close()
        if len(blob) > MAX_TORRENT_BYTES:
            return JSONResponse(
                {"error": str(TowError("content.too_large")), "code": "content.too_large"},
                status_code=413,
                headers={"Cache-Control": "no-store"},
            )
    try:
        if source not in {"torrent", "magnet"} or (source == "magnet" and blob is not None):
            raise TowError("content.changed")
        if source == "magnet":
            result = await run_in_threadpool(services.prepare_magnet_content, url, client_id)
        else:
            result = await run_in_threadpool(services.prepare_content, url, client_id, blob, allow_limited)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    except (ValueError, RuntimeError, OSError) as exc:
        message = str(exc) if isinstance(exc, TowError) else str(TowError("content.unavailable"))
        return JSONResponse(
            {"error": message, "code": exc.code if isinstance(exc, TowError) else "content.unavailable"},
            status_code=400,
            headers={"Cache-Control": "no-store"},
        )


@router.post("/content/resolve")
def content_resolve(
    token: str = Form(),
    url: str = Form(),
    client_id: str = Form(),
    mode: str = Form(),
    value: str = Form(""),
) -> JSONResponse:
    try:
        from tow.guess import canon_watch_url

        metadata = parse_torrent_metadata(services.read_content(token, canon_watch_url(url.strip()), client_id))
        plan = resolve_selection(metadata.files, normalize_policy(mode, value))
        return JSONResponse({"indices": list(plan.selected_indices)}, headers={"Cache-Control": "no-store"})
    except (ValueError, RuntimeError, OSError) as exc:
        message = str(exc) if isinstance(exc, TowError) else str(TowError("content.unavailable"))
        return JSONResponse({"error": message}, status_code=400, headers={"Cache-Control": "no-store"})
