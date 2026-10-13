"""Byparr's documented FlareSolverr-compatible cookie-only API."""

from typing import Any

from tow.protection.providers import cookie_api
from tow.protection.spec import Clearance

KIND = "byparr"
TITLE = "Byparr"


def solve(provider: dict[str, Any], origin: str, timeout: float) -> Clearance:
    return cookie_api(provider, origin, timeout, media_field="blockMedia")
