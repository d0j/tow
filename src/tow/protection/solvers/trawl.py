"""The independently operated Camoufox service's cookie-only compatibility API."""

from typing import Any

from tow.protection.providers import cookie_api
from tow.protection.spec import Clearance

KIND = "trawl"
TITLE = "Trawl"


def solve(provider: dict[str, Any], origin: str, timeout: float) -> Clearance:
    return cookie_api(provider, origin, timeout, media_field=None)
