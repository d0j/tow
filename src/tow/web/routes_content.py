"""Explicit metadata preparation, never a client add or a dry-run check."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from tow.episodes import parse_season_hint
from tow.errors import TowError
from tow.selection import SelectionPendingError, normalize_policy, resolve_selection
from tow.torrent import MAX_TORRENT_BYTES, TorrentPathConflictError, parse_torrent_metadata
from tow.web import services

router = APIRouter()
_NO_STORE = {"Cache-Control": "no-store"}


def _error_response(error: TowError | None = None, status_code: int = 400) -> JSONResponse:
    """Render catalog data only; never stringify an exception at the HTTP boundary."""
    public = error if error is not None else TowError("content.unavailable")
    return JSONResponse({"error": public.text(), "code": public.code}, status_code=status_code, headers=_NO_STORE)


async def _answer(work: Callable[[], dict[str, Any]]) -> JSONResponse:
    """Run ``work`` off the event loop; every failure becomes a catalog message."""
    try:
        return JSONResponse(await run_in_threadpool(work), headers=_NO_STORE)
    except TorrentPathConflictError:
        return _error_response(TowError("content.path_conflict"))
    except TowError as exc:
        return _error_response(exc)
    except ValueError, RuntimeError, OSError:
        return _error_response()


@router.post("/content/snapshot")
async def content_snapshot(token: str = Form(), url: str = Form(), client_id: str = Form()) -> JSONResponse:
    """Restore a refused form without a second tracker request or a client operation."""

    def snapshot() -> dict[str, Any]:
        from tow.content import describe
        from tow.guess import canon_watch_url

        return describe(services.read_content(token, canon_watch_url(url.strip()), client_id), token)

    return await _answer(snapshot)


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
            return _error_response(TowError("content.too_large"), 413)

    def prepare() -> dict[str, Any]:
        if source not in {"torrent", "fresh", "magnet"} or (source != "torrent" and blob is not None):
            raise TowError("content.changed")
        if source == "magnet":
            return services.prepare_magnet_content(url, client_id)
        if source == "fresh":
            return services.prepare_fresh_content(url, client_id, allow_limited)
        return services.prepare_content(url, client_id, blob, allow_limited)

    return await _answer(prepare)


@router.post("/content/resolve")
async def content_resolve(
    token: str = Form(),
    url: str = Form(),
    client_id: str = Form(),
    mode: str = Form(),
    value: str = Form(""),
    topic_id: str = Form(""),
    title: str = Form(""),
    tracking_mode: str = Form("watch"),
) -> JSONResponse:
    def resolve() -> dict[str, Any]:
        from tow.guess import canon_watch_url

        link = canon_watch_url(url.strip())
        metadata = parse_torrent_metadata(services.read_content(token, link, client_id))
        context = services.content_context_title(topic_id, link, client_id, title)
        policy = normalize_policy(mode, value, tracking_mode)
        try:
            plan = resolve_selection(metadata.files, policy, preferred_season=parse_season_hint(context))
        except SelectionPendingError as pending:
            if policy["tracking_mode"] != "watch":
                raise
            waiting = TowError("check.waiting_episodes", pending=pending.params.get("episodes", "")).text()
            return {"indices": [], "waiting": waiting}
        return {"indices": list(plan.selected_indices)}

    return await _answer(resolve)
