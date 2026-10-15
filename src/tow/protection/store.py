"""Encrypted verified clearance and durable cross-process attempt reservations."""

from __future__ import annotations

import hashlib
import math
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tow.paths import data_dir, tmp_dir
from tow.platform import current as current_platform
from tow.platform import is_plain_dir, is_plain_file
from tow.protection.providers import validate_clearance
from tow.protection.spec import AttemptProof, Clearance, target_origin
from tow.storage.jsonio import decode_json_bytes, encode_json
from tow.storage.locks import check_run_lock
from tow.store import atomic_write_bytes, master_fernet, persistence_lock, read_regular_bytes

_MAX_BYTES = 1024 * 1024
_MAX_RECORDS = 256
_COMMON_FIELDS = frozenset({"attempt_at", "lease_until", "settings"})
_SERVICE_FIELDS = frozenset({"service_status", "service_failures", "service_retry_at"})


def _finite_time(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and 0 <= value <= 253402300799
    )


def _validate_record(record: object) -> None:
    if not isinstance(record, dict):
        raise ValueError("invalid clearance record")  # noqa: TRY004 - the bounded storage decoder uses ValueError
    service = "service_status" in record
    fields = _COMMON_FIELDS | (_SERVICE_FIELDS if service else {"clearance", "attempt_id", "provider_cursor"})
    if set(record) - fields or not set(record) >= _COMMON_FIELDS:
        raise ValueError("invalid clearance record")
    raw = record.get("clearance")
    if (
        isinstance(raw, dict)
        and "attempt_id" in raw
        and (not isinstance(raw["attempt_id"], str) or not re.fullmatch(r"[0-9a-f]{32}", raw["attempt_id"]))
    ):
        raise ValueError("invalid clearance identity")
    timestamp, lease = record["attempt_at"], record["lease_until"]
    if not _finite_time(timestamp) or not _finite_time(lease) or lease < timestamp:
        raise ValueError("invalid clearance reservation")
    binding = record["settings"]
    if not isinstance(binding, str) or len(binding) != 64 or any(c not in "0123456789abcdef" for c in binding):
        raise ValueError("invalid clearance configuration binding")
    for name, pattern in (("attempt_id", r"[0-9a-f]{32}"), ("provider_cursor", r"[a-z0-9][a-z0-9_-]{0,47}")):
        if name in record and (not isinstance(record[name], str) or not re.fullmatch(pattern, record[name])):
            raise ValueError("invalid clearance attempt")
    if service:
        failures, retry = record.get("service_failures"), record.get("service_retry_at")
        if (
            record["service_status"] not in {"succeeded", "unavailable", "busy", "protocol"}
            or type(failures) is not int
            or not 0 <= failures <= 8
            or not _finite_time(retry)
            or (record["service_status"] == "succeeded" and (failures != 0 or retry != 0))
            or (
                record["service_status"] != "succeeded"
                and (failures < 1 or not timestamp <= retry <= timestamp + 86400)
            )
        ):
            raise ValueError("invalid clearance service observation")


def _key(site: str, origin: str) -> str:
    if not isinstance(site, str) or not 1 <= len(site) <= 128:
        raise ValueError("invalid protection site")
    return hashlib.sha256(encode_json([site, origin], compact=True)).hexdigest()


def signature(settings: dict[str, Any]) -> str:
    return hashlib.sha256(encode_json(settings, compact=True)).hexdigest()


def _path() -> Path:
    path = data_dir(create=False) / "protection.enc"
    folder = path.parent
    if folder.is_symlink() or folder.is_junction():
        raise ValueError("redirected clearance storage")
    return path


def _load() -> dict[str, Any]:
    path = _path()
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {}
    if not is_plain_file(info):
        raise ValueError("invalid clearance storage")
    raw = read_regular_bytes(path, _MAX_BYTES, same_as=info)
    value = decode_json_bytes(master_fernet().decrypt(raw))
    if (
        not isinstance(value, dict)
        or type(value.get("version")) is not int
        or value["version"] not in {1, 2}
        or not isinstance(value.get("records"), dict)
    ):
        raise ValueError("invalid clearance storage")
    records = value["records"]
    if len(records) > _MAX_RECORDS:
        raise ValueError("clearance storage limit")
    for key, record in records.items():
        if not isinstance(key, str) or len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
            raise ValueError("invalid clearance binding")
        _validate_record(record)
    return dict(records)


def _save(records: dict[str, Any]) -> None:
    if len(records) > _MAX_RECORDS:
        raise ValueError("clearance storage limit")
    for record in records.values():
        _validate_record(record)
    payload = encode_json({"version": 2, "records": records}, compact=True)
    encrypted = master_fernet().encrypt(payload)
    if len(encrypted) > _MAX_BYTES:
        raise ValueError("clearance storage limit")
    atomic_write_bytes(_path(), encrypted)


def _service_key(provider: dict[str, Any]) -> str:
    # The provider id, enable flag, order and unrelated connection edits cannot reset a fuse.
    return signature({"type": "service", "kind": provider["kind"], "url": provider["url"]})


# The longest reservation: a recovery's 90-second limit and 5 seconds to finish it.
_MAX_LEASE = 95


def _clock_corrected(record: dict[str, Any], now: float) -> dict[str, Any] | None:
    """``record`` with an anchor in the future (the clock was set back) moved to ``now`` and its
    waits kept as long as they were meant to be; None when it needs no correction. Without it
    a site or a service would wait until the old time came again - days or years."""
    shift = max(0.0, record["attempt_at"] - now)
    attempt_at = record["attempt_at"] - shift
    lease_until = min(record["lease_until"] - shift, attempt_at + _MAX_LEASE)
    if not shift and lease_until == record["lease_until"]:
        return None
    fixed = {**record, "attempt_at": attempt_at, "lease_until": max(attempt_at, lease_until)}
    if fixed.get("service_retry_at"):
        fixed["service_retry_at"] = record["service_retry_at"] - shift
    return fixed


def correct_clock(site: str, origin: str, providers: list[dict[str, Any]], *, now: float) -> None:
    """Persist the correction of a future reservation or service pause once, on an applying
    recovery: corrected only in memory it would move along with every later reading."""
    if not _finite_time(now):
        raise ValueError("invalid clearance observation time")
    with persistence_lock():
        records = _load()
        changed = False
        for key in (_key(site, origin), *(_service_key(provider) for provider in providers)):
            if key in records and (fixed := _clock_corrected(records[key], now)) is not None:
                records[key] = fixed
                changed = True
        if changed:
            _save(records)


def service_states(providers: list[dict[str, Any]], *, now: float) -> dict[str, dict[str, Any]]:
    """Read-only API observations, never clearance values or an assertion of site access."""
    if not _finite_time(now):
        raise ValueError("invalid clearance observation time")
    records = _load()
    result = {}
    for provider in providers:
        record = records.get(_service_key(provider), {})
        if record:
            record = _clock_corrected(record, now) or record  # shown as meant; written by a recovery
        retry = float(record.get("service_retry_at", 0))
        result[provider["id"]] = {
            "status": record.get("service_status", "unknown"),
            "at": record.get("attempt_at"),
            "retry_at": retry,
            "ready": retry <= now,
        }
    return result


def eligible_providers(
    site: str, origin: str, settings: dict[str, Any], candidates: list[dict[str, Any]], *, now: float
) -> list[dict[str, Any]]:
    """Continue a failed bounded chain rather than starving every later enabled adapter."""
    records = _load()
    previous = records.get(_key(site, origin), {})
    ordered = [p for p in settings["providers"] if p["enabled"]]
    if previous.get("settings") == signature(settings):
        ids = [p["id"] for p in ordered]
        cursor = previous.get("provider_cursor")
        if cursor in ids:
            start = ids.index(cursor) + 1
            ordered = ordered[start:] + ordered[:start]
    available = {p["id"] for p in candidates}
    return [
        p
        for p in ordered
        if p["id"] in available and records.get(_service_key(p), {}).get("service_retry_at", 0) <= now
    ]


def note_service(
    provider: dict[str, Any],
    status: str,
    *,
    now: float,
    retry_after: float = 0,
    attempt: AttemptProof | None = None,
) -> None:
    """Persist API-level backoff across sites; a refused site replay must never call this."""
    if status not in {"succeeded", "unavailable", "busy", "protocol"} or not _finite_time(now):
        raise ValueError("invalid clearance service observation")
    if not _finite_time(retry_after):
        raise ValueError("invalid clearance retry interval")
    with persistence_lock():
        records = _load()
        if (
            attempt is not None
            and records.get(_key(attempt.site, attempt.origin), {}).get("attempt_id") != attempt.nonce
        ):
            return  # reset/new reservation invalidates every late outcome of an older request
        key = _service_key(provider)
        if key not in records and len(records) >= _MAX_RECORDS:
            records = {k: v for k, v in records.items() if now - v["attempt_at"] < 86400}
            if len(records) >= _MAX_RECORDS:
                return
        previous = records.get(key, {})
        old_failures = previous.get("service_failures", 0) if now - previous.get("attempt_at", now) < 86400 else 0
        failures = 0 if status == "succeeded" else min(8, old_failures + 1)
        pause = 0 if failures == 0 else min(86400, max(min(1800, 30 * 2 ** (failures - 1)), retry_after))
        records[key] = {
            "attempt_at": now,
            "lease_until": now,
            "settings": key,
            "service_status": status,
            "service_failures": failures,
            "service_retry_at": now + pause if pause else 0,
        }
        _save(records)


def _read_clearance(record: dict[str, Any], origin: str, settings: dict[str, Any], now: float) -> Clearance | None:
    if record.get("settings") != signature(settings):
        return None
    raw = record.get("clearance")
    fields = {"cookies", "user_agent", "expires_at", "provider"}
    if not isinstance(raw, dict) or set(raw) not in (fields, fields | {"attempt_id"}):
        return None
    # Legacy records tied clearance to the active reservation. Never use that fallback
    # for the new nested identity: a later reservation cannot relabel an older cookie.
    value = Clearance(
        **{**raw, "attempt_id": raw.get("attempt_id") if "attempt_id" in raw else record.get("attempt_id")}
    )
    try:
        value = validate_clearance(value, origin, now=now)
    except ValueError, TypeError:
        return None
    if value.provider not in {p["id"] for p in settings["providers"] if p["enabled"]}:
        return None
    return value


def read(site: str, origin: str, settings: dict[str, Any], *, now: float) -> Clearance | None:
    # One atomic file: read-only previews do not take a lock which can recover journals.
    return _read_clearance(_load().get(_key(site, origin), {}), origin, settings, now)


def recovery_origins(site: str, origins: list[str]) -> list[str]:
    """Oldest attempts first: a shared call cap cannot permanently starve later mirrors."""
    records = _load()

    def last_attempt(origin: str) -> float:
        try:
            return float(records.get(_key(site, target_origin(origin)), {}).get("attempt_at", -1))
        except ValueError:
            return -1

    return sorted(origins, key=last_attempt)


def _reserved(previous: dict[str, Any] | None, settings: dict[str, Any], now: float) -> bool:
    # Configuration changes must not bypass single-flight/cooldown. A future timestamp after
    # the clock was set back refuses attempts until a recovery corrects it (correct_clock).
    if not previous:
        return False
    return bool(now < previous["lease_until"] or now - previous["attempt_at"] < settings["min_interval_sec"])


def recoverable(
    site: str, origins: list[str], settings: dict[str, Any], providers: list[dict[str, Any]], *, now: float
) -> list[str]:
    """Read-only: the origins a new reservation could call one of ``providers`` for now."""
    records = _load()
    ready = any(records.get(_service_key(p), {}).get("service_retry_at", 0) <= now for p in providers)
    return [origin for origin in origins if ready and not _reserved(records.get(_key(site, origin)), settings, now)]


def reserve(site: str, origin: str, settings: dict[str, Any], *, now: float) -> bool:
    """Compatibility helper for a caller that only needs to know whether it reserved."""
    return reserve_attempt(site, origin, settings, now=now) is not None


def reserve_attempt(site: str, origin: str, settings: dict[str, Any], *, now: float) -> str | None:
    """Persist before network; a crash still consumes the attempt's cooldown."""
    with persistence_lock():
        records = _load()
        key = _key(site, origin)
        previous = records.get(key)
        if _reserved(previous, settings, now):
            return None
        if key not in records and len(records) >= _MAX_RECORDS:
            records = {k: v for k, v in records.items() if now - v["attempt_at"] < 86400}
            if len(records) >= _MAX_RECORDS:
                return None
        nonce = uuid.uuid4().hex
        records[key] = {
            "attempt_at": now,
            "lease_until": now + settings["timeout_sec"] + 5,
            "settings": signature(settings),
            "attempt_id": nonce,
        }
        if previous and previous.get("settings") == signature(settings) and previous.get("provider_cursor"):
            records[key]["provider_cursor"] = previous["provider_cursor"]
        if previous:
            retained = _read_clearance(previous, origin, settings, now)
            if retained is not None and retained.attempt_id is not None:
                records[key]["clearance"] = _encode_clearance(retained)
        _save(records)
        return nonce


def note_attempt(attempt: AttemptProof, provider_id: str) -> bool:
    """Write before the API call; a killed call still advances the next bounded chain."""
    with persistence_lock():
        records = _load()
        record = records.get(_key(attempt.site, attempt.origin))
        if not record or record.get("attempt_id") != attempt.nonce:
            return False
        record["provider_cursor"] = provider_id
        _save(records)
        return True


@dataclass(frozen=True, repr=False)
class PendingAttempt:
    """A reversible reservation until an external call may have asked the site (a local
    module's startup failure or warming answer has not)."""

    proof: AttemptProof
    previous: dict[str, Any] | None


def prepare_attempt(site: str, origin: str, settings: dict[str, Any], *, now: float) -> PendingAttempt | None:
    with persistence_lock():
        previous = _load().get(_key(site, origin))
        nonce = reserve_attempt(site, origin, settings, now=now)
        return None if nonce is None else PendingAttempt(AttemptProof(site, origin, nonce), previous)


def cancel_unused_attempt(pending: PendingAttempt) -> None:
    """Undo preparation, never a reset or a newer reservation's state."""
    with persistence_lock():
        records = _load()
        key = _key(pending.proof.site, pending.proof.origin)
        if records.get(key, {}).get("attempt_id") != pending.proof.nonce:
            return
        if pending.previous is None:
            records.pop(key)
        else:
            records[key] = pending.previous
        _save(records)


def finish_attempt(attempt: AttemptProof, provider_id: str) -> None:
    """Retain the last actually called provider, not one whose preparation timed out."""
    with persistence_lock():
        records = _load()
        record = records.get(_key(attempt.site, attempt.origin))
        if (
            record
            and record.get("attempt_id") == attempt.nonce
            and (record.get("clearance") or {}).get("attempt_id") != attempt.nonce
            and record.get("provider_cursor") != provider_id
        ):
            record["provider_cursor"] = provider_id
            _save(records)


def _encode_clearance(value: Clearance) -> dict[str, Any]:
    result = {
        "cookies": value.cookies,
        "user_agent": value.user_agent,
        "expires_at": value.expires_at,
        "provider": value.provider,
    }
    if value.attempt_id is not None:
        result["attempt_id"] = value.attempt_id
    return result


def accept(
    site: str,
    origin: str,
    settings: dict[str, Any],
    clearance: Clearance,
    *,
    now: float,
    require_attempt: bool = False,
) -> None:
    with persistence_lock():
        clearance = validate_clearance(clearance, origin, now=now)
        records = _load()
        key = _key(site, origin)
        record = records.get(key)
        if record is None or record["settings"] != signature(settings):
            return
        if require_attempt and (clearance.attempt_id is None or record.get("attempt_id") != clearance.attempt_id):
            return
        # Compatibility callers without a proof use the existing reservation identity.
        encoded = _encode_clearance(
            Clearance(
                clearance.cookies,
                clearance.user_agent,
                clearance.expires_at,
                clearance.provider,
                clearance.attempt_id or record.get("attempt_id"),
            )
        )
        if record.get("clearance") == encoded and "provider_cursor" not in record:
            return
        record["clearance"] = encoded
        record.pop("provider_cursor", None)
        _save(records)


def discard(site: str, origin: str, *, expected: Clearance | None = None) -> None:
    with persistence_lock():
        records = _load()
        record = records.get(_key(site, origin))
        if record and "clearance" in record:
            raw = record["clearance"]
            identity = (
                raw.get("attempt_id") if isinstance(raw, dict) and "attempt_id" in raw else record.get("attempt_id")
            )
            if expected is not None and identity != expected.attempt_id:
                return
            if expected is not None and {key: value for key, value in raw.items() if key != "attempt_id"} != {
                key: value for key, value in _encode_clearance(expected).items() if key != "attempt_id"
            }:
                return  # an old refused replay does not revoke a newer verified credential
            del record["clearance"]
            _save(records)


def reset() -> bool:
    """Explicit local recovery: preserve an unreadable record without stopping a check.

    A running applying check owns the check lock before taking the data lock. Keeping the
    same order prevents reset from racing its eventual verified credential acceptance.
    """
    with check_run_lock(wait=False), persistence_lock():
        path = _path()
        try:
            original = path.lstat()
        except FileNotFoundError:
            return False
        if not is_plain_file(original):
            raise ValueError("clearance reset refuses non-regular storage")
        raw = read_regular_bytes(path, _MAX_BYTES, same_as=original)
        folder = tmp_dir(create=False)
        if folder.parent != path.parent or not is_plain_dir(path.parent.lstat()):
            raise ValueError("clearance reset folder refused")
        try:
            info = folder.lstat()
        except FileNotFoundError:
            folder.mkdir(mode=0o700)
            info = folder.lstat()
        if not is_plain_dir(info):
            raise ValueError("clearance reset folder refused")
        retained = folder / f"protection-reset-{uuid.uuid4().hex}.enc"
        current = path.lstat()
        if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != (
            original.st_dev,
            original.st_ino,
            original.st_size,
            original.st_mtime_ns,
        ) or not is_plain_file(current):
            raise ValueError("clearance reset source changed")
        current_platform().publish_exclusive(path, retained)
        moved = retained.lstat()
        if not is_plain_file(moved) or read_regular_bytes(retained, _MAX_BYTES, same_as=moved) != raw:
            raise ValueError("clearance reset read-back failed")
        # Windows publishes by exclusive rename; POSIX by exclusive hard link. Only the
        # exact previously inspected source is removed in the latter case.
        try:
            remaining = path.lstat()
        except FileNotFoundError:
            return True
        if (remaining.st_dev, remaining.st_ino, remaining.st_size, remaining.st_mtime_ns) != (
            original.st_dev,
            original.st_ino,
            original.st_size,
            original.st_mtime_ns,
        ) or not is_plain_file(remaining):
            raise ValueError("clearance reset source changed")
        path.unlink()
        try:
            path.lstat()
        except FileNotFoundError:
            return True
        raise ValueError("clearance reset removal not confirmed")
