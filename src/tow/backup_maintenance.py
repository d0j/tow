"""Bounded retry of already-pending local backup cleanup; never makes a new copy.

Expensive archive verification is outside the data lock. A durable reservation precedes it;
the final metadata comparison and one local removal share the writers' data lock. Unknown or
network volumes are left alone until a shared per-folder writer protocol can cover them.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from tow import platform
from tow.backup_actions import copy_revision
from tow.backup_retention import copy_time, retained_copies, retention_settings
from tow.config import as_bool, load_config
from tow.diagnostic_json import encode_object, read_object
from tow.locations import MANUAL, NIGHT, volume_unseen
from tow.paths import data_dir, read_only_now
from tow.store import atomic_write_text, derive_local_secret, load_state, persistence_lock

BACKOFF = (600, 1800, 7200)
TTL_SEC = 24 * 3600
STATE_NAME = "backup-maintenance.json"


class Deferred(RuntimeError):
    """A typed reason, never a path, secret or arbitrary exception text."""


@dataclass(frozen=True)
class Context:
    kind: str
    folder: Path
    policy: dict[str, Any]
    revision: str


@dataclass(frozen=True)
class Plan:
    context: Context
    candidate: Path | None
    protected: Path | None
    marker: str
    expected: frozenset[str]
    complete: bool
    retained: tuple[Path, ...] = ()


def _context(kind: str, cfg: dict[str, Any]) -> Context:
    from tow import restore_points, snapshots

    if kind == "night" and not as_bool(cfg.get("backup_enabled"), True):
        raise Deferred("disabled")
    folder = snapshots.backup_root(cfg) if kind == "night" else restore_points.restore_points_dir(cfg=cfg)
    volume = platform.volume_of(folder)
    if volume is not None and volume.kind == "network":
        raise Deferred("remote_folder")
    if volume is None or volume.kind != "local" or not volume.identity:
        raise Deferred("volume_unknown")
    if volume_unseen(load_state(), NIGHT if kind == "night" else MANUAL, folder):
        raise Deferred("volume_changed")
    observed = (snapshots.cleanup_status_unlocked if kind == "night" else restore_points.cleanup_status)(cfg=cfg)
    if observed.get("read_error") or observed.get("legacy") or observed.get("pending") is None:
        raise Deferred("observation_unknown")
    if observed["pending"] is not True:
        raise Deferred("not_pending")
    # Names alone miss an in-place replacement, a changed byte budget, or a partial removal.
    candidates = list(folder.iterdir())
    names = [
        path
        for path in candidates
        if (
            path.name.startswith(("tow-", ".tow-"))
            if kind == "night"
            else restore_points.is_restore_point_file(path.name)
        )
    ]
    inventory = [(path.name, copy_revision(path)) for path in sorted(names)]
    policy = retention_settings(cfg) if kind == "night" else {"keep": restore_points.RESTORE_POINT_LIMIT}
    raw = [kind, str(folder.resolve()), volume.identity, policy, inventory, derive_local_secret("backup-cleanup-owner")]
    revision = hashlib.sha256(json.dumps(raw, sort_keys=True).encode("utf-8")).hexdigest()
    return Context(kind, folder, policy, revision)


def _plan(context: Context) -> Plan:
    from tow import restore_points, snapshots
    from tow.snapshots import create
    from tow.snapshots.manifest import owned_manifest, signing_key
    from tow.snapshots.members import copy_members_in, owned_tree

    if context.kind == "manual":
        points = restore_points.list_restore_points()
        old_points = points[context.policy["keep"] :]
        candidate = restore_points.point_path(old_points[-1]["id"]) if old_points else None
        if candidate is not None and candidate.parent.resolve() != context.folder.resolve():
            raise Deferred("context_changed")
        kept_manual = tuple(restore_points.point_path(point["id"]) for point in points[: context.policy["keep"]])
        if any(path.parent.resolve() != context.folder.resolve() for path in kept_manual):
            raise Deferred("context_changed")
        return Plan(context, candidate, None, "", frozenset(), len(old_points) <= 1, kept_manual)
    key = signing_key()
    status = snapshots.current_status()
    entries, copies = [], {}
    from tow.snapshots.location import snapshot_folders
    from tow.snapshots.members import POINT_MEMBER, fixed_members

    fixed = {name for name, _path in fixed_members()}
    for folder in snapshot_folders(context.folder):
        manifest = owned_manifest(folder, key)
        if (
            manifest is None
            or any(name not in fixed and POINT_MEMBER.fullmatch(name) is None for name in manifest["files"])
            or not owned_tree(folder, {"MANIFEST.json", *manifest["files"]})
        ):
            raise Deferred("copy_unknown")
        entries.append((folder.name, copy_time(manifest["created_at"]), create.copy_disk_bytes(folder)))
        copies[folder.name] = manifest
    latest = str(status.get("last_snapshot") or "")
    if latest not in copies:
        raise Deferred("protected_copy_unknown")
    protected = context.folder / latest
    retained, _over_budget = retained_copies(
        entries,
        newest=latest,
        now=copy_time(copies[latest]["created_at"]),
        policy=context.policy,
    )
    old = sorted(name for name in copies if name not in retained)
    partials = create.partial_folders(context.folder)
    if old:
        name = old[0]
        return Plan(
            context,
            context.folder / name,
            protected,
            "MANIFEST.json",
            frozenset({"MANIFEST.json", *copies[name]["files"]}),
            len(old) == 1 and not partials,
        )
    if partials:
        path = min(partials)
        marker = create.partial_owner_proof(path, key)
        if marker is None:
            raise Deferred("copy_unknown")
        expected = frozenset({marker, "MANIFEST.json", *copy_members_in(path)})
        if not owned_tree(path, set(expected)):
            raise Deferred("copy_unknown")
        return Plan(context, path, protected, marker, expected, len(partials) == 1)
    return Plan(context, None, protected, "", frozenset(), True)


def _verify(plan: Plan) -> Plan:
    from tow import restore_points, snapshots
    from tow.bundle import ExportImportError

    if plan.context.kind == "manual" and plan.candidate is not None:
        for retained in plan.retained:
            try:
                restore_points.verify_restore_point_file(retained)
            except ExportImportError, OSError, restore_points.RestorePointError:
                continue
            restore_points.verify_restore_point_file(plan.candidate)
            return replace(plan, protected=retained)
        raise Deferred("protected_copy_unknown")
    elif plan.protected is not None and snapshots.verify_snapshot(plan.protected).get("signed") is not True:
        # Preserve a real verified recovery copy before pruning an older signed owned tree.
        raise Deferred("protected_copy_unknown")
    return plan


def _read() -> dict[str, Any]:
    try:
        value = read_object(data_dir() / STATE_NAME)
        if any(key not in {"manual", "night"} or not isinstance(row, dict) for key, row in value.items()):
            raise ValueError("invalid maintenance observation")
        for row in value.values():
            if "revision" not in row:
                if set(row) != {"reason"} or not isinstance(row["reason"], str) or not row["reason"]:
                    raise ValueError("unknown maintenance budget")
                continue
            if (
                not isinstance(row["revision"], str)
                or re.fullmatch(r"[a-f0-9]{64}", row["revision"]) is None
                or type(row.get("failures")) is not int
                or not 0 <= row["failures"] <= len(BACKOFF)
                or not isinstance(row.get("reason"), str)
                or not row["reason"]
            ):
                raise ValueError("invalid maintenance budget")
            for name in ("since", "next_at"):
                stamp = row.get(name)
                if (
                    not isinstance(stamp, (int, float))
                    or isinstance(stamp, bool)
                    or not math.isfinite(stamp)
                    or stamp < 0
                ):
                    raise ValueError("invalid maintenance deadline")
            operation = row.get("operation")
            if operation is not None and (
                not isinstance(operation, str) or re.fullmatch(r"[a-f0-9]{32}", operation) is None
            ):
                raise ValueError("invalid maintenance reservation")
        return value
    except FileNotFoundError:
        return {}
    except OSError, UnicodeError, ValueError, TypeError, RecursionError:
        raise Deferred("retry_record_unknown") from None


def _write(state: dict[str, Any]) -> None:
    atomic_write_text(data_dir() / STATE_NAME, encode_object(state))


def _reserve(context: Context, now: float) -> str | None:
    with persistence_lock():
        state = _read()
        old = state.get(context.kind) or {}
        same = isinstance(old, dict) and old.get("revision") == context.revision
        if same:
            failed, since, next_at = old.get("failures", 0), old.get("since"), old.get("next_at", 0)
            if not (
                type(failed) is int
                and 0 <= failed <= len(BACKOFF)
                and isinstance(since, (int, float))
                and not isinstance(since, bool)
                and isinstance(next_at, (int, float))
                and not isinstance(next_at, bool)
                and math.isfinite(since)
                and math.isfinite(next_at)
                and since >= 0
                and next_at >= 0
            ):
                raise Deferred("retry_record_unknown")
            if since > now:
                # A backward clock jump is anchored once, never on each subsequent tick.
                old.update(since=now, next_at=now + min(max(0, next_at - since), max(BACKOFF)))
                state[context.kind] = old
                _write(state)
                return None
            if failed >= len(BACKOFF) or now - since > TTL_SEC:
                return None
            if now < next_at:
                return None
            if old.get("operation"):
                failed += 1  # a crashed reservation is not a fresh unlimited retry budget
                old.update(failures=failed, reason="interrupted")
                old.pop("operation", None)
                state[context.kind] = old
                _write(state)
                if failed >= len(BACKOFF):
                    return None
        operation = uuid.uuid4().hex
        state[context.kind] = {
            "revision": context.revision,
            "failures": old.get("failures", 0) if same else 0,
            "since": old["since"] if same else now,
            "next_at": now + BACKOFF[0],
            "operation": operation,
            "reason": "reserved",
        }
        _write(state)
        return operation


def _finish(context: Context, operation: str, now: float, *, reason: str, success: bool) -> None:
    state = _read()
    old = state.get(context.kind) or {}
    if old.get("operation") != operation:
        return
    failures = 0 if success else old["failures"] + 1
    wait = BACKOFF[0] if success else BACKOFF[min(failures - 1, len(BACKOFF) - 1)]
    old.update(failures=failures, reason=reason, next_at=now + wait)
    old.pop("operation", None)
    state[context.kind] = old
    _write(state)


def _commit(plan: Plan, operation: str, now: float) -> str:
    from tow import restore_points
    from tow.snapshots.create import remove_copy
    from tow.snapshots.record import cleanup_inventory, record_status

    with persistence_lock():
        fresh = _context(plan.context.kind, load_config())
        if fresh.revision != plan.context.revision:
            _finish(plan.context, operation, now, reason="context_changed", success=False)
            return "context_changed"
        if (_read().get(fresh.kind) or {}).get("operation") != operation:
            return "reservation_lost"
        path = plan.candidate
        if path is not None:
            try:
                if fresh.kind == "manual":
                    path.unlink()
                else:
                    remove_copy(path, fresh.folder, plan.marker, set(plan.expected))
            except OSError:
                pass  # a lost acknowledgement is confirmed from actual absence below
            try:
                path.lstat()
            except FileNotFoundError:
                absent = True
            except OSError:
                absent = False  # an inaccessible path is not proof of deletion
            else:
                absent = False
            if not absent:
                # A partial removal changes metadata but must not reset its failure budget.
                state = _read()
                state[fresh.kind]["revision"] = _context(fresh.kind, load_config()).revision
                _write(state)
                _finish(fresh, operation, now, reason="remove_failed", success=False)
                return "remove_failed"
        if plan.complete:
            if fresh.kind == "manual":
                restore_points.record_cleanup_observation(False, fresh.folder)
            else:
                record_status(last_cleanup_pending=False, **cleanup_inventory(fresh.folder))
            from tow import snapshots

            observed = (snapshots.cleanup_status_unlocked if fresh.kind == "night" else restore_points.cleanup_status)()
            if observed.get("pending") is not False or observed.get("read_error") or observed.get("legacy"):
                _finish(fresh, operation, now, reason="observation_unconfirmed", success=False)
                return "observation_unconfirmed"
        _finish(fresh, operation, now, reason="complete" if plan.complete else "continued", success=True)
        return "complete" if plan.complete else "continued"


def run(*, now: float | None = None) -> dict[str, str]:
    """One automatic pass. Reads and previews never call it; no real messages are sent here."""
    if read_only_now():
        return {"manual": "read_only", "night": "read_only"}
    at = time.time() if now is None else now
    result = {}
    for kind in ("manual", "night"):
        context, operation = None, None
        try:
            context = _context(kind, load_config())
            operation = _reserve(context, at)
            if operation is None:
                result[kind] = "waiting_or_exhausted"
                continue
            plan = _plan(context)
            plan = _verify(plan)  # potentially large archive reads: never hold the data lock here
            result[kind] = _commit(plan, operation, at)
        except Exception as exc:  # noqa: BLE001 - cleanup must never stop the service or watchdog
            reason = str(exc) if isinstance(exc, Deferred) else "validation_failed"
            result[kind] = reason
            if context is not None and operation is not None:
                # Failure to record the failure is still unknown, never a reason to abort
                # the watchdog's independent service checks or clear the original warning.
                with suppress(Exception), persistence_lock():
                    _finish(context, operation, at, reason=reason, success=False)
            else:
                with suppress(Exception), persistence_lock():
                    state = _read()
                    previous = state.get(kind) or {}
                    if not previous.get("operation") and previous.get("reason") != reason:
                        state[kind] = {**previous, "reason": reason}
                        _write(state)
    return result
