"""One explicit, revision-bound request to recover selected files through their client."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import time
from collections.abc import Mapping
from typing import Any, cast
from uuid import uuid4

from tow import file_presence, folders
from tow.clients import factory, files
from tow.clients.spec import TorrentClientAdapter
from tow.clients.state import matches_hash
from tow.config import load_config
from tow.errors import TowError
from tow.jsonish import as_dict
from tow.log import log_event
from tow.progress.confirmation import filesystem_confirmation, safe_relative_path
from tow.progress.expected import selected_native_indices
from tow.records import Topic
from tow.store import check_run_lock, load_download_history, load_secrets, load_state, persistence_lock, save_state

TIMEOUT_SEC = 12 * 3600
MAX_ATTEMPTS = 3
ACTIVE_PHASES = frozenset({"requested", "checking", "resuming", "downloading"})
_ID = re.compile(r"[0-9a-f]{32}")
_DIGEST = re.compile(r"[0-9a-f]{64}")


def binding(topic: Mapping[str, Any], client_id: str) -> str:
    values = [file_presence.binding(topic, client_id), topic.get("url"), topic.get("paused")]
    return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def receipt_of(topic: Mapping[str, Any]) -> dict[str, Any]:
    value = topic.get("repair_pending")
    if not isinstance(value, dict):
        return {}
    if (
        value.get("version") != 1
        or type(value.get("version")) is not int
        or not isinstance(value.get("id"), str)
        or not _ID.fullmatch(value["id"])
        or not isinstance(value.get("phase"), str)
        or value["phase"] not in ACTIVE_PHASES | {"blocked"}
        or value.get("resume_authorized") is not True
        or not all(isinstance(value.get(key), str) and value[key] for key in ("hash", "client", "path", "volume"))
        or not all(
            isinstance(value.get(key), str) and _DIGEST.fullmatch(value[key])
            for key in ("binding", "files", "before_progress")
        )
        or type(value.get("count")) is not int
        or value["count"] <= 0
        or type(value.get("attempts")) is not int
        or not 0 <= value["attempts"] <= MAX_ATTEMPTS
    ):
        return {}
    for key in ("requested_at", "updated_at", "deadline", "next_at"):
        stamp = value.get(key)
        if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not math.isfinite(stamp) or stamp < 0:
            return {}
    if not 0 < value["deadline"] - value["requested_at"] <= TIMEOUT_SEC:
        return {}
    if value["phase"] == "blocked" and (not isinstance(value.get("blocked"), str) or not value["blocked"]):
        return {}
    if "blocked_params" in value and not isinstance(value["blocked_params"], dict):
        return {}
    for key in ("stop_dispatched", "recheck_dispatched", "resume_dispatched", "saw_checking", "running_seen"):
        if key in value and type(value[key]) is not bool:
            return {}
    for key in ("stop_at", "check_at", "resume_at"):
        stamp = value.get(key)
        if key in value and (
            isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not math.isfinite(stamp) or stamp < 0
        ):
            return {}
    if value["phase"] == "checking" and (value.get("recheck_dispatched") is not True or "check_at" not in value):
        return {}
    needs_start = value["phase"] == "resuming" or (
        value["phase"] == "downloading" and value.get("running_existing") is not True
    )
    if needs_start and (value.get("resume_dispatched") is not True or "resume_at" not in value):
        return {}
    if value["phase"] == "requested" and (value.get("recheck_dispatched") or value.get("resume_dispatched")):
        return {}
    return copy.deepcopy(value)


def current(topic: Mapping[str, Any], receipt: Mapping[str, Any], config: Mapping[str, Any] | None = None) -> bool:
    client_id = str(receipt.get("client") or "")
    selected = str(
        topic.get("client_id") or (factory.default_client_id(dict(config)) if config is not None else client_id)
    )
    return bool(
        selected == client_id
        and topic.get("repair_request_revision") == receipt.get("id")
        and receipt.get("binding") == binding(topic, client_id)
        and not topic.get("paused")
        and not topic.get("selection_dirty")
    )


def active(topic: Mapping[str, Any], *, now: float | None = None) -> bool:
    receipt = receipt_of(topic)
    stamp = time.time() if now is None else now
    return bool(
        receipt
        and receipt["phase"] in ACTIVE_PHASES
        and not receipt.get("blocked")
        and receipt["requested_at"] <= stamp < receipt["deadline"]
        and current(topic, receipt)
    )


def start_confirmation_pending(topic: Mapping[str, Any], *, now: float | None = None) -> bool:
    """A legacy false failure may observe its already dispatched start, never repeat it.

    This schedules read-only evidence, not an active repair or fresh authorization. Its
    original deadline and context remain authoritative, including cancellation and pause.
    """
    receipt = receipt_of(topic)
    stamp = time.time() if now is None else now
    return bool(
        receipt
        and receipt["phase"] == "blocked"
        and receipt.get("blocked") == "client.managed.repair_start_unconfirmed"
        and receipt.get("resume_dispatched") is True
        and receipt.get("running_seen") is not True
        and "resume_at" in receipt
        and receipt["requested_at"] <= receipt["resume_at"] <= stamp < receipt["deadline"]
        and current(topic, receipt)
    )


def waiting_topics(state: Mapping[str, Any]) -> list[Topic]:
    return [
        cast(Topic, topic)
        for topic in state.get("topics") or []
        if isinstance(topic, dict)
        and (receipt := receipt_of(topic))
        and ((receipt["phase"] in ACTIVE_PHASES and not receipt.get("blocked")) or start_confirmation_pending(topic))
        and not topic.get("paused")
        and not topic.get("selection_dirty")
    ]


def progress_signature(info: Mapping[str, Any]) -> str:
    rows = []
    for row in info.get("files") or []:
        progress = row.get("progress")
        if (
            isinstance(progress, bool)
            or not isinstance(progress, (int, float))
            or not math.isfinite(progress)
            or not 0 <= progress <= 1
        ):
            raise TowError("repair.unavailable")
        rows.append((row.get("index"), float(progress) if progress else 0.0))
    return hashlib.sha256(json.dumps(sorted(rows), separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def validate_info(topic: Mapping[str, Any], receipt: Mapping[str, Any], info: Mapping[str, Any] | None) -> None:
    if not info or not matches_hash(info, str(receipt["hash"])):
        raise TowError("repair.changed")
    if "tow" not in {str(tag).strip().casefold() for tag in info.get("tags") or []}:
        raise TowError("repair.not_owned")
    if not folders.paths_equal(str(info.get("save_path") or ""), str(receipt["path"])):
        raise TowError("repair.changed")
    try:
        signature = files.recovery_fingerprint(list(info.get("files") or []))
    except ValueError as error:
        raise TowError("repair.selection_changed") from error
    if signature != receipt["files"]:
        raise TowError("repair.selection_changed")
    if topic.get("move_pending") or topic.get("verification_pending") or topic.get("selection_dirty"):
        raise TowError("repair.client_busy")


def evidence(info: Mapping[str, Any], receipt: Mapping[str, Any]) -> dict[str, int]:
    path, volume = str(receipt["path"]), str(receipt["volume"])
    if not folders.seen_from_here(path, volume=volume):
        raise TowError("repair.volume_unavailable")
    counts = {
        "absent": 0,
        "unknown": 0,
        "incomplete": 0,
        "selected": 0,
        "remaining": 0,
        "stale_claims": 0,
        "partial_claims": 0,
    }
    for row in info.get("files") or []:
        if row["priority"] <= 0 or files.padding_like(row["name"]):
            continue
        counts["selected"] += 1
        rel = safe_relative_path(path, row["name"])
        if rel is None:
            raise TowError("repair.selection_changed")
        present = filesystem_confirmation(path, rel, row["size"], seen=True)
        counts["absent"] += present is False
        counts["unknown"] += present is None
        fraction = row.get("progress")
        if (
            isinstance(fraction, bool)
            or not isinstance(fraction, (int, float))
            or not math.isfinite(fraction)
            or not 0 <= fraction <= 1
        ):
            raise TowError("repair.unavailable")
        counts["incomplete"] += fraction < 1
        counts["stale_claims"] += present is False and fraction > 0
        counts["partial_claims"] += 0 < fraction < 1
        counts["remaining"] += row["size"] if present is False else math.ceil(row["size"] * (1 - fraction))
    if not counts["selected"]:
        raise TowError("repair.selection_changed")
    return counts


def prepare(topic: Topic, client: TorrentClientAdapter, client_id: str, *, now: float, volume: str) -> dict[str, Any]:
    if topic.get("paused"):
        raise TowError("repair.paused")
    if not math.isfinite(now) or now < 0:
        raise TowError("repair.unavailable")
    if (
        topic.get("selection_verified") is not True
        or str(topic.get("selection_hash") or "").casefold() != str(topic.get("hash") or "").casefold()
    ):
        raise TowError("repair.selection_changed")
    if not (client.capabilities or {}).get("recheck_files") or not callable(
        getattr(client, "resume_repaired_torrent", None)
    ):
        raise TowError("repair.unsupported")
    if (
        topic.get("selection_dirty")
        or topic.get("move_pending")
        or topic.get("verification_pending")
        or topic.get("waiting_space")
    ):
        raise TowError("repair.client_busy")
    negative = as_dict(topic.get("files_missing"))
    count = negative.get("count")
    if negative.get("binding") != file_presence.binding(topic, client_id) or type(count) is not int or count <= 0:
        raise TowError("repair.no_missing")
    info = client.inspect_torrent(str(topic.get("hash") or ""))
    try:
        rows = list((info or {}).get("files") or [])
        signature = files.recovery_fingerprint(rows)
        expected = selected_native_indices(
            topic,
            rows,
            content_path=str((info or {}).get("content_path") or ""),
            save_path=str(topic.get("save_path") or ""),
        )
        wanted = {row["index"] for row in rows if row["priority"] > 0 and not files.padding_like(row["name"])}
        if wanted != expected:
            raise ValueError("native wanted files differ from the saved selection")
    except (TowError, ValueError, TypeError, KeyError) as error:
        raise TowError("repair.selection_changed") from error
    receipt = {
        "version": 1,
        "id": uuid4().hex,
        "binding": binding(topic, client_id),
        "hash": str(topic.get("hash") or ""),
        "client": client_id,
        "path": str(topic.get("save_path") or ""),
        "files": signature,
        "volume": volume,
        "phase": "requested",
        "requested_at": now,
        "updated_at": now,
        "deadline": now + TIMEOUT_SEC,
        "attempts": 0,
        "next_at": now,
        "resume_authorized": True,
        "count": count,
        "before_progress": progress_signature(info or {}),
    }
    validate_info(topic, receipt, info)
    if str((info or {}).get("state") or "").casefold().startswith("checking") or (info or {}).get("force_start"):
        raise TowError("repair.client_busy")
    present = evidence(info or {}, receipt)
    if present["unknown"]:
        raise TowError("repair.volume_unavailable")
    if not present["absent"]:
        raise TowError("repair.no_missing")
    receipt["check_required"] = bool(present["stale_claims"] or present["partial_claims"])
    return receipt


def _topic(state: Mapping[str, Any], topic_id: str) -> Topic | None:
    return cast(
        Topic | None,
        next(
            (
                topic
                for topic in state.get("topics") or []
                if isinstance(topic, dict) and str(topic.get("id")) == topic_id
            ),
            None,
        ),
    )


def request(topic_id: str) -> dict[str, Any]:
    """Persist explicit intent, without giving any client mutation command."""
    with check_run_lock(wait=False):
        config, secrets = load_config(), load_secrets()
        with persistence_lock():
            state = load_state(quarantine=False)
            topic = _topic(state, topic_id)
            history = load_download_history(quarantine=False)
        if topic is None:
            raise TowError("repair.unavailable")
        existing = receipt_of(topic)
        if active(topic) and current(topic, existing, config):
            return existing
        client_id = str(topic.get("client_id") or factory.default_client_id(config))
        if not factory.on_this_computer(config, secrets, client_id):
            raise TowError("repair.volume_unavailable")
        client = factory.from_secrets(config, secrets, client_id)
        record = as_dict(as_dict(history.get("topics")).get(topic_id))
        volume = folders.recorded_volume(record.get("volume"), str(topic.get("save_path") or ""))
        if not volume:
            raise TowError("repair.volume_unavailable")
        receipt = prepare(topic, client, client_id, now=time.time(), volume=volume)
        with persistence_lock():
            disk = load_state(quarantine=False)
            current_topic = _topic(disk, topic_id)
            current_config = load_config()
            if (
                current_topic is None
                or binding(current_topic, client_id) != receipt["binding"]
                or str(current_topic.get("client_id") or factory.default_client_id(current_config)) != client_id
            ):
                raise TowError("repair.changed")
            current_topic["repair_request_revision"] = receipt["id"]
            current_topic["repair_pending"] = receipt
            save_state(disk)
        log_event(
            "files_repair_requested",
            topic=topic_id,
            topic_id=topic_id,
            title=topic.get("title"),
            hash=receipt["hash"],
            client_id=client_id,
            n=receipt["count"],
            operation_id=f"repair-{receipt['id']}",
        )
        return copy.deepcopy(receipt)


def cancel(topic_id: str, *, expected_id: str | None = None) -> bool:
    """Withdraw future automatic steps; an already requested native operation may finish."""
    with persistence_lock():
        state = load_state(quarantine=False)
        topic = _topic(state, topic_id)
        if topic is None:
            return False
        raw = as_dict(topic.get("repair_pending"))
        if expected_id is not None and (
            raw.get("id") != expected_id or topic.get("repair_request_revision") != expected_id
        ):
            return False
        topic.pop("repair_pending", None)
        topic["repair_request_revision"] = uuid4().hex
        save_state(state)
        if raw:
            log_event(
                "files_repair_cancelled",
                topic=topic_id,
                topic_id=topic_id,
                title=topic.get("title"),
                hash=topic.get("hash"),
                client_id=raw.get("client"),
                operation_id=f"repair-{raw.get('id', '')}",
            )
        return True


def persist(topic: Topic, receipt: Mapping[str, Any] | None, *, expected_id: str) -> bool:
    """CAS the receipt before dispatch, preserving all concurrent owner changes."""
    with persistence_lock():
        state, config = load_state(quarantine=False), load_config()
        disk_topic = _topic(state, str(topic.get("id") or ""))
        old = receipt_of(disk_topic or {})
        if disk_topic is None or old.get("id") != expected_id or not current(disk_topic, old, config):
            return False
        if receipt is None:
            disk_topic.pop("repair_pending", None)
        else:
            candidate = copy.deepcopy(dict(receipt))
            if candidate.get("id") != expected_id or candidate.get("binding") != old.get("binding"):
                return False
            disk_topic["repair_pending"] = candidate
        save_state(state)
        if receipt is None:
            topic.pop("repair_pending", None)
        else:
            topic["repair_pending"] = copy.deepcopy(candidate)
        return True
