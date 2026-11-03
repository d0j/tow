"""The record of the last night copy (backup-status.json) and the cleanup it observed."""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import time
from pathlib import Path
from typing import Any

from tow.backup_actions import added_names, cleanup_names, inventory_digest, observed_in
from tow.diagnostic_json import check_epochs, check_types, encode_object, read_object
from tow.i18n import t
from tow.log import log_event
from tow.paths import data_dir
from tow.platform import is_plain_file
from tow.scrub import scrub_credentials
from tow.snapshots.errors import SnapshotError
from tow.snapshots.location import PREFIX, backup_root
from tow.snapshots.manifest import owned_manifest, signing_key
from tow.store import persistence_lock

STATUS_NAME = "backup-status.json"


def location_revision(cfg: dict[str, Any]) -> str:
    """Opaque identity for a backup attempt's destination, stable within a portable install."""
    from tow.paths import in_root

    try:
        location = backup_root(cfg)
        relative = in_root(location)
        name = "relative:" + relative if relative is not None else "absolute:" + str(location.resolve())
        name = os.path.normcase(name)
    except OSError, ValueError, TypeError, SnapshotError:
        # A bad copy destination must not disable checks, the web server or the watchdog.
        name = "unresolved:" + str(cfg.get("backup_dir") or "")
    return hashlib.sha256(name.encode("utf-8")).hexdigest()


def status_path() -> Path:
    return data_dir() / STATUS_NAME


def status() -> dict[str, Any]:
    """Last success and last failure of a night copy (for the watchdog and Settings)."""
    try:
        info = status_path().lstat()
        if not is_plain_file(info):
            raise ValueError("backup record is not a regular file")
        value = read_object(status_path())
        check_epochs(value, ("last_ok_at", "last_error_at"))
        check_types(
            value,
            {
                "last_error": str,
                "last_snapshot": str,
                "location": str,
                "location_in_root": str,
                "attempt_location": str,
                "attempt_location_in_root": str,
                "last_cleanup_pending": bool,
                "last_bytes": int,
                "last_missing": list,
            },
        )
        if value.get("last_bytes", 0) is not None and value.get("last_bytes", 0) < 0:
            raise ValueError("invalid service size")
        if any(not isinstance(item, str) for item in (value.get("last_missing") or [])):
            raise ValueError("invalid missing members")
    except FileNotFoundError:
        return {}
    except OSError, UnicodeError, ValueError, TypeError, RecursionError:
        return {"read_error": True, "last_error": t("watchdog.backup.status_unreadable")}
    return value


def cleanup_inventory(location: Path) -> dict[str, Any]:
    """The record fields binding cleanup to committed copy names (partial copies begin with a dot)."""
    names = cleanup_names(location, lambda name: name.startswith(PREFIX))
    return {"cleanup_inventory": inventory_digest(names), "cleanup_names": names}


def _new_copy_of_ours(location: Path, state: dict[str, Any]) -> bool:
    """A copy of ours committed after the observation; one removed by hand or a foreign folder is not."""
    names = cleanup_names(location, lambda name: name.startswith(PREFIX))
    added = added_names(state.get("cleanup_names"), state["cleanup_inventory"], names)
    if not added:
        return False
    key = signing_key()
    return any(owned_manifest(location / name, key) is not None for name in added)


def cleanup_status(*, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Cleanup in the current folder: pending, complete or unknown, never inferred from absence."""
    with persistence_lock():  # inventory and record must describe the same completed copy
        return cleanup_status_in_lock(cfg=cfg)


def cleanup_status_unlocked(*, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """``cleanup_status`` for a reader that must not take the data lock (whose taking runs the
    recovery steps) - the web's synchronization poll. The same reading without the lock: a copy
    committed at that very moment may read as unknown until the next look."""
    return cleanup_status_in_lock(cfg=cfg)


def cleanup_status_in_lock(*, cfg: dict[str, Any] | None) -> dict[str, Any]:
    result: dict[str, Any] = {"pending": None, "read_error": False, "location": "", "legacy": False}
    try:
        location = backup_root(cfg)
        result["location"] = str(location.resolve())
        state = status()
        if state.get("read_error"):
            raise ValueError("unreadable backup record")
        if "last_cleanup_pending" not in state:
            # Copies made before cleanup monitoring have a full copy result but no flag.
            # This is migration, not lost observation; a prior bound watchdog still detects
            # loss of both new fields through its persistent bound marker.
            if (
                "cleanup_inventory" not in state
                and isinstance(state.get("last_ok_at"), (int, float))
                and isinstance(state.get("last_snapshot"), str)
                and re.fullmatch(r"tow-\d{8}-\d{6}(?:-[a-f0-9]{6})?", state["last_snapshot"])
                and state.get("location") == result["location"]
            ):
                cleanup_names(location, bool)  # old metadata cannot hide a folder-access failure
                result["legacy"] = True
            return result  # a new install or a copy failure alone has no cleanup observation
        if type(state["last_cleanup_pending"]) is not bool or not isinstance(state.get("location"), str):
            raise ValueError("invalid cleanup observation")
        if not state["location"]:
            raise ValueError("missing cleanup location")
        inventory = state.get("cleanup_inventory")
        if "cleanup_inventory" in state and (
            not isinstance(inventory, str) or re.fullmatch(r"[a-f0-9]{64}", inventory) is None
        ):
            raise ValueError("invalid cleanup inventory")
        if observed_in(state, location, result["location"]):
            if "cleanup_inventory" not in state:
                cleanup_names(location, bool)  # old metadata cannot hide a folder-access failure
                result["legacy"] = True
                if state["last_cleanup_pending"]:
                    result["pending"] = True
            elif _new_copy_of_ours(location, state):
                raise ValueError("cleanup observation belongs to an earlier inventory")
            else:
                result["pending"] = state["last_cleanup_pending"]
    except OSError, ValueError, TypeError, UnicodeError, RecursionError, SnapshotError:
        result["read_error"] = True
    return result


def status_failed(value: dict[str, Any]) -> bool:
    """The latest attempt's error wins, even with equal dates or a clock correction."""
    if value.get("read_error"):
        return True
    if "last_error" in value:
        return bool(value["last_error"])
    failed = value.get("last_error_at")
    return bool(failed is not None and (not value.get("last_ok_at") or failed >= value["last_ok_at"]))


def current_status(*, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Copy health in the selected folder; old folders cannot prove or disprove its backup.

    Cleanup keeps using the original record, with its independent inventory binding. A
    failed attempt has its own location: it must never rebind an earlier successful copy.
    """
    value = status()
    if not value or value.get("read_error"):
        return value
    try:
        location = backup_root(cfg)
        resolved = str(location.resolve())
        success_here = observed_in(value, location, resolved)
        attempt = {
            "location": value.get("attempt_location", value.get("location")),
            "location_in_root": value.get("attempt_location_in_root", value.get("location_in_root")),
        }
        # A legacy failure without any location stays visible: its cause is unknown.
        failure_here = not attempt["location"] or observed_in(attempt, location, resolved)
    except OSError, ValueError, TypeError, SnapshotError:
        return {"read_error": True, "last_error": t("watchdog.backup.status_unreadable")}
    scoped = dict(value)
    if not success_here:
        for field in ("last_ok_at", "last_snapshot", "last_bytes", "last_missing"):
            scoped.pop(field, None)
    if not failure_here:
        for field in ("last_error", "last_error_at"):
            scoped.pop(field, None)
    return scoped


def record_status(**fields: Any) -> None:
    from tow.store import atomic_write_text

    current = status()
    if current.get("read_error"):
        current = {}
    current.update(fields)
    # The status is informative; the copy itself already succeeded or failed.
    with contextlib.suppress(OSError, ValueError, UnicodeError, RecursionError):
        atomic_write_text(status_path(), encode_object(current))


def record_failure(error: str) -> None:
    error = scrub_credentials(error)  # a share's link may carry a sign-in: never stored, never shown
    from tow.paths import in_root

    fields: dict[str, Any] = {"attempt_location": "", "attempt_location_in_root": None}
    with contextlib.suppress(OSError, ValueError, TypeError, SnapshotError):
        location = backup_root()
        fields = {"attempt_location": str(location.resolve()), "attempt_location_in_root": in_root(location)}
    record_status(last_error=error[:300], last_error_at=round(time.time(), 3), **fields)
    log_event("backup_failed", error=error[:300], how="auto")
