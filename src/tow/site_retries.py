"""Bounded check recovery: site outages and an owner's automatic previous-version replacement."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from tow import auth_observations
from tow.diagnostic_json import epoch
from tow.errors import TowError, codes_in, value_record
from tow.mirrors import cooldown_of, pause_floor
from tow.records import replaces_previous
from tow.topic_timers import retry_reservation as reservation

QUEUE_GRACE_SEC = 60
CLIENT_RETRY_CAUSES = frozenset(
    {
        "check.client_empty",
        "doctor.reason.timeout",
        "doctor.reason.refused",
        "doctor.reason.no_address",
        "doctor.reason.reset",
        "doctor.reason.network",
    }
)
CLIENT_TRANSIENT_HTTP = frozenset({408, 425, 429, 500, 502, 503, 504})


def client_recovery(topic: Mapping[str, Any]) -> bool:
    """Only a typed connection/readiness refusal may retry the existing applying check."""
    if topic.get("last_error_code") != "check.client_unreachable" or any(
        topic.get(key)
        for key in ("selection_dirty", "move_pending", "verification_pending", "repair_pending", "waiting_space")
    ):
        return False
    return client_retry_after(topic) is not None


def client_retry_after(topic: Mapping[str, Any]) -> float | None:
    """A transport/readiness or numeric temporary HTTP refusal; no words grant permission."""
    params = topic.get("last_error_params")
    if CLIENT_RETRY_CAUSES.intersection(codes_in({"params": params})):
        return 0.0
    reason = value_record(params.get("reason")) if isinstance(params, Mapping) else None
    detail = reason.get("params") if isinstance(reason, Mapping) else None
    if (
        not isinstance(reason, Mapping)
        or reason.get("code") != "client.managed.http_error"
        or not isinstance(detail, Mapping)
    ):
        return None
    status = detail.get("code")
    if type(status) is not int or status not in CLIENT_TRANSIENT_HTTP:
        return None
    retry = epoch(detail.get("retry_after", 0))
    return retry if retry is not None and retry <= 86400 else None


def _client_site(topic: Mapping[str, Any], cfg: Mapping[str, Any]) -> str | None:
    if not client_recovery(topic):
        return None
    from tow.clients import factory
    from tow.trackers import load_trackers, match_tracker

    selected = str(topic.get("client_id") or factory.default_client_id(dict(cfg)))
    if not any(
        str(row["id"]) == selected and row.get("enabled", True) for row in factory.client_configurations(dict(cfg))
    ):
        return None
    tracker = match_tracker(load_trackers(dict(cfg)), str(topic.get("url") or ""))
    return tracker.name if tracker is not None else None


def _reported_time(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value)
        return epoch(stamp.timestamp()) if stamp.tzinfo is not None else None
    except ValueError, OverflowError, OSError:
        return None


def _active_job(report: Mapping[str, Any], now: float) -> dict[str, Any]:
    from tow.supervisor.jobs import BY_NAME

    raw = report.get("job")
    if not isinstance(raw, Mapping) or not isinstance(raw.get("name"), str):
        return {}
    spec = BY_NAME.get(raw["name"])
    since, deadline = _reported_time(raw.get("since")), _reported_time(raw.get("deadline"))
    if (
        spec is None
        or spec.timeout_sec is None
        or type(raw.get("pid")) is not int
        or raw["pid"] <= 0
        or since is None
        or deadline is None
        or not since <= now < deadline
        or abs(deadline - since - spec.timeout_sec) > 1
    ):
        return {}
    return {"name": spec.name, "since": since, "deadline": deadline}


def auth_revision(topic: Mapping[str, Any], cfg: Mapping[str, Any], site: str, signed_in_at: float) -> str:
    """Bind a post-sign-in observation to the owner settings its ordinary check will use."""
    values = {
        "topic": {
            key: topic.get(key)
            for key in (
                "id",
                "url",
                "hash",
                "save_path",
                "client_id",
                "selection",
                "selection_dirty",
                "last_check",
                "auth_error_revision",
                "check_timer_revision",
                "check_interval_min",
                "file_observation_revision",
            )
        },
        "site": (cfg.get("trackers") or {}).get(site),
        "signed_in_at": signed_in_at,
        "interval_sec": cfg.get("interval_sec"),
    }
    return hashlib.sha256(json.dumps(values, sort_keys=True, default=str).encode()).hexdigest()


def _queue_deadline(at: float, now: float, job: Mapping[str, Any]) -> float | None:
    deadline = at + QUEUE_GRACE_SEC
    if at < now and job:
        deadline = max(deadline, job["deadline"] + QUEUE_GRACE_SEC)
    return deadline if now < deadline else None


def _ordinary_after_auth(
    topic: Mapping[str, Any],
    cfg: Mapping[str, Any],
    report: Mapping[str, Any],
    schedule: Mapping[str, Any],
    job: Mapping[str, Any],
    signed: float,
    now: float,
) -> tuple[str, float, float] | None:
    from tow.topic_timers import policy, reserved_at

    timer = policy(topic)
    if timer is not None:
        batch = schedule.get("timer_batch")
        saved = batch.get(str(topic.get("id"))) if isinstance(batch, Mapping) else None
        if job.get("name") == "timer" and signed <= job["since"] and abs(reserved_at(timer, saved) - job["since"]) <= 1:
            return "running", job["since"], job["deadline"]
        plans = report.get("topic_timers")
        entry = plans.get(str(topic.get("id"))) if isinstance(plans, Mapping) else None
        if not isinstance(entry, Mapping) or entry.get("revision") != timer["revision"]:
            return None
        at, interval = _reported_time(entry.get("at")), timer["minutes"] * 60
    else:
        if job.get("name") == "check" and signed <= job["since"]:
            return "running", job["since"], job["deadline"]
        plans = report.get("next")
        at = _reported_time(plans.get("check")) if isinstance(plans, Mapping) else None
        interval = cfg.get("interval_sec", 43200)
        if type(interval) is not int or interval <= 0:
            return None
    if at is None or not signed <= at <= now + max(120, interval) + QUEUE_GRACE_SEC:
        return None
    deadline = _queue_deadline(at, now, job)
    return ("scheduled" if at > now else "queued", at, deadline) if deadline is not None else None


def _auth_status(
    state: Mapping[str, Any],
    cfg: Mapping[str, Any],
    report: Mapping[str, Any],
    schedule: Mapping[str, Any],
    job: Mapping[str, Any],
    now: float,
) -> dict[str, dict[str, Any]]:
    from tow.trackers import load_trackers, match_tracker

    candidates = [
        topic
        for topic in state.get("topics") or []
        if isinstance(topic, Mapping)
        and topic.get("id")
        and not (topic.get("paused") or topic.get("once_done") or topic.get("selection_dirty"))
        and topic.get("last_error")
        and topic.get("last_error_class") == "tracker_auth"
    ]
    if not candidates:
        return {}
    try:
        trackers = load_trackers(dict(cfg))
    except AttributeError, KeyError, TypeError, ValueError:
        return {}  # bad optional auth evidence never hides another site's valid retry
    mirrors = state.get("mirrors")
    mirrors = mirrors if isinstance(mirrors, Mapping) else {}
    found = {}
    for topic in candidates:
        tracker = match_tracker(trackers, str(topic.get("url") or ""))
        bucket = mirrors.get(tracker.name) if tracker is not None else None
        if not isinstance(bucket, Mapping) or bucket.get("frozen"):
            continue
        signed, observed = epoch(bucket.get("signed_in_at")), _reported_time(topic.get("last_check"))
        if (
            signed is None
            or observed is None
            or signed > now
            or tracker is None
            or not auth_observations.confirmed(topic, bucket, cfg, tracker.name)
        ):
            continue
        try:
            ordinary = _ordinary_after_auth(topic, cfg, report, schedule, job, signed, now)
        except AttributeError, KeyError, TypeError, ValueError, OverflowError, TowError:
            continue
        if ordinary is not None:
            phase, at, deadline = ordinary
            found[str(topic["id"])] = {
                "phase": phase,
                "revision": auth_revision(topic, cfg, tracker.name, signed),
                "observed_at": observed,
                "next_at": at,
                "deadline": deadline,
                "attempt": 0,
                "reason": "auth_confirmation",
                "site": tracker.name,
                "signed_in_at": signed,
            }
    return found


def recovery_status(
    state: Mapping[str, Any],
    cfg: Mapping[str, Any],
    *,
    report: Mapping[str, Any],
    schedule: Mapping[str, Any],
    now: float,
) -> dict[str, dict[str, Any]]:
    """Project a live supervisor's bounded continuation without planning or dispatching.

    The caller supplies the report only while the supervisor owns its lock. Its ``at``
    is deliberately not a heartbeat: status is written only when the plan changes.
    """
    from tow.topic_timers import retry_deadline

    if not report or report.get("stopping") or epoch(now) is None:
        return {}
    active = policies(state, cfg, now=now)
    planned = report.get("site_retries")
    planned = planned if isinstance(planned, Mapping) else {}
    attempts = schedule.get("site_retry_attempts")
    attempts = attempts if isinstance(attempts, Mapping) else {}
    batch = schedule.get("site_retry_batch")
    batch = batch if isinstance(batch, Mapping) else {}
    job = _active_job(report, now)
    found = {}
    for tid, item in active.items():
        current = reservation(item, batch.get(tid))
        if (
            job.get("name") == "site_retry"
            and current
            and current.get("observed_at") == item["observed_at"]
            and abs(current["started_at"] - job["since"]) <= 1
            and item["floor"] <= current["started_at"]
            and reservation(item, attempts.get(tid)) == current
        ):
            found[tid] = {
                "phase": "running",
                "revision": item["revision"],
                "observed_at": item["observed_at"],
                "next_at": current["started_at"],
                "deadline": job["deadline"],
                "attempt": current["count"],
                "reason": item["reason"],
            }
            continue
        entry = planned.get(tid)
        if not isinstance(entry, Mapping) or entry.get("revision") != item["revision"]:
            continue
        if type(entry.get("observed_at")) not in (int, float) or entry["observed_at"] != item["observed_at"]:
            continue
        previous = reservation(item, attempts.get(tid))
        if (
            retry_deadline(item, attempts.get(tid), anchor=max(item["observed_at"], previous.get("started_at", 0)))
            is None
        ):
            continue  # damaged or exhausted records never invent another attempt
        at = _reported_time(entry.get("at"))
        if at is None or not item["floor"] <= at <= max(now, item["floor"]) + max(7200, item["delay"]) + 30:
            continue
        deadline = _queue_deadline(at, now, job)
        if deadline is None:
            continue
        found[tid] = {
            "phase": "scheduled" if at > now else "queued",
            "revision": item["revision"],
            "observed_at": item["observed_at"],
            "next_at": at,
            "deadline": deadline,
            "attempt": previous.get("count", 0) + 1,
            "reason": item["reason"],
        }
    found.update(_auth_status(state, cfg, report, schedule, job, now))
    return found


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
        and (topic.get("last_error_class") in {"tracker", "cloudflare"} or client_recovery(topic))
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
        client_site = _client_site(topic, cfg)
        if not (replacement_site or client_site) and topic.get("last_error_class") not in {"tracker", "cloudflare"}:
            continue
        params = topic.get("last_error_params")
        site = replacement_site or client_site or (params.get("tracker") if isinstance(params, Mapping) else None)
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
        if client_site:
            floor = max(floor, observed + (client_retry_after(topic) or 0))
        warmup = {}
        if cloudflare:
            from tow.protection.warmup import pending

            warmup = pending(state, cfg, site, observed_at=observed, now=clock)
            if warmup and max(floor, warmup["retry_at"]) >= warmup["deadline"]:
                warmup = {}  # native readiness never shortens a real origin/server pause
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
            "reason": "client_recovery" if client_site else "site_retry",
        }
        if warmup:
            result[str(topic["id"])]["module_warmup"] = warmup["nonce"]
            result[str(topic["id"])]["warmup_at"] = max(floor, warmup["retry_at"])
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
