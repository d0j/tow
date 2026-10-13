"""FlareSolverr's documented cookie-only request.get protocol."""

from typing import Any

from tow.protection.providers import cookie_api
from tow.protection.spec import Clearance

KIND = "flaresolverr"
TITLE = "FlareSolverr"


def solve(provider: dict[str, Any], origin: str, timeout: float) -> Clearance:
    return cookie_api(provider, origin, timeout)
