"""Optional ordered provider fallback after ordinary tracker mirrors are exhausted."""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable, Iterator
from dataclasses import replace
from typing import Any
from urllib.parse import urlsplit

import httpcore
import httpx

from tow.log import log_event
from tow.net_guard import public_addresses
from tow.protection import store
from tow.protection.providers import adapters, validate_clearance
from tow.protection.spec import (
    AccessFailure,
    AttemptProof,
    Clearance,
    ProviderUnavailableError,
    RecoveryBudget,
    public_probe_path,
    target_origin,
    validate_settings,
)

_LOG = logging.getLogger(__name__)


def record_event(
    kind: str, site: str, provider: str, status: str, *, reason: str = "", phase: str = "", elapsed_ms: int = 0
) -> None:
    """Optional diagnostics cannot change a confirmed site result or stop fallback."""
    try:
        extra: dict[str, Any] = {}
        if reason in {
            "timeout",
            "unavailable",
            "busy",
            "warming",
            "protocol",
            "refused",
            "origin",
            "status",
            "missing_cookie",
            "scope",
            "invalid_response",
            "limit",
            "provider_mismatch",
            "invalid_module",
            "failed",
            "cloudflare",
            "interactive",
            "rate_limited",
            "quota",
            "blocked",
            "auth",
            "origin_down",
            "transport",
        }:
            extra["reason"] = reason
        if phase in {"startup", "service", "validation", "probe", "resource"}:
            extra["phase"] = phase
        if type(elapsed_ms) is int and 0 <= elapsed_ms <= 120000:
            extra["elapsed_ms"] = elapsed_ms
        log_event(kind, tracker=site, provider=provider, status=status, **extra)
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


def _note_service(provider: dict[str, Any], status: str, attempt: AttemptProof, *, retry_after: float = 0) -> None:
    try:
        store.note_service(provider, status, now=time.time(), retry_after=retry_after, attempt=attempt)
    except Exception:  # noqa: BLE001 - optional telemetry cannot fail a valid result or interrupt fallback
        _LOG.debug("clearance service observation unavailable")


def _failed_service(provider: dict[str, Any], error: Exception, attempt: AttemptProof) -> None:
    if isinstance(error, ProviderUnavailableError):
        _note_service(provider, error.reason, attempt, retry_after=error.retry_after)
    elif isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout, httpcore.ConnectError, httpcore.ConnectTimeout)):
        _note_service(provider, "unavailable", attempt)


def _recovery_budget(configured: float, limit: float | None) -> float | None:
    if limit is not None:
        if not isinstance(limit, (int, float)) or isinstance(limit, bool) or not math.isfinite(limit):
            return None
        configured = min(configured, limit)
    return configured if configured >= 1 else None


def _no_site_request(error: Exception) -> bool:
    """The local module failed to start or answered that it is still warming: refused before
    its browser opened the site, so the call asked the site nothing."""
    return isinstance(error, ProviderUnavailableError) and error.phase == "startup"


def _finish_reservation(pending: store.PendingAttempt, last_called: str | None) -> None:
    """``last_called``: the last provider whose call may have asked the site; None gives the
    origin's reservation back (the provider's own backoff stays)."""
    try:
        if last_called is None:
            store.cancel_unused_attempt(pending)
        else:
            store.finish_attempt(pending.proof, last_called)
    except Exception:  # noqa: BLE001 - unreadable storage retains the safe crash lease
        _LOG.debug("clearance reservation finalization unavailable")


def waiting_origins(site: str, origins: list[str], settings: dict[str, Any]) -> int:
    """How many of ``origins`` a recovery could still call a service for (no recent reservation
    of their own, an installed and enabled service out of its pause): each keeps one call of
    the shared limit (``RecoveryBudget.for_origin``). Read-only; unreadable storage keeps none."""
    try:
        settings = validate_settings(settings)
        if not settings["enabled"]:
            return 0
        available = adapters()
        enabled = [item for item in settings["providers"] if item["enabled"] and item["kind"] in available]
        if not enabled:
            return 0
        targets = [target_origin(origin) for origin in origins]
        return len(store.recoverable(site, targets, settings, enabled, now=time.time()))
    except Exception:  # noqa: BLE001 - optional storage: the current origin keeps the whole remaining limit
        return 0


def candidates(
    site: str,
    origin: str,
    settings: dict[str, Any],
    *,
    public_only: bool = True,
    budget_sec: float | None = None,
    skip_cached: bool = False,
    call_budget: RecoveryBudget | None = None,
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
        if call_budget is None:
            call_budget = RecoveryBudget(settings["max_attempts"])
        if call_budget.remaining <= 0:
            return
        budget = _recovery_budget(settings["timeout_sec"], budget_sec)
        if budget is None:
            return
        deadline = time.monotonic() + budget
        verification_margin = min(10.0, budget / 3)
        enabled = [item for item in settings["providers"] if item["enabled"]]
        if not enabled:
            return
        origin = target_origin(origin)
        probe_path = public_probe_path(site)
        parts = urlsplit(origin)
        assert parts.hostname is not None
        public_addresses(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
        cached = None if skip_cached else store.read(site, origin, settings, now=time.time())
        if cached is not None:
            yield cached
        if time.monotonic() >= deadline:
            return
        available = adapters()
        enabled = [item for item in enabled if item["kind"] in available]
        enabled = store.eligible_providers(site, origin, settings, enabled, now=time.time())[: call_budget.remaining]
        if not enabled:
            return
        if deadline - time.monotonic() - verification_margin < 1:
            return  # no callable solver budget must not start a persistent cooldown
        pending = store.prepare_attempt(site, origin, settings, now=time.time())
        if pending is None:
            return
    except Exception:  # noqa: BLE001 - optional recovery fails closed without exposing credentials
        _LOG.debug("clearance recovery unavailable")
        return
    yield from _run_candidates(
        pending, site, origin, probe_path, enabled, available, deadline, verification_margin, call_budget
    )


def _run_candidates(
    pending: store.PendingAttempt,
    site: str,
    origin: str,
    probe_path: str,
    enabled: list[dict[str, Any]],
    available: dict[str, Callable[[dict[str, Any], str, float], Clearance]],
    deadline: float,
    verification_margin: float,
    call_budget: RecoveryBudget,
) -> Iterator[Clearance]:
    attempt = pending.proof
    # The last provider whose call may have asked the site. While there is none, the origin's
    # reservation is given back: a warming local module does not cost the site its interval.
    last_called = None
    queue = list(enabled)
    retried: set[str] = set()
    try:
        for index, provider in enumerate(queue):
            started = time.monotonic()
            called, before = False, last_called
            try:
                solve = available.get(provider["kind"])
                if solve is None:
                    continue
                available_time = deadline - time.monotonic() - verification_margin
                if available_time < 1:
                    return
                slots = min(call_budget.remaining, len(queue) - index, math.floor(available_time))
                if slots <= 0:
                    return
                timeout = available_time / slots
                provider_deadline = time.monotonic() + timeout
                if not store.note_attempt(attempt, provider["id"]):
                    return
                timeout = min(timeout, provider_deadline - time.monotonic())
                if timeout < 1:
                    continue
                record_event("protection_attempt", site, provider["id"], "started")
                timeout = min(timeout, provider_deadline - time.monotonic())
                if timeout < 1:
                    record_event("protection_failed", site, provider["id"], "failed", reason="timeout", phase="service")
                    continue
                if not call_budget.consume():
                    return
                called, last_called = True, provider["id"]  # a crash or ambiguous response retains the cooldown
                clearance = solve({**provider, "_probe_path": probe_path}, origin, timeout)
                if time.monotonic() >= deadline:
                    record_event("protection_failed", site, provider["id"], "failed", reason="timeout", phase="service")
                    return
                if time.monotonic() >= provider_deadline:
                    record_event("protection_failed", site, provider["id"], "failed", reason="timeout", phase="service")
                    continue  # even a misbehaving optional adapter cannot consume verification's reserved time
                if clearance.provider != provider["id"]:
                    record_event(
                        "protection_failed",
                        site,
                        provider["id"],
                        "failed",
                        reason="provider_mismatch",
                        phase="validation",
                    )
                    continue
                validated = replace(validate_clearance(clearance, origin, now=time.time()), attempt_id=attempt.nonce)
                _note_service(provider, "succeeded", attempt)
                yield validated
            except Exception as error:  # noqa: BLE001 - one optional provider must never disable the next
                _failed_service(provider, error, attempt)
                if called and _no_site_request(error):
                    last_called = before
                reason, phase = failure_details(error)
                record_event(
                    "protection_failed",
                    site,
                    provider["id"],
                    "failed",
                    reason=reason,
                    phase=phase,
                    elapsed_ms=max(0, int((time.monotonic() - started) * 1000)),
                )
                # Some browser responses finish before issuing a clearance. Give other
                # connections priority, then permit one more anonymous call within the
                # original reservation, shared call limit and verification deadline.
                if (
                    isinstance(error, AccessFailure)
                    and error.reason == "missing_cookie"
                    and error.phase == "validation"
                    and provider["id"] not in retried
                    and call_budget.remaining > 0
                    and deadline - time.monotonic() - verification_margin >= 1
                ):
                    retried.add(provider["id"])
                    queue.append(provider)
                _LOG.debug("clearance provider refused or unavailable")
    finally:
        _finish_reservation(pending, last_called)


def failure_details(error: Exception, phase: str = "service") -> tuple[str, str]:
    if isinstance(error, (AccessFailure, ProviderUnavailableError)):
        if error.reason == "protocol" and error.phase == "startup":
            return "invalid_module", "startup"
        reason = "warming" if error.reason == "busy" and error.phase == "startup" else error.reason
        return reason, error.phase
    if isinstance(error, (TimeoutError, httpx.TimeoutException, httpcore.TimeoutException)):
        return "timeout", phase
    if isinstance(error, (httpx.ConnectError, httpcore.ConnectError, ConnectionError)):
        return "unavailable", phase
    if isinstance(error, (ValueError, TypeError)):
        return "invalid_response", "validation"
    return "failed", phase


def accept(site: str, origin: str, settings: dict[str, Any], clearance: Clearance) -> None:
    """Persist only after the caller's normal tracker replay positively verifies access."""
    try:
        settings = validate_settings(settings)
        if not settings["enabled"] or clearance.provider not in {
            p["id"] for p in settings["providers"] if p["enabled"]
        }:
            return
        store.accept(site, target_origin(origin), settings, clearance, now=time.time(), require_attempt=True)
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
