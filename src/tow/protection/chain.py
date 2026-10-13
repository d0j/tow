"""Optional ordered provider fallback after ordinary tracker mirrors are exhausted."""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlsplit

from tow.log import log_event
from tow.net_guard import public_addresses
from tow.protection import store
from tow.protection.providers import adapters, validate_clearance
from tow.protection.spec import Clearance, target_origin, validate_settings

_LOG = logging.getLogger(__name__)


def record_event(kind: str, site: str, provider: str, status: str) -> None:
    """Optional diagnostics cannot change a confirmed site result or stop fallback."""
    try:
        log_event(kind, tracker=site, provider=provider, status=status)
    except Exception:  # noqa: BLE001 - an optional log write is not tracker access or provider success
        _LOG.debug("optional site-access event not recorded")


def cached(site: str, origin: str, settings: dict[str, Any]) -> Clearance | None:
    """Read-only verified credentials for ordinary HTTP and sign-in, with no DNS or solver."""
    try:
        settings = validate_settings(settings)
        if not settings["enabled"] or not any(p["enabled"] for p in settings["providers"]):
            return None
        return store.read(site, target_origin(origin), settings, now=time.time())
    except Exception:  # noqa: BLE001 - optional unreadable credentials never disable ordinary HTTP
        return None


def candidates(
    site: str, origin: str, settings: dict[str, Any], *, public_only: bool = True, budget_sec: float | None = None
) -> Iterator[Clearance]:
    """Yield credentials only; the caller must validate with its ordinary tracker request.

    This function belongs only to applying challenge recovery, never preview, diagnostics,
    torrent downloads or authentication. Services are externally operated, not installed.
    """
    del public_only  # even a private tracker policy cannot grant a browser service LAN targets
    try:
        settings = validate_settings(settings)
        if not settings["enabled"]:
            return
        budget = settings["timeout_sec"]
        if budget_sec is not None:
            if (
                not isinstance(budget_sec, (int, float))
                or isinstance(budget_sec, bool)
                or not math.isfinite(budget_sec)
            ):
                return
            budget = min(budget, budget_sec)
        if budget < 1:
            return
        deadline = time.monotonic() + budget
        enabled = [item for item in settings["providers"] if item["enabled"]]
        if not enabled:
            return
        origin = target_origin(origin)
        parts = urlsplit(origin)
        assert parts.hostname is not None
        public_addresses(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
        cached = store.read(site, origin, settings, now=time.time())
        if cached is not None:
            yield cached
        if time.monotonic() >= deadline:
            return
        available = adapters()
        enabled = [item for item in enabled if item["kind"] in available][: settings["max_attempts"]]
        if not enabled:
            return
        if not store.reserve(site, origin, settings, now=time.time()):
            return
    except Exception:  # noqa: BLE001 - optional recovery fails closed without exposing credentials
        _LOG.debug("clearance recovery unavailable")
        return
    for index, provider in enumerate(enabled):
        remaining = deadline - time.monotonic()
        timeout = remaining / (len(enabled) - index)
        if timeout < 1:
            return
        try:
            solve = available.get(provider["kind"])
            if solve is None:
                continue
            record_event("protection_attempt", site, provider["id"], "started")
            clearance = solve(provider, origin, timeout)
            if time.monotonic() >= deadline:
                return
            if clearance.provider != provider["id"]:
                continue
            yield validate_clearance(clearance, origin, now=time.time())
        except Exception:  # noqa: BLE001 - one optional provider must never disable the next
            record_event("protection_failed", site, provider["id"], "failed")
            _LOG.debug("clearance provider refused or unavailable")


def accept(site: str, origin: str, settings: dict[str, Any], clearance: Clearance) -> None:
    """Persist only after the caller's normal tracker replay positively verifies access."""
    try:
        settings = validate_settings(settings)
        if not settings["enabled"] or clearance.provider not in {
            p["id"] for p in settings["providers"] if p["enabled"]
        }:
            return
        store.accept(site, target_origin(origin), settings, clearance, now=time.time())
    except Exception:  # noqa: BLE001 - optional credential retention cannot fail a successful check
        _LOG.debug("clearance retention unavailable")


def discard(site: str, origin: str, *, expected: Clearance | None = None) -> None:
    """Refuse this replay's credential, never a newer concurrent verified replacement.

    Automatic callers pass ``expected``. None is an explicit whole-origin/manual removal.
    """
    try:
        store.discard(site, target_origin(origin), expected=expected)
    except Exception:  # noqa: BLE001 - preserve unreadable optional storage rather than replace it
        _LOG.debug("clearance discard unavailable")
