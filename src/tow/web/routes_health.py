"""Routes: what a monitor and the page header poll - ``/healthz``, ``/health.json`` - and the favicon."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import Response

from tow import __version__
from tow.web.templating import header_health

router = APIRouter()


@router.get("/healthz", response_model=None)
def healthz() -> dict[str, Any]:
    return {"ok": True, "version": __version__}


@router.get("/health.json", response_model=None)
def health_json() -> dict[str, Any]:
    return {"ok": True, **header_health()}


@router.get("/favicon.ico")
def favicon() -> Response:
    return Response(status_code=204)
