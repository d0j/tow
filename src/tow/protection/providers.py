"""Bounded cookie-only adapters; provider HTML is never tracker content."""

from __future__ import annotations

import ipaddress
import logging
import math
import re
import time
from collections.abc import Callable
from types import ModuleType
from typing import Any
from urllib.parse import urlsplit

import httpcore
import httpx

from tow.http import retry_after_seconds
from tow.plugins import scan
from tow.protection.spec import (
    AccessFailure,
    Clearance,
    ProviderSpec,
    ProviderUnavailableError,
    service_url,
    target_origin,
    validate_probe_path,
)
from tow.protection.transport import DeadlineBackend
from tow.storage.jsonio import decode_json_bytes

_MAX_RESPONSE = 1024 * 1024


def _transport(deadline: float) -> httpx.HTTPTransport:
    # As in tow.net_guard: check the locked pool's contract before installing a backend.
    transport = httpx.HTTPTransport(trust_env=False)
    pool = getattr(transport, "_pool", None)
    if not isinstance(pool, httpcore.ConnectionPool):
        transport.close()
        raise TypeError("clearance transport cannot enforce deadline")
    pool._network_backend = DeadlineBackend(pool._network_backend, deadline)
    return transport


def _text(value: object, maximum: int) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum or any(ord(c) < 32 or ord(c) > 126 for c in value):
        raise ValueError("invalid clearance field")
    return value


def _cookie_scope(domain: object, host: str) -> bool:
    """RFC label-boundary matching; the provider domain never becomes cookie-jar scope.

    No parent-scoped cookie is ever created: only its value is used for this exact
    verified origin. A public-suffix database is unnecessary for that fixed-origin
    contract: even an ancestor attribute cannot authorize reuse on another origin.
    """
    if not isinstance(domain, str) or not domain or len(domain) > 253:
        return False
    value = domain.lower().removeprefix(".")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return value == host
    labels = value.split(".")
    if len(labels) < 2 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels):
        return False
    return value == host or host.endswith("." + value)


def validate_clearance(value: Clearance, origin: str, *, now: float) -> Clearance:
    """Validate persisted and freshly returned credentials through the same contract."""
    target_origin(origin)
    if set(value.cookies) != {"cf_clearance"}:
        raise ValueError("invalid clearance cookie names")
    token = _text(value.cookies["cf_clearance"], 4096)
    if any(c in token for c in ';,"\\ '):
        raise ValueError("invalid clearance cookie value")
    ua = _text(value.user_agent, 1024)
    if not isinstance(value.expires_at, (int, float)) or isinstance(value.expires_at, bool):
        raise TypeError("invalid clearance expiry")
    if not math.isfinite(value.expires_at) or not now < value.expires_at <= now + 86400:
        raise ValueError("invalid clearance expiry")
    provider = _text(value.provider, 48)
    if value.attempt_id is not None and not re.fullmatch(r"[0-9a-f]{32}", value.attempt_id):
        raise ValueError("invalid clearance attempt")
    return Clearance({"cf_clearance": token}, ua, float(value.expires_at), provider, value.attempt_id)


def _solution(raw: object, origin: str, ident: str) -> Clearance:
    if not isinstance(raw, dict) or raw.get("status") != "ok" or not isinstance(raw.get("solution"), dict):
        raise AccessFailure("refused", "service", "clearance service refused")
    solution = raw["solution"]
    url = solution.get("url")
    if not isinstance(url, str):
        raise TypeError("missing clearance origin")
    parsed = urlsplit(url)
    if parsed.username or parsed.password or parsed.fragment or "\\" in url:
        raise ValueError("invalid clearance response origin")
    if target_origin(f"{parsed.scheme}://{parsed.netloc}") != origin:
        raise AccessFailure("origin", "validation", "clearance response changed origin")
    if solution.get("status") != 200:
        raise AccessFailure("status", "validation", "clearance response not successful")
    cookies = solution.get("cookies")
    if not isinstance(cookies, list) or len(cookies) > 128:
        raise ValueError("invalid clearance cookies")
    selected = [cookie for cookie in cookies if isinstance(cookie, dict) and cookie.get("name") == "cf_clearance"]
    if not selected:
        raise AccessFailure("missing_cookie", "validation", "missing clearance cookie")
    if len(selected) != 1:
        raise AccessFailure("invalid_response", "validation", "ambiguous clearance cookie")
    cookie = selected[0]
    host = urlsplit(origin).hostname
    if host is None or not _cookie_scope(cookie.get("domain"), host) or cookie.get("path") != "/":
        raise AccessFailure("scope", "validation", "clearance cookie scope mismatch")
    if cookie.get("secure") is True and not origin.startswith("https://"):
        raise ValueError("secure clearance for an insecure origin")
    now = time.time()
    expiry = cookie.get("expires", cookie.get("expiry", -1))
    if not isinstance(expiry, (int, float)) or isinstance(expiry, bool) or not math.isfinite(expiry):
        raise ValueError("invalid clearance expiry")
    if expiry == -1:
        expiry = now + 3600  # browser session cookies never become permanent credentials
    expiry = min(expiry, now + 86400)
    return validate_clearance(
        Clearance(
            {"cf_clearance": _text(cookie.get("value"), 4096)}, _text(solution.get("userAgent"), 1024), expiry, ident
        ),
        origin,
        now=now,
    )


def _retry_after(value: str | None) -> float:
    """A finite service retry interval, never a reason to slow unrelated tracker HTTP."""
    return retry_after_seconds(value, now=time.time())


def _service_status(response: httpx.Response) -> None:
    if response.status_code in {429, 503}:
        phase = "startup" if response.headers.get("x-tow-module-state") == "warming" else "service"
        raise ProviderUnavailableError(
            "busy", retry_after=_retry_after(response.headers.get("retry-after")), phase=phase
        )
    if response.status_code in {400, 401, 403, 404, 405, 422, 501}:
        raise ProviderUnavailableError("protocol")
    if response.status_code != 200:
        raise AccessFailure("status", "service", "clearance service HTTP failure")


def cookie_api(
    provider: dict[str, Any],
    origin: str,
    timeout: float,
    *,
    media_field: str | None = "disableMedia",
    command: str = "request.get",
) -> Clearance:
    """Bounded cookie-only commands, without login cookies, sessions or proxies."""
    origin = target_origin(origin)
    probe_path = validate_probe_path(provider.get("_probe_path", "/"))
    endpoint = service_url(provider["url"])
    if media_field not in {None, "disableMedia", "blockMedia"} or command not in {"request.get", "request.get_cookies"}:
        raise ValueError("invalid clearance media option")
    if timeout < 1:
        raise AccessFailure("timeout", "service", "clearance time budget exhausted")
    deadline = time.monotonic() + timeout
    from tow.protection.local_service import ensure

    # A module that misses its startup deadline is out of service, like one that cannot start:
    # its backoff pauses it for every site (no site was asked yet), other services still run.
    try:
        locally_managed = ensure(provider, deadline)
    except (TimeoutError, OSError) as exc:
        raise ProviderUnavailableError("unavailable", phase="startup") from exc
    except ValueError as exc:
        raise ProviderUnavailableError("protocol", phase="startup") from exc
    if locally_managed:
        timeout = deadline - time.monotonic()
        if timeout < 1:
            raise ProviderUnavailableError("unavailable", phase="startup")
    # Leave time for the service to serialize and return after its browser timeout.
    # Keep at least 1000 ms: Byparr interprets smaller maxTimeout values as seconds.
    browser_ms = int((timeout - 1 if timeout >= 2 else timeout) * 1000)
    payload = {
        "cmd": command,
        "url": origin + probe_path,
        "maxTimeout": browser_ms,
        "returnOnlyCookies": True,
    }
    if media_field is not None:
        payload[media_field] = True
    # Every socket phase shares a hard deadline; numeric service addresses do not need DNS.
    with (
        httpx.Client(
            trust_env=False, follow_redirects=False, timeout=timeout, transport=_transport(deadline)
        ) as client,
        client.stream("POST", endpoint, json=payload, headers={"Accept": "application/json"}) as response,
    ):
        _service_status(response)
        if response.headers.get("content-encoding", "identity").strip().lower() not in {"", "identity"}:
            raise AccessFailure("invalid_response", "validation", "compressed clearance response refused")
        length = response.headers.get("content-length")
        if length is not None and not 0 <= int(length) <= _MAX_RESPONSE:
            raise AccessFailure("limit", "validation", "clearance response limit")
        raw = bytearray()
        for chunk in response.iter_bytes(chunk_size=16384):
            if time.monotonic() >= deadline:
                raise AccessFailure("timeout", "service", "clearance time budget exhausted")
            if len(raw) + len(chunk) > _MAX_RESPONSE:
                raise AccessFailure("limit", "validation", "clearance response limit")
            raw.extend(chunk)
    if time.monotonic() >= deadline:
        raise AccessFailure("timeout", "service", "clearance time budget exhausted")
    return _solution(decode_json_bytes(bytes(raw)), origin, provider["id"])


# Services explicitly implement their protocol; replacing a provider adds one adapter,
# not another branch in the orchestration chain.
def registry() -> dict[str, ProviderSpec]:
    """Discover independently importable adapters; a broken module is skipped."""
    from tow.protection import solvers

    def load(module: ModuleType) -> ProviderSpec:
        kind = getattr(module, "KIND", None)
        solve = getattr(module, "solve", None)
        if not isinstance(kind, str) or not callable(solve):
            raise TypeError("invalid clearance adapter contract")
        title = getattr(module, "TITLE", kind)
        if not isinstance(title, str) or not title or len(title) > 80:
            raise TypeError("invalid clearance adapter title")
        return ProviderSpec(kind, title, solve)

    found, _ = scan(solvers, helpers=(), load=load, label="clearance provider", logger=logging.getLogger(__name__))
    result: dict[str, ProviderSpec] = {}
    duplicates: set[str] = set()
    for spec in found:
        if spec.kind in result:
            duplicates.add(spec.kind)
        result[spec.kind] = spec
    return {kind: spec for kind, spec in result.items() if kind not in duplicates}


def adapters() -> dict[str, Callable[[dict[str, Any], str, float], Clearance]]:
    return {kind: spec.solve for kind, spec in registry().items()}
