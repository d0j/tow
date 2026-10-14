"""Bounded site-outage retries, independent of owner cadence and client actions."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from tow.diagnostic_json import epoch
from tow.errors import codes_in
from tow.topic_timers import retry_reservation as reservation


def policies(state: Mapping[str, Any], cfg: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Saved evidence only: no requests, writes or automatic retries of owner-action errors."""
    result: dict[str, dict[str, Any]] = {}
    mirrors = state.get("mirrors") or {}
    sites = cfg.get("trackers") or {}
    for topic in state.get("topics") or []:
        if topic.get("paused") or topic.get("once_done") or not topic.get("last_error"):
            continue
        if topic.get("last_error_class") not in {"tracker", "cloudflare"}:
            continue
        params = topic.get("last_error_params") or {}
        site = params.get("tracker")
        spec = sites.get(site) if isinstance(site, str) else None
        if not isinstance(site, str) or not isinstance(spec, Mapping) or not topic.get("id"):
            continue
        bucket = mirrors.get(site) or {}
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
        hosts = spec.get("fetch_hosts") or []
        floors = [
            max(epoch((bucket.get("rate_cool") or {}).get(host)) or 0, epoch((bucket.get("cool") or {}).get(host)) or 0)
            for host in hosts
        ]
        binding = {
            key: topic.get(key)
            for key in (
                "id",
                "url",
                "hash",
                "save_path",
                "client_id",
                "selection",
                "tracking_mode",
                "check_timer_revision",
                "check_interval_min",
                "last_ok_at",
            )
        }
        binding["site_config"] = spec
        binding["protection_config"] = cfg.get("protection")
        revision = hashlib.sha256(json.dumps(binding, sort_keys=True, default=str).encode()).hexdigest()
        # Same-site jitter coalesces related topics, but spreads unrelated sites deterministically.
        jitter = int(hashlib.sha256(site.encode()).hexdigest()[:4], 16) % 31
        result[str(topic["id"])] = {
            "revision": revision,
            "observed_at": observed,
            "delay": delay,
            "floor": min(floors) if floors else 0,
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
