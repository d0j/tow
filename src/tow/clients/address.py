"""A torrent client's Web UI address, as the settings give it (a helper module, not a client)."""

from __future__ import annotations

from urllib.parse import urlsplit


def base_url(host: str, port: int) -> str:
    """The client's address with ``port`` unless the address names one; an IPv6 address
    (``[fd00::5]``, its colons are not a port) keeps its brackets."""
    host = host.strip().rstrip("/")
    if "://" not in host:
        host = f"http://{host}"
    scheme, rest = host.split("://", 1)
    netloc, _, tail = rest.partition("/")
    if netloc.count(":") > 1 and not netloc.startswith("["):
        netloc = f"[{netloc}]"  # a bare IPv6 address
    try:
        named = urlsplit(f"{scheme}://{netloc}").port is not None
    except ValueError:
        named = True  # a port that is not a number: shown as written, the client says it is wrong
    if not named:
        netloc = f"{netloc}:{port}"
    return f"{scheme}://{netloc}" + (f"/{tail}" if tail else "")
