"""Read-only projection of bounded client work the live supervisor will actually run."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any, cast

from tow import file_repair
from tow.check import space, verification
from tow.check.rows import client_marks
from tow.clients import factory
from tow.diagnostic_json import epoch
from tow.records import Topic
from tow.supervisor.jobs import BY_NAME

QUEUE_GRACE_SEC = 60


def _time(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value)
        return epoch(stamp.timestamp()) if stamp.tzinfo is not None else None
    except ValueError, OverflowError, OSError:
        return None


def _job(report: Mapping[str, Any], now: float) -> dict[str, Any]:
    raw = report.get("job")
    if not isinstance(raw, Mapping) or not isinstance(raw.get("name"), str):
        return {}
    spec = BY_NAME.get(raw["name"])
    since, deadline = _time(raw.get("since")), _time(raw.get("deadline"))
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


def _candidate(topic: Mapping[str, Any], cfg: Mapping[str, Any], now: float) -> dict[str, Any]:
    try:
        return _bound_candidate(topic, cfg, now)
    except factory.ClientUnavailableError, AttributeError, KeyError, TypeError, ValueError, OverflowError:
        return {}


def _bound_candidate(topic: Mapping[str, Any], cfg: Mapping[str, Any], now: float) -> dict[str, Any]:
    if topic.get("paused") or topic.get("selection_dirty") or topic.get("move_pending"):
        return {}
    selected = str(topic.get("client_id") or factory.default_client_id(dict(cfg)))
    configuration = factory.client_configuration(dict(cfg), selected)
    if not configuration.get("enabled", True):
        return {}
    repair = file_repair.receipt_of(topic)
    if repair:
        if not file_repair.current(topic, repair, cfg) or not file_repair.continuation_pending(topic, now=now):
            return {}
        waiting, kind = repair, "repair"
        since, deadline = repair["requested_at"], repair["deadline"]
        floor = max(since, float(repair.get("next_at") or since))
        reason = str((repair.get("retry_error") or {}).get("code") or repair.get("blocked") or "")
    else:
        raw_waiting = topic.get("verification_pending") or topic.get("waiting_space")
        if not isinstance(raw_waiting, Mapping) or not raw_waiting:
            return {}
        waiting = dict(raw_waiting)
        kind = "verification" if topic.get("verification_pending") else "space"
        if (
            str(waiting.get("hash") or "").upper() != str(topic.get("hash") or "").upper()
            or not topic.get("hash")
            or waiting.get("client") != selected
            or waiting.get("client_generation") != topic.get("file_observation_revision")
            or waiting.get("repair_only")
        ):
            return {}
        if kind == "verification":
            if (
                waiting.get("owner") != list(client_marks(cast(Topic, topic)))
                or waiting.get("path") != topic.get("save_path")
                or not verification.maintenance_pending(cast(Topic, topic))
                or not isinstance(verification.receipt_of(dict(waiting)), Mapping)
            ):
                return {}
        elif waiting.get("blocked") or not waiting.get("selection"):
            return {}
        since = _time(waiting.get("since"))
        if since is None:
            return {}
        deadline, floor = since + verification.OVERDUE_SEC, since
        reason = str(waiting.get("blocked") or (space.WAITING_SPACE if kind == "space" else ""))
    if not since <= now < deadline or floor >= deadline:
        return {}
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
                "selection_hash",
                "selection_verified",
                "file_observation_revision",
                "repair_request_revision",
                "paused",
            )
        },
        "client": configuration,
        "kind": kind,
        "waiting": waiting,
    }
    encoded = json.dumps(values, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    revision = hashlib.sha256(encoded).hexdigest()
    return {
        "binding": revision,
        "revision": revision,
        "kind": kind,
        "since": since,
        "limit": deadline,
        "floor": floor,
        "reason": reason,
    }


def client_continuation_status(
    state: Mapping[str, Any], cfg: Mapping[str, Any], *, report: Mapping[str, Any], now: float
) -> dict[str, dict[str, Any]]:
    """Project actual finite jobs; a stopped service or stale plan supplies no pending proof."""
    if not report or report.get("stopping") or epoch(now) is None:
        return {}
    job = _job(report, now)
    next_jobs = report.get("next")
    next_jobs = next_jobs if isinstance(next_jobs, Mapping) else {}
    found = {}
    for topic in state.get("topics") or []:
        if not isinstance(topic, Mapping) or not (identity := str(topic.get("id") or "")):
            continue
        item = _candidate(topic, cfg, now)
        if not item:
            continue
        # A maintenance pass may truthfully observe the receipt before its RPC retry floor;
        # the FSM still skips native I/O until next_at, without extending authorization.
        active = job.get("name") in {"space", "progress"} and item["since"] <= job.get("since", 0)
        choices = []
        for name in ("space", "progress"):
            at = _time(next_jobs.get(name))
            spec = BY_NAME[name]
            if at is not None and item["since"] <= at <= now + int(spec.every_sec or 0) + QUEUE_GRACE_SEC:
                choices.append(at)
        if active:
            at, deadline, phase = job["since"], min(job["deadline"], item["limit"]), "running"
        elif choices:
            at = min(choices)
            grace = max(at, job.get("deadline", 0)) + QUEUE_GRACE_SEC
            deadline, phase = min(grace, item["limit"]), "scheduled" if at > now else "queued"
        else:
            continue
        if not now < deadline or at >= item["limit"]:
            continue
        found[identity] = {key: item[key] for key in ("binding", "revision", "kind", "reason")}
        found[identity].update(phase=phase, next_at=at, deadline=deadline)
    return found


def client_continuation_matches(
    topic: Mapping[str, Any], cfg: Mapping[str, Any], entry: Mapping[str, Any], *, now: float
) -> bool:
    """An issue snapshot may quiet only the unchanged, still-bounded native transaction."""
    if not isinstance(entry, Mapping):
        return False
    item = _candidate(topic, cfg, now)
    deadline, next_at = epoch(entry.get("deadline")), epoch(entry.get("next_at"))
    return bool(
        item
        and isinstance(entry.get("phase"), str)
        and entry.get("phase") in {"scheduled", "queued", "running"}
        and entry.get("binding") == item["binding"]
        and entry.get("revision") == item["revision"]
        and deadline is not None
        and next_at is not None
        and next_at <= deadline <= item["limit"]
        and now < deadline
    )
