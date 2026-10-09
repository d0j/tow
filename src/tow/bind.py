from __future__ import annotations

import ipaddress
from urllib.parse import urlparse


def resolve_bind(host: str | None) -> str:
    h = (host or "127.0.0.1").strip() or "127.0.0.1"
    if h in ("::", "[::]"):
        return "0.0.0.0"
    return h


def is_loopback_bind(host: str | None) -> bool:
    value = resolve_bind(host)
    if value.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(value.strip("[]")).is_loopback
    except ValueError:
        return False


def validate_bind(host: str | None, *, allow_lan: bool = False) -> str:
    """The address to listen on; a ConfigError (a ValueError, in the owner's language) when it
    opens TOW to the network while network access is off."""
    from tow.config import ConfigError

    value = resolve_bind(host)
    if not allow_lan and not is_loopback_bind(value):
        raise ConfigError("config_error.bind_needs_network", bind=value)
    return value


def origin_matches_request(origin: str | None, request_host: str, request_scheme: str = "http") -> bool:
    if not origin or origin == "null":
        return False
    parsed = urlparse(origin)
    if (
        parsed.scheme.lower() != request_scheme.lower()
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        return False
    raw_host = (request_host or "").strip()
    request = urlparse(f"//{raw_host}")
    if (
        not raw_host
        or request.username
        or request.password
        or request.path
        or request.params
        or request.query
        or request.fragment
        or request.netloc != raw_host
    ):
        return False
    request_name = (request.hostname or "").lower().strip("[]")
    origin_name = parsed.hostname.lower().strip("[]")
    if not request_name or origin_name != request_name:
        return False
    try:
        origin_port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        request_port = request.port or (443 if request_scheme.lower() == "https" else 80)
    except ValueError:
        return False
    return origin_port == request_port
