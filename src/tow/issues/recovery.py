"""Pure validation of the automatic continuations supplied with an issue snapshot."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tow import site_retries
from tow.check import client_continuation_matches
from tow.diagnostic_json import epoch
from tow.issues.model import Snapshot


def restored_confirmation(topic: Mapping[str, Any]) -> bool:
    waiting = topic.get("verification_pending")
    return isinstance(waiting, Mapping) and waiting.get("blocked") == "check.restored_confirmation"


def planned(entry: Any, *, now: float) -> bool:
    """An actual, bounded pending/running plan; absent or damaged facts never hide a problem."""
    if not isinstance(entry, Mapping) or entry.get("phase") not in {"scheduled", "queued", "running"}:
        return False
    next_at, deadline = epoch(entry.get("next_at")), epoch(entry.get("deadline"))
    return next_at is not None and deadline is not None and next_at <= deadline and now < deadline


def site_retrying(snapshot: Snapshot) -> frozenset[str]:
    """Match a supplied plan to the current topic, configuration and failed observation."""
    if not snapshot.site_retries:
        return frozenset()
    try:
        current = site_retries.policies(snapshot.state, snapshot.config, now=snapshot.now)
    except AttributeError, KeyError, TypeError, ValueError, OverflowError:
        return frozenset()
    return frozenset(
        identity
        for identity, policy in current.items()
        if planned(entry := snapshot.site_retries.get(identity), now=snapshot.now)
        and entry is not None
        and entry.get("revision") == policy["revision"]
        and entry.get("observed_at") == policy["observed_at"]
    )


def client_retrying(snapshot: Snapshot) -> frozenset[str]:
    """Only a current client transaction with an actual live bounded job is pending."""
    plans = snapshot.client_continuations
    if not isinstance(plans, Mapping):
        return frozenset()
    return frozenset(
        str(topic["id"])
        for topic in snapshot.topics
        if topic.get("id")
        and isinstance(entry := plans.get(str(topic["id"])), Mapping)
        and client_continuation_matches(topic, snapshot.config, entry, now=snapshot.now)
    )


def client_error_retrying(topic: Mapping[str, Any], identities: frozenset[str]) -> bool:
    """Native work cannot resolve or quiet an unrelated tracker/authentication failure."""
    if str(topic.get("id") or "") not in identities:
        return False
    code = topic.get("last_error_code")
    if not isinstance(code, str):
        return False
    if code == "check.client_unreachable":
        return site_retries.client_retry_after(topic) is not None
    if code == "client.managed.http_error":
        params = topic.get("last_error_params")
        status = params.get("code") if isinstance(params, Mapping) else None
        return type(status) is int and (status in {408, 425, 429} or 500 <= status < 600)
    if code in {"check.waiting_space", "check.verifying_files", "check.client_empty"}:
        return True
    waiting = topic.get("verification_pending")
    if isinstance(waiting, Mapping) and code in {"client.managed.error_state", "client.managed.start_unconfirmed"}:
        return waiting.get("blocked") == code and not waiting.get("repair_only")
    repair = topic.get("repair_pending")
    return bool(
        isinstance(repair, Mapping)
        and code
        in {
            "repair.space",
            "repair.volume_unavailable",
            "repair.resume_unconfirmed",
            "repair.dispatch_unconfirmed",
            "client.managed.repair_start_unconfirmed",
        }
        and repair.get("blocked") == code
    )
