"""Authenticated, bounded handover receipts, kept outside archived application data."""

from __future__ import annotations

import hashlib
import json
import math
import re
import stat
from typing import Any, cast

from cryptography.fernet import InvalidToken

from tow import paths
from tow.errors import TowError
from tow.store import atomic_write_bytes, master_fernet, persistence_lock, read_regular_bytes

MAX_RECEIPTS = 64
MAX_BYTES = 32 * 1024 * 1024
LIFETIME = 12 * 3600
RETENTION = 24 * 3600
STATUSES = frozenset({"pending", "added", "existing", "failed", "cancelled"})
PHASES = frozenset({"queued", "observing_add", "checking", "ready", "observing_start", "done", "failed", "cancelled"})
JOURNAL = "client-only-adds.enc"
COMPATIBILITY = "client-only-compat.json"


def valid_id(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) is not None


def _valid_time(value: Any) -> bool:
    return type(value) in {int, float} and math.isfinite(value) and value > 0


def _validate(entries: Any) -> None:
    if not isinstance(entries, dict) or len(entries) > MAX_RECEIPTS:
        raise ValueError("invalid receipt container")
    for identity, row in entries.items():
        if not valid_id(identity) or not isinstance(row, dict) or row.get("operation_id") != identity:
            raise ValueError("invalid receipt identity")
        if row.get("status") not in STATUSES or row.get("phase") not in PHASES:
            raise ValueError("invalid receipt outcome")
        if not all(_valid_time(row.get(name)) for name in ("created_at", "deadline", "next_at")):
            raise ValueError("invalid receipt time")
        if not row["created_at"] <= row["deadline"] <= row["created_at"] + LIFETIME:
            raise ValueError("invalid receipt deadline")
        if any(type(row.get(name)) is not bool for name in ("add_dispatched", "start_dispatched")):
            raise ValueError("invalid dispatch reservation")
        if not isinstance(row.get("context"), dict) or not isinstance(row.get("client_id"), str):
            raise TypeError("invalid client context")
        context = row["context"]
        if context.get("client") != row["client_id"] or not all(
            isinstance(context.get(key), str) and re.fullmatch(r"[0-9a-f]{64}", context[key])
            for key in ("endpoint", "auth")
        ):
            raise ValueError("invalid context identity")
        hashes = row.get("hashes")
        if (
            not isinstance(hashes, list)
            or not hashes
            or any(
                not isinstance(value, str) or re.fullmatch(r"[0-9A-F]{40}|[0-9A-F]{64}", value) is None
                for value in hashes
            )
            or row.get("hash") not in hashes
        ):
            raise ValueError("invalid torrent identities")
        if type(row.get("failures")) is not int or row["failures"] < 0:
            raise ValueError("invalid retry count")
        if row["start_dispatched"] and not row["add_dispatched"]:
            raise ValueError("start without add authority")
        if not all(isinstance(row.get(name), str) for name in ("save_path", "install", "payload", "request")):
            raise ValueError("invalid receipt scope")


def _read_file(name: str, limit: int) -> bytes:
    folder = paths.run_dir()
    if folder.is_symlink() or (folder.exists() and not folder.is_dir()):
        raise ValueError("invalid handoff folder")
    path = folder / name
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("invalid handoff file")
    return read_regular_bytes(path, limit, same_as=info)


def _decode(raw: bytes) -> dict[str, dict[str, Any]]:
    try:
        content = master_fernet().decrypt(raw)
        if len(content) > MAX_BYTES:
            raise ValueError("receipt size limit")
        value = json.loads(content)
        if not isinstance(value, dict) or value.get("schema") != 1:
            raise ValueError("invalid receipt format")
        entries = value.get("entries")
        _validate(entries)
    except (InvalidToken, UnicodeError, ValueError, TypeError, RecursionError) as error:
        raise TowError("client_only.store_invalid") from error
    return cast("dict[str, dict[str, Any]]", entries)


def read_entries() -> dict[str, dict[str, Any]]:
    try:
        try:
            raw = _read_file(JOURNAL, MAX_BYTES * 2)
        except FileNotFoundError:
            try:
                _read_file(COMPATIBILITY, 4096)
            except FileNotFoundError:
                return {}
            raise TowError("client_only.store_invalid") from None
    except (OSError, ValueError) as error:
        raise TowError("client_only.store_invalid") from error
    return _decode(raw)


def _projection(raw: bytes, entries: dict[str, dict[str, Any]]) -> bytes:
    return json.dumps(
        {
            "schema": 1,
            "journal_sha256": hashlib.sha256(raw).hexdigest(),
            "pending": any(row["status"] == "pending" for row in entries.values()),
        },
        sort_keys=True,
    ).encode()


def sync_compatibility() -> bool:
    """Authenticate current receipts and repair only their derived compatibility projection.

    The background producer also migrates terminal journals written by 1.37.0, so a standalone
    rollback does not depend on visiting a web page. Unknown data or a failed write raises.
    """
    with persistence_lock():
        try:
            raw = _read_file(JOURNAL, MAX_BYTES * 2)
        except FileNotFoundError:
            try:
                _read_file(COMPATIBILITY, 4096)
            except FileNotFoundError:
                return False
            raise TowError("client_only.store_invalid") from None
        entries = _decode(raw)
        expected = _projection(raw, entries)
        try:
            current = _read_file(COMPATIBILITY, 4096)
        except FileNotFoundError, ValueError:
            current = b""
        if current != expected:
            atomic_write_bytes(paths.run_dir() / COMPATIBILITY, expected)
        return any(row["status"] == "pending" for row in entries.values())


def has_pending_or_unknown() -> bool:
    """An old release may proceed only after an authenticated terminal journal, or no journal."""
    try:
        return sync_compatibility()
    except OSError, RuntimeError, ValueError:
        return True


def write_entries(entries: dict[str, dict[str, Any]]) -> None:
    try:
        _validate(entries)
        content = json.dumps({"schema": 1, "entries": entries}, ensure_ascii=False, allow_nan=False).encode()
        if len(content) > MAX_BYTES:
            raise ValueError("receipt size limit")
    except (ValueError, TypeError, RecursionError) as error:
        raise TowError("client_only.capacity") from error
    with persistence_lock():
        raw = master_fernet().encrypt(content)
        # Journal first: interruption before the projection leaves a digest mismatch, so an
        # older updater never trusts a stale "no pending" projection for new authorization.
        atomic_write_bytes(paths.run_dir() / JOURNAL, raw)
        atomic_write_bytes(paths.run_dir() / COMPATIBILITY, _projection(raw, entries))


def prune(entries: dict[str, dict[str, Any]], now: float) -> None:
    terminal = sorted(
        (row for row in entries.values() if row["status"] != "pending"), key=lambda row: row["created_at"]
    )
    for row in terminal:
        if now - row["created_at"] >= RETENTION or len(entries) >= MAX_RECEIPTS:
            entries.pop(row["operation_id"], None)
