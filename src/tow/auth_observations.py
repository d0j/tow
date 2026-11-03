"""Nonsecret observation generations fence a confirmed sign-in from a later refusal."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any
from uuid import uuid4


def revision(value: Any) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"[a-f0-9]{32}", value) else None


def new_revision() -> str:
    return uuid4().hex


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def site_revision(cfg: Mapping[str, Any], name: str) -> str:
    return _digest((cfg.get("trackers") or {}).get(name))


def _legacy(topic: Mapping[str, Any]) -> str:
    # Exact snapshot, not the second-precision clock: a later result cannot inherit it.
    return _digest(dict(topic))


def capture(state: Mapping[str, Any], cfg: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Called before the confirming check releases its check lock; no writes or credentials."""
    from tow.trackers import load_trackers, match_tracker

    trackers = load_trackers(dict(cfg))
    found: dict[str, dict[str, Any]] = {}
    for topic in state.get("topics") or []:
        if not isinstance(topic, Mapping) or not topic.get("id") or not topic.get("last_error"):
            continue
        if topic.get("last_error_class") != "tracker_auth":
            continue
        tracker = match_tracker(trackers, str(topic.get("url") or ""))
        if tracker is None:
            continue
        bucket = found.setdefault(tracker.name, {"site_revision": site_revision(cfg, tracker.name), "topics": {}})
        value = revision(topic.get("auth_error_revision"))
        bucket["topics"][str(topic["id"])] = {"revision": value} if value else {"legacy": _legacy(topic)}
    return found


def accept(state: Mapping[str, Any], cfg: Mapping[str, Any], name: str, captured: Mapping[str, Any]) -> dict[str, Any]:
    """Bind only the captured refusals still current under the persistence lock.

    Old releases had no generation. Assign one only if that entire old observation still
    matches; all new refusals already have a fresh generation even in the same second.
    """
    wanted = captured.get(name)
    if not isinstance(wanted, Mapping) or wanted.get("site_revision") != site_revision(cfg, name):
        return {}
    saved = wanted.get("topics")
    if not isinstance(saved, Mapping):
        return {}
    accepted: dict[str, str] = {}
    for topic in state.get("topics") or []:
        if (
            not isinstance(topic, dict)
            or topic.get("last_error_class") != "tracker_auth"
            or not topic.get("last_error")
        ):
            continue
        tid = str(topic.get("id") or "")
        expected = saved.get(tid)
        if not isinstance(expected, Mapping):
            continue
        current = revision(topic.get("auth_error_revision"))
        if current is not None and expected.get("revision") == current:
            accepted[tid] = current
        elif current is None and expected.get("legacy") == _legacy(topic):
            topic["auth_error_revision"] = new_revision()
            accepted[tid] = topic["auth_error_revision"]
    return {"site_revision": wanted["site_revision"], "topics": accepted}


def confirmed(topic: Mapping[str, Any], bucket: Mapping[str, Any], cfg: Mapping[str, Any], name: str) -> bool:
    saved = bucket.get("signed_in_refusals")
    if not isinstance(saved, Mapping) or saved.get("site_revision") != site_revision(cfg, name):
        return False
    topics = saved.get("topics")
    current = revision(topic.get("auth_error_revision"))
    return isinstance(topics, Mapping) and current is not None and topics.get(str(topic.get("id") or "")) == current
