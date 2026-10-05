"""Explicit metadata preparation, never a client add or a dry-run check."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from tow.episodes import parse_season_hint
from tow.errors import TowError
from tow.selection import SelectionPendingError, normalize_policy, resolve_selection
from tow.torrent import MAX_TORRENT_BYTES, parse_torrent_metadata
from tow.web import services

router = APIRouter()


def _error_response(error: TowError | None = None) -> JSONResponse:
    """Render catalog data only; never stringify an exception at the HTTP boundary."""
    public = error if error is not None else TowError("content.unavailable")
    return JSONResponse(
        {"error": public.text(), "code": public.code},
        status_code=400,
        headers={"Cache-Control": "no-store"},
    )


@router.post("/content/snapshot")
def content_snapshot(token: str = Form(), url: str = Form(), client_id: str = Form()) -> JSONResponse:
    """Restore a refused form without a second tracker request or a client operation."""
    try:
        from tow.content import describe
        from tow.guess import canon_watch_url

        blob = services.read_content(token, canon_watch_url(url.strip()), client_id)
        return JSONResponse(describe(blob, token), headers={"Cache-Control": "no-store"})
    except TowError as exc:
        return _error_response(exc)
    except ValueError, RuntimeError, OSError:
        return _error_response()


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
    except TowError as exc:
        return _error_response(exc)
    except ValueError, RuntimeError, OSError:
        return _error_response()


@router.post("/content/resolve")
def content_resolve(
    token: str = Form(),
    url: str = Form(),
    client_id: str = Form(),
    mode: str = Form(),
    value: str = Form(""),
    topic_id: str = Form(""),
    title: str = Form(""),
    tracking_mode: str = Form("watch"),
) -> JSONResponse:
    try:
        from tow.guess import canon_watch_url

        url = canon_watch_url(url.strip())
        metadata = parse_torrent_metadata(services.read_content(token, url, client_id))
        context = services.content_context_title(topic_id, url, client_id, title)
        policy = normalize_policy(mode, value, tracking_mode)
        try:
            plan = resolve_selection(metadata.files, policy, preferred_season=parse_season_hint(context))
        except SelectionPendingError as pending:
            if policy["tracking_mode"] != "watch":
                raise
            return JSONResponse(
                {
                    "indices": [],
                    "waiting": TowError("check.waiting_episodes", pending=pending.params.get("episodes", "")).text(),
                },
                headers={"Cache-Control": "no-store"},
            )
        return JSONResponse({"indices": list(plan.selected_indices)}, headers={"Cache-Control": "no-store"})
    except TowError as exc:
        return _error_response(exc)
    except ValueError, RuntimeError, OSError:
        return _error_response()
