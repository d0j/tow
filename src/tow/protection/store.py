"""Encrypted verified clearance and durable cross-process attempt reservations."""

from __future__ import annotations

import hashlib
import math
import uuid
from pathlib import Path
from typing import Any

from tow.paths import data_dir, tmp_dir
from tow.platform import current as current_platform
from tow.platform import is_plain_dir, is_plain_file
from tow.protection.providers import validate_clearance
from tow.protection.spec import Clearance
from tow.storage.jsonio import decode_json_bytes, encode_json
from tow.storage.locks import check_run_lock
from tow.store import atomic_write_bytes, master_fernet, persistence_lock, read_regular_bytes

_MAX_BYTES = 1024 * 1024
_MAX_RECORDS = 256


def _key(site: str, origin: str) -> str:
    if not isinstance(site, str) or not 1 <= len(site) <= 128:
        raise ValueError("invalid protection site")
    return hashlib.sha256(encode_json([site, origin], compact=True)).hexdigest()


def signature(settings: dict[str, Any]) -> str:
    return hashlib.sha256(encode_json(settings, compact=True)).hexdigest()


def _path() -> Path:
    path = data_dir() / "protection.enc"
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
    if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("records"), dict):
        raise ValueError("invalid clearance storage")
    records = value["records"]
    if len(records) > _MAX_RECORDS:
        raise ValueError("clearance storage limit")
    for key, record in records.items():
        if not isinstance(key, str) or len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
            raise ValueError("invalid clearance binding")
        if not isinstance(record, dict) or set(record) - {"attempt_at", "lease_until", "settings", "clearance"}:
            raise ValueError("invalid clearance record")
        timestamp = record.get("attempt_at")
        if (
            not isinstance(timestamp, (int, float))
            or isinstance(timestamp, bool)
            or not math.isfinite(timestamp)
            or timestamp < 0
        ):
            raise ValueError("invalid clearance reservation")
        if not isinstance(record.get("settings"), str) or len(record["settings"]) != 64:
            raise ValueError("invalid clearance configuration binding")
        lease = record.get("lease_until")
        if (
            not isinstance(lease, (int, float))
            or isinstance(lease, bool)
            or not math.isfinite(lease)
            or lease < timestamp
        ):
            raise ValueError("invalid clearance lease")
    return dict(records)


def _save(records: dict[str, Any]) -> None:
    payload = encode_json({"version": 1, "records": records}, compact=True)
    encrypted = master_fernet().encrypt(payload)
    if len(encrypted) > _MAX_BYTES:
        raise ValueError("clearance storage limit")
    atomic_write_bytes(_path(), encrypted)


def read(site: str, origin: str, settings: dict[str, Any], *, now: float) -> Clearance | None:
    # One atomically published file: no data lock (which may recover/write journals)
    # is needed for a read-only preview or an already verified credential lookup.
    record = _load().get(_key(site, origin), {})
    if record.get("settings") != signature(settings):
        return None
    raw = record.get("clearance")
    if not isinstance(raw, dict) or set(raw) != {"cookies", "user_agent", "expires_at", "provider"}:
        return None
    value = Clearance(**raw)
    try:
        value = validate_clearance(value, origin, now=now)
    except ValueError, TypeError:
        return None
    if value.provider not in {p["id"] for p in settings["providers"] if p["enabled"]}:
        return None
    return value


def reserve(site: str, origin: str, settings: dict[str, Any], *, now: float) -> bool:
    """Persist before network; a crash still consumes the attempt's cooldown."""
    with persistence_lock():
        records = _load()
        key = _key(site, origin)
        previous = records.get(key)
        # Configuration changes must not bypass single-flight/cooldown. A future timestamp
        # after a clock correction refuses attempts until the original anchor is reached.
        if previous and (now < previous["lease_until"] or now - previous["attempt_at"] < settings["min_interval_sec"]):
            return False
        if key not in records and len(records) >= _MAX_RECORDS:
            records = {k: v for k, v in records.items() if now - v["attempt_at"] < 86400}
            if len(records) >= _MAX_RECORDS:
                return False
        records[key] = {
            "attempt_at": now,
            "lease_until": now + settings["timeout_sec"] + 5,
            "settings": signature(settings),
        }
        _save(records)
        return True


def accept(site: str, origin: str, settings: dict[str, Any], clearance: Clearance, *, now: float) -> None:
    with persistence_lock():
        clearance = validate_clearance(clearance, origin, now=now)
        records = _load()
        key = _key(site, origin)
        record = records.get(key)
        if record is None or record["settings"] != signature(settings):
            return
        encoded = {
            "cookies": clearance.cookies,
            "user_agent": clearance.user_agent,
            "expires_at": clearance.expires_at,
            "provider": clearance.provider,
        }
        if record.get("clearance") == encoded:
            return
        record["clearance"] = encoded
        _save(records)


def discard(site: str, origin: str, *, expected: Clearance | None = None) -> None:
    with persistence_lock():
        records = _load()
        record = records.get(_key(site, origin))
        if record and "clearance" in record:
            if expected is not None and record["clearance"] != {
                "cookies": expected.cookies,
                "user_agent": expected.user_agent,
                "expires_at": expected.expires_at,
                "provider": expected.provider,
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
