"""What the site form accepts: mirror hosts (never an address inside the home network unless
allowed), the site name, paths and regular expressions."""

from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlparse

from tow.config import as_bool
from tow.mirrors import origin_key
from tow.net_guard import is_public
from tow.trackers.generic import validate_tracker_regex
from tow.web import services
from tow.web.text import t


def split_lines(value: str) -> list[str]:
    return [x.strip() for x in (value or "").splitlines() if x.strip()]


def _resolve_addresses(name: str) -> list[str]:
    """Addresses a host name resolves to ([] when it does not resolve)."""

    try:
        return sorted({str(info[4][0]) for info in socket.getaddrinfo(name, None)})
    except OSError, UnicodeError, ValueError:
        return []


def internal_host(hostname: str) -> bool:
    """Loopback, private, link-local, CGNAT/Tailscale and other non-public addresses.

    A name is judged by what it resolves to: ``127.0.0.1.nip.io`` or a rebinding name must not
    turn a server-side fetch into a probe of this PC or the home network. A name that does not
    resolve now (a typo, or a site the provider's DNS blocks) is not internal: every fetch
    checks the DNS answer again and connects only to a public address (``tow.net_guard``), so
    it cannot become a private one after it was saved; ``unresolved_hosts`` names it instead.
    """
    name = hostname.strip("[]").lower()
    if name == "localhost" or name.endswith((".localhost", ".local", ".internal", ".lan", ".home.arpa")):
        return True
    try:
        return not is_public(name)
    except ValueError:
        pass
    for address in _resolve_addresses(name):
        try:
            if not is_public(address):
                return True
        except ValueError:
            continue
    return False


def unresolved_hosts(addresses: list[str]) -> list[str]:
    """Host names among these addresses that do not resolve now: saved, but not reachable yet."""
    names = []
    for address in addresses:
        try:
            name = (urlparse(address).hostname or "").strip("[]").lower()
        except ValueError:
            continue
        if name and not _is_ip(name) and not _resolve_addresses(name):
            names.append(name)
    return list(dict.fromkeys(names))


def _is_ip(name: str) -> bool:
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return False
    return True


def unresolved_note(addresses: list[str]) -> str:
    """The sentence a save appends for addresses that do not resolve ("" when all do)."""
    names = unresolved_hosts(addresses)
    return t("web.site_form.unresolved", hosts=", ".join(names)) if names else ""


def valid_site_hosts(hosts: list[str]) -> list[str]:
    if not hosts:
        raise ValueError(t("web.site_form.mirrors_empty"))
    if len(hosts) > 16:
        raise ValueError(t("web.site_form.mirrors_too_many", limit=16))
    # Tracker hosts are fetched server-side; an internal address would let the site form
    # probe the owner's network. Opt in with allow_private_tracker_hosts: true.
    allow_internal = as_bool(services.load_config().get("allow_private_tracker_hosts"))
    clean = []
    origins = set()
    for host in hosts:
        try:
            parsed = urlparse(host)
            valid = (
                parsed.scheme in {"http", "https"}
                and parsed.hostname is not None
                and parsed.port != 0
                and parsed.username is None
                and parsed.password is None
                and parsed.path in {"", "/"}
                and not parsed.params
                and not parsed.query
                and not parsed.fragment
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError(t("web.site_form.mirror_invalid"))
        if not allow_internal and internal_host(str(parsed.hostname)):
            raise ValueError(t("web.site_form.internal_off"))
        value = host.rstrip("/")
        origin = origin_key(value)
        if origin in origins:
            raise ValueError(t("web.site_form.mirrors_repeat"))
        origins.add(origin)
        clean.append(value)
    return clean


def valid_site_name(value: str) -> str:
    name = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9_]{1,64}", name) or name in {"new", "guess"}:
        raise ValueError(t("web.site_form.name_invalid"))
    return name


def valid_tracker_path(value: str, *, label: str) -> str:
    path = value.strip()
    if not path:
        return ""
    parsed = urlparse(path)
    if (
        not path.startswith("/")
        or path.startswith("//")
        or "\\" in path
        or parsed.scheme
        or parsed.netloc
        or parsed.fragment
    ):
        raise ValueError(t("web.site_form.path_invalid", label=label))
    return path


def validate_tracker_regexes(url_regex: str, download_href_regex: str) -> None:
    for label, value in (("url", url_regex), ("download", download_href_regex)):
        if value:
            # Same rules as at fetch time: compiles, bounded length, no catastrophic backtracking.
            validate_tracker_regex(value, label=label)
