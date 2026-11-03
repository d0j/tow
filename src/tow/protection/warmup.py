"""One bounded continuation of an owned cold start that has not requested a site yet."""

from __future__ import annotations

import re
import time
from collections.abc import Mapping
from typing import Any

from tow.diagnostic_json import epoch
from tow.protection.spec import ProviderUnavailableError, target_origin, validate_settings
from tow.store import load_state, persistence_lock, save_state


def _binding(cfg: Mapping[str, Any], site: str) -> str:
    from tow.protection.store import signature

    return signature(
        {"protection": validate_settings(cfg.get("protection")), "site": (cfg.get("trackers") or {}).get(site)}
    )


def pending(
    state: Mapping[str, Any], cfg: Mapping[str, Any], site: str, *, observed_at: float, now: float
) -> dict[str, Any]:
    """Pure saved proof, never an assertion that the module or site is ready."""
    try:
        return _pending(state, cfg, site, observed_at=observed_at, now=now)
    except AttributeError, KeyError, TypeError, ValueError, OverflowError:
        return {}


def _pending(
    state: Mapping[str, Any], cfg: Mapping[str, Any], site: str, *, observed_at: float, now: float
) -> dict[str, Any]:
    mirrors = state.get("mirrors")
    bucket = mirrors.get(site) if isinstance(mirrors, Mapping) else None
    raw = bucket.get("module_warmup") if isinstance(bucket, Mapping) and not bucket.get("frozen") else None
    if not isinstance(raw, dict) or raw.get("phase") != "pending":
        return {}
    issued, deadline, retry = (epoch(raw.get(key)) for key in ("issued_at", "deadline", "retry_at"))
    if (
        issued is None
        or deadline is None
        or retry is None
        or not issued <= retry < deadline <= issued + 120
        or not issued <= now < deadline
        or observed_at + 1 < issued
        or not isinstance(raw.get("nonce"), str)
        or not re.fullmatch(r"[a-f0-9]{32}", raw["nonce"])
        or raw.get("binding") != _binding(cfg, site)
    ):
        return {}
    settings = validate_settings(cfg.get("protection"))
    provider = next((p for p in settings["providers"] if p["id"] == raw.get("provider") and p["enabled"]), None)
    spec = (cfg.get("trackers") or {}).get(site)
    hosts = spec.get("fetch_hosts") if isinstance(spec, Mapping) else []
    if (
        not settings["enabled"]
        or provider is None
        or provider["kind"] != "flaresolverr"
        or raw.get("origin") not in {target_origin(host) for host in hosts or []}
    ):
        return {}
    return dict(raw)


def note(site: str, origin: str, provider: dict[str, Any], settings: dict[str, Any], error: Exception) -> None:
    """Only the trusted warming response, before any anonymous browser request, grants this."""
    if not (
        isinstance(error, ProviderUnavailableError)
        and error.reason == "busy"
        and error.phase == "startup"
        and isinstance(error.continuation, dict)
    ):
        return
    from tow.config import load_config
    from tow.protection.store import service_states

    now = time.time()
    nonce, deadline = error.continuation.get("nonce"), epoch(error.continuation.get("deadline"))
    if not isinstance(nonce, str) or not re.fullmatch(r"[a-f0-9]{32}", nonce) or deadline is None:
        return
    if not now < deadline <= now + 120:
        return
    retry = epoch(service_states([provider], now=now).get(provider["id"], {}).get("retry_at"))
    if retry is None or not now <= retry < deadline:
        return
    with persistence_lock():
        cfg = load_config()
        if validate_settings(cfg.get("protection")) != settings or site not in (cfg.get("trackers") or {}):
            return
        state = load_state()
        bucket = state.setdefault("mirrors", {}).setdefault(site, {})
        previous = bucket.get("module_warmup")
        if isinstance(previous, dict) and previous.get("nonce") == nonce:
            return  # a repeated answer never renews a consumed proof or its cold-start deadline
        bucket["module_warmup"] = {
            "phase": "pending",
            "nonce": nonce,
            "issued_at": now,
            "deadline": deadline,
            "retry_at": retry,
            "binding": _binding(cfg, site),
            "origin": target_origin(origin),
            "provider": provider["id"],
        }
        save_state(state)


def _settle(site: str, proof: Mapping[str, Any], phase: str) -> bool:
    with persistence_lock():
        state = load_state()
        bucket = (state.get("mirrors") or {}).get(site)
        if not isinstance(bucket, dict) or bucket.get("module_warmup") != proof:
            return False
        bucket["module_warmup"] = {**proof, "phase": phase}
        save_state(state)
        return True


def prepare_batch(state: Mapping[str, Any], cfg: Mapping[str, Any], schedule: Mapping[str, Any]) -> list[str]:
    """Called under the check lock, before loading a run or making any tracker request.

    Persisted batches carry the cold-start nonce. A lost/changed/consumed proof excludes
    their early I/O. Readiness only observes that child; it cannot cold-start another one.
    """
    from tow import site_retries
    from tow.config import load_config
    from tow.protection import local_service, store

    batch = schedule.get("site_retry_batch") or {}
    allowed = site_retries.batch_ids(state, cfg, schedule)
    active = site_retries.policies(state, cfg)
    ordinary = [tid for tid in allowed if not batch[tid].get("module_warmup")]
    groups: dict[tuple[str, str], list[str]] = {}
    for tid in allowed:
        nonce = batch[tid].get("module_warmup")
        if nonce:
            groups.setdefault((active[tid]["site"], nonce), []).append(tid)
    for (site, nonce), ids in groups.items():
        proof = pending(state, cfg, site, observed_at=active[ids[0]]["observed_at"], now=time.time())
        if not proof or proof["nonce"] != nonce or time.time() < proof["retry_at"]:
            continue
        ready = False
        until = time.monotonic() + max(0, proof["deadline"] - time.time())
        while time.time() < proof["deadline"] and time.monotonic() < until:
            current_cfg, current = load_config(), load_state(quarantine=False)
            current_proof = pending(
                current, current_cfg, site, observed_at=active[ids[0]]["observed_at"], now=time.time()
            )
            if current_proof != proof or not set(ids).intersection(
                site_retries.batch_ids(current, current_cfg, schedule)
            ):
                break
            provider = next(
                p for p in validate_settings(current_cfg.get("protection"))["providers"] if p["id"] == proof["provider"]
            )
            settings = validate_settings(current_cfg.get("protection"))
            if not store.recoverable(site, [proof["origin"]], settings, [provider], now=time.time()):
                break  # no override of a real origin lease or a newer provider Retry-After
            left = proof["deadline"] - time.time()
            status = local_service.readiness(provider, nonce, timeout=min(0.75, left))
            if status == "ready":
                ready = _settle(site, proof, "consumed")
                break
            if status == "gone":
                break
            time.sleep(min(2, max(0, proof["deadline"] - time.time())))
        if ready:
            ordinary.extend(ids)
        else:
            _settle(site, proof, "failed")
    return ordinary
