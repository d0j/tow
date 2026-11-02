"""Bounded check recovery: site outages and an owner's automatic previous-version replacement."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from tow.diagnostic_json import epoch
from tow.errors import codes_in
from tow.mirrors import cooldown_of, pause_floor
from tow.records import replaces_previous
from tow.topic_timers import retry_reservation as reservation


def _replacement_hash(topic: Mapping[str, Any]) -> str:
    params = topic.get("last_error_params")
    value = (
        params.get("hash")
        if topic.get("last_error_code") == "check.previous_revision_active" and isinstance(params, Mapping)
        else topic.get("replacement_pending_hash")
    )
    return (
        value.upper() if isinstance(value, str) and re.fullmatch(r"[a-fA-F0-9]{40}(?:[a-fA-F0-9]{24})?", value) else ""
    )


def _replacement_site(topic: Mapping[str, Any], cfg: Mapping[str, Any]) -> str | None:
    """An authorized replacement still waiting; site matching is read-only, never a request."""
    if not replaces_previous(topic, cfg.get("replace_previous_revision") is True):
        return None
    if not _replacement_hash(topic):
        return None
    blocked = topic.get("last_error_code") == "check.previous_revision_active"
    if not blocked and not (
        topic.get("replacement_pending_hash")
        and (
            topic.get("last_error_class") in {"tracker", "cloudflare"}
            or topic.get("last_error_code") == "check.client_unreachable"
        )
    ):
        return None
    from tow.trackers import load_trackers, match_tracker

    tracker = match_tracker(load_trackers(dict(cfg)), str(topic.get("url") or ""))
    return tracker.name if tracker is not None else None


def policies(
    state: Mapping[str, Any], cfg: Mapping[str, Any], *, now: float | None = None
) -> dict[str, dict[str, Any]]:
    """Saved evidence only: no requests, writes or automatic retries of owner-action errors."""
    clock = time.time() if now is None else now
    result: dict[str, dict[str, Any]] = {}
    # Saved values are read defensively: one damaged record leaves its topic out, never the others.
    saved_mirrors, saved_sites = state.get("mirrors"), cfg.get("trackers")
    mirrors: Mapping[str, Any] = saved_mirrors if isinstance(saved_mirrors, Mapping) else {}
    sites: Mapping[str, Any] = saved_sites if isinstance(saved_sites, Mapping) else {}
    for topic in state.get("topics") or []:
        if topic.get("paused") or topic.get("once_done") or not topic.get("last_error"):
            continue
        replacement_site = _replacement_site(topic, cfg)
        if not replacement_site and topic.get("last_error_class") not in {"tracker", "cloudflare"}:
            continue
        params = topic.get("last_error_params")
        site = replacement_site or (params.get("tracker") if isinstance(params, Mapping) else None)
        spec = sites.get(site) if isinstance(site, str) else None
        if not isinstance(site, str) or not isinstance(spec, Mapping) or not topic.get("id"):
            continue
        saved_bucket = mirrors.get(site)
        bucket: Mapping[str, Any] = saved_bucket if isinstance(saved_bucket, Mapping) else {}
        if bucket.get("frozen"):
            continue
        try:
            observed = epoch(datetime.fromisoformat(topic["last_check"]).timestamp())
        except KeyError, TypeError, ValueError, OverflowError, OSError:
            continue
        if observed is None:
            continue
        record = {"code": topic.get("last_error_code"), "params": params}
        kinds = set(codes_in(record))
        # An explicit ban is transport-classed, but never authorizes unattended retries.
        if kinds & {"mirrors.blocked", "mirrors.tracker_daily_limit", "mirrors.http_auth"}:
            continue
        cloudflare = "mirrors.cloudflare" in kinds or topic.get("last_error_class") == "cloudflare"
        delay = max(1800, int((cfg.get("protection") or {}).get("min_interval_sec", 1800))) if cloudflare else 300
        # The deadlines as a check reads them: clamped after a clock jump, hosts without a "/".
        floor = pause_floor(bucket, list(spec.get("fetch_hosts") or []), now=clock, cooldown_sec=cooldown_of(spec))
        binding = {
            key: topic.get(key)
            for key in (
                "id",
                "url",
                "hash",
                "save_path",
                "client_id",
                "selection",
                "selection_dirty",
                "tracking_mode",
                "check_timer_revision",
                "check_interval_min",
                "last_ok_at",
            )
        }
        binding["site_config"] = spec
        binding["protection_config"] = cfg.get("protection")
        if replacement_site:
            binding["replacement"] = {
                "hash": _replacement_hash(topic),
                "enabled": True,
            }
        revision = hashlib.sha256(json.dumps(binding, sort_keys=True, default=str).encode()).hexdigest()
        # Same-site jitter coalesces related topics, but spreads unrelated sites deterministically.
        jitter = int(hashlib.sha256(site.encode()).hexdigest()[:4], 16) % 31
        result[str(topic["id"])] = {
            "revision": revision,
            "observed_at": observed,
            "delay": delay,
            "floor": floor,
            "jitter": jitter,
            "site": site,
        }
    delays: dict[str, int] = {}
    for item in result.values():
        delays[item["site"]] = max(delays.get(item["site"], 0), item["delay"])
    for item in result.values():
        item["delay"] = delays[item["site"]]
    return result


def batch_ids(state: Mapping[str, Any], cfg: Mapping[str, Any], schedule: Mapping[str, Any]) -> list[str]:
    """Revalidate after the check lock: deleted/paused/edited/recovered topics never run."""
    batch = schedule.get("site_retry_batch")
    if not isinstance(batch, dict):
        return []
    return [
        tid
        for tid, item in policies(state, cfg).items()
        if (saved := reservation(item, batch.get(tid)))
        and saved.get("observed_at") == item["observed_at"]
        and item["floor"] <= saved["started_at"]
    ]
