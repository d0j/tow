"""The zendriver-based service's explicit cookie-only command, not a browser dependency."""

from typing import Any

from tow.protection.providers import cookie_api
from tow.protection.spec import Clearance

KIND = "flare_bypasser"
TITLE = "FlareBypasser (zendriver)"


def solve(provider: dict[str, Any], origin: str, timeout: float) -> Clearance:
    return cookie_api(provider, origin, timeout, command="request.get_cookies", media_field=None)
