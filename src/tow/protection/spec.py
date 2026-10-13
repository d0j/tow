"""Pure contracts and fail-closed configuration for optional clearance services."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit


class ProviderUnavailableError(ValueError):
    """An API-level outage, not a site's challenge or a refused clearance replay."""

    def __init__(self, reason: str, *, retry_after: float = 0) -> None:
        if reason not in {"unavailable", "busy", "protocol"}:
            raise ValueError("invalid clearance failure reason")
        super().__init__("clearance API " + reason)
        self.reason = reason
        self.retry_after = retry_after


@dataclass(frozen=True, repr=False)
class Clearance:
    """An untrusted candidate until a normal, read-only tracker replay succeeds."""

    cookies: dict[str, str]
    user_agent: str
    expires_at: float
    provider: str
    attempt_id: str | None = field(default=None, compare=False)


@dataclass(frozen=True, repr=False)
class AttemptProof:
    site: str
    origin: str
    nonce: str


@dataclass(frozen=True)
class ProviderSpec:
    kind: str
    title: str
    solve: Callable[[dict[str, Any], str, float], Clearance]


@dataclass
class RecoveryBudget:
    """One call limit shared by all origins in a recovery phase, not per mirror."""

    remaining: int

    def consume(self) -> bool:
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        return True


def service_url(value: object) -> str:
    """Only explicitly configured local/LAN service literals; never public DNS."""
    if not isinstance(value, str) or any(c in value for c in "\\ %?#") or any(ord(c) < 32 for c in value):
        raise ValueError("invalid clearance service address")
    try:
        parts = urlsplit(value)
        host = parts.hostname
        port = parts.port
        if parts.scheme not in {"http", "https"} or not host or "@" in parts.netloc or parts.netloc.endswith(":"):
            raise ValueError
        if parts.path not in {"", "/", "/v1"}:
            raise ValueError
        if host == "localhost":
            host = "127.0.0.1"
        address = ipaddress.ip_address(host)
        private_ranges = (
            ipaddress.ip_network("10.0.0.0/8"),
            ipaddress.ip_network("172.16.0.0/12"),
            ipaddress.ip_network("192.168.0.0/16"),
            ipaddress.ip_network("fc00::/7"),
        )
        if not (address.is_loopback or any(address in subnet for subnet in private_ranges)):
            raise ValueError
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
        host = f"[{address}]" if address.version == 6 else str(address)
        authority = host if port is None or port == {"http": 80, "https": 443}[parts.scheme] else f"{host}:{port}"
        return urlunsplit((parts.scheme, authority, "/v1", "", ""))
    except ValueError:
        raise ValueError("invalid clearance service address") from None


def target_origin(value: str) -> str:
    """An exact public-site origin, not a topic, download or credential-bearing URL."""
    if not isinstance(value, str) or any(c in value for c in "\\ %?#") or any(ord(c) < 32 for c in value):
        raise ValueError("invalid clearance target")
    try:
        parts = urlsplit(value)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or "@" in parts.netloc
            or parts.netloc.endswith(":")
        ):
            raise ValueError
        if parts.path not in {"", "/"}:
            raise ValueError
        host = parts.hostname.encode("idna").decode("ascii").lower()
        port = parts.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
        authority = f"[{host}]" if ":" in host else host
        if port is not None and port != {"http": 80, "https": 443}[parts.scheme]:
            authority += f":{port}"
        return urlunsplit((parts.scheme, authority, "", "", ""))
    except ValueError, UnicodeError:
        raise ValueError("invalid clearance target") from None


def validate_probe_path(path: object) -> str:
    """Audited anonymous GET pages only, never arbitrary user/topic/download URLs."""
    if not isinstance(path, str) or path not in {"/", "/browse.php"}:
        raise ValueError("invalid public clearance probe")
    return path


def public_probe_path(site: str) -> str:
    """A trusted site module, not a configuration field, chooses the public probe."""
    from tow.trackers.presets import get

    preset = get(site)
    return validate_probe_path(preset.public_probe_path if preset else "/")


def validate_settings(raw: object) -> dict[str, Any]:
    """Unknown keys, coercions and broken providers refuse the complete configuration."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict) or set(raw) - {
        "enabled",
        "timeout_sec",
        "min_interval_sec",
        "max_attempts",
        "providers",
    }:
        raise ValueError("invalid protection settings")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("invalid protection enabled flag")  # noqa: TRY004 - config contract is ValueError
    result: dict[str, Any] = {"enabled": enabled}
    for name, default, low, high in (
        ("timeout_sec", 30, 5, 90),
        ("min_interval_sec", 1800, 60, 86400),
        ("max_attempts", 2, 1, 4),
    ):
        value = raw.get(name, default)
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"invalid protection {name}")
        result[name] = value
    providers = raw.get("providers", [])
    if not isinstance(providers, list) or len(providers) > 16:
        raise ValueError("invalid clearance providers")
    items: list[dict[str, Any]] = []
    ids: set[str] = set()
    endpoints: set[str] = set()
    for entry in providers:
        if not isinstance(entry, dict) or set(entry) - {"id", "kind", "url", "enabled"}:
            raise ValueError("invalid clearance provider")
        ident = entry.get("id")
        kind = entry.get("kind")
        active = entry.get("enabled", False)
        if not isinstance(ident, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,47}", ident) or ident in ids:
            raise ValueError("invalid clearance provider id")
        if (
            not isinstance(kind, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9_]{0,47}", kind)
            or not isinstance(active, bool)
        ):
            raise ValueError("invalid clearance provider kind or enabled flag")
        url = service_url(entry.get("url"))
        if url in endpoints:
            raise ValueError("duplicate clearance service")
        ids.add(ident)
        endpoints.add(url)
        items.append({"id": ident, "kind": kind, "url": url, "enabled": active})
    result["providers"] = items
    return result
