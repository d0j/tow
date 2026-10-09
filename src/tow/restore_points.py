from __future__ import annotations

import contextlib
import logging
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tow.backup_actions import added_names, cleanup_names, copy_revision, inventory_digest
from tow.bundle import (
    MAX_BUNDLE_BYTES,
    ExportImportError,
    export_bundle,
    import_bundle,
    rollback_import,
    verify_bundle,
)
from tow.config import load_config
from tow.diagnostic_json import encode_object, read_object
from tow.i18n import t
from tow.log import log_event, owner_language
from tow.paths import data_dir
from tow.platform import child_path, is_link_like, is_plain_file
from tow.store import SecretStoreError, atomic_write_text, derive_local_secret, persistence_lock

RESTORE_POINT_LIMIT = 10
_ID_RE = re.compile(r"^(?P<stamp>\d{8}T\d{6}Z)-(?P<nonce>[a-f0-9]{8})$")
_ACCESS_KEYS = ("bind", "port", "allow_lan")


# What failed, whatever the language of the message: callers choose their reaction (the web
# flash) by ``RestorePointError.kind``, never by matching the translated text.
RESTORE_FAILED = "restore_failed"  # nothing changed (or it was rolled back): current data kept
ROLLBACK_FAILED = "rollback_failed"  # the restore failed and undoing it failed too: data may have changed
INVALID_FILE = "invalid_file"  # the file or point did not pass the check; nothing changed
CREATE_FAILED = "create_failed"  # a restore point (also the safety point before a restore) was not created
EXPORT_FAILED = "export_failed"
MASTER_KEY = "master_key"
UNKNOWN_POINT = "unknown_point"  # unknown, unsafe or missing restore point
CANNOT_READ = "cannot_read"
DELETE_FAILED = "delete_failed"


class RestorePointError(RuntimeError):
    """Raised when a server-local restore point cannot be handled safely; ``kind`` says what failed."""

    def __init__(self, message: str = "", *, kind: str = RESTORE_FAILED, reason: str = "") -> None:
        super().__init__(message)
        self.kind = kind
        self.reason = reason  # why a file did not pass the check, in the owner's words ("" unknown)


# Why a file did not pass the check (tow.bundle.ExportImportError.reason), in the owner's words.
_INVALID_REASONS = {
    "not_tow": "backup.restore_point.reason_not_tow",
    "other_key": "backup.restore_point.reason_other_key",
    "too_large": "backup.restore_point.reason_too_large",
    "empty": "backup.restore_point.reason_empty",
    "damaged": "backup.restore_point.reason_damaged",
}


def invalid_file(message_key: str, reason: str) -> RestorePointError:
    """A file or point that did not pass the check: ``message_key`` and why, both translated."""
    lang = owner_language()
    because = t(_INVALID_REASONS.get(reason, _INVALID_REASONS["damaged"]), lang)
    return RestorePointError(t(message_key, lang), kind=INVALID_FILE, reason=because)


def _is_rollback_failure(exc: BaseException) -> bool:
    """A restore whose undo failed too: ours, or the import engine's own rollback failure."""
    if isinstance(exc, RestorePointError):
        return exc.kind == ROLLBACK_FAILED
    # tow.bundle's internal (untranslated) message; typed import errors are planned for 1.18.
    return isinstance(exc, ExportImportError) and "rollback also failed" in str(exc)


def restore_points_dir(*, cfg: dict[str, Any] | None = None) -> Path:
    """``restore_points_dir`` from config.yaml (local or network folder); by default data/restore-points.
    A path of another system (a Windows config on Linux) is refused, never created."""
    from tow.locations import MANUAL, LocationError, resolve_checked

    try:
        current = load_config() if cfg is None else cfg
        return resolve_checked(str(current.get("restore_points_dir") or ""), MANUAL)
    except LocationError as exc:
        raise RestorePointError(str(exc), kind=CREATE_FAILED) from exc


def _passphrase() -> str:
    try:
        return derive_local_secret("restore-points")
    except SecretStoreError as exc:
        raise RestorePointError(t("backup.restore_point.master_key", owner_language()), kind=MASTER_KEY) from exc


def _point_names(location: Path) -> list[str]:
    """Archive names, without reading or trusting their contents."""
    return cleanup_names(location, lambda name: name.endswith(".towx") and _ID_RE.fullmatch(name[:-5]) is not None)


def cleanup_status(*, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """A scoped observation, not permission to delete; None never means completion."""
    result: dict[str, Any] = {"pending": None, "read_error": False, "location": "", "legacy": False}
    try:
        location = restore_points_dir() if cfg is None else restore_points_dir(cfg=cfg)
        result["location"] = str(location.resolve())
        path = data_dir() / "restore-point-status.json"
        try:
            info = path.lstat()
        except FileNotFoundError:
            return result  # no observation yet, or an observation that disappeared
        if not is_plain_file(info):
            raise ValueError("cleanup record is not a regular file")
        state = read_object(path)
        if type(state.get("cleanup_pending")) is not bool or not isinstance(state.get("location"), str):
            raise ValueError("invalid cleanup observation")
        if not state["location"]:
            raise ValueError("missing cleanup location")
        if "inventory" in state and (
            not isinstance(state["inventory"], str) or re.fullmatch(r"[a-f0-9]{64}", state["inventory"]) is None
        ):
            raise ValueError("invalid cleanup inventory")
        if state["location"] == result["location"]:
            if "inventory" not in state:
                _point_names(location)  # legacy format cannot hide a real folder-access failure
                result["legacy"] = True
                if state["cleanup_pending"]:
                    result["pending"] = True  # retain a legacy warning, not legacy success
                return result
            # A point deleted by hand is no change; a new archive is not proven by its name alone.
            if added_names(state.get("names"), state["inventory"], _point_names(location)):
                raise ValueError("cleanup observation belongs to an earlier inventory")
            result["pending"] = state["cleanup_pending"]
    except OSError, ValueError, TypeError, UnicodeError, RecursionError, RestorePointError:
        result["read_error"] = True
    return result


def _record_cleanup(pending: bool, location: Path) -> None:
    if type(pending) is not bool:
        raise TypeError("cleanup observation must be boolean")
    # A monitoring write must not invalidate an already verified archive.
    with contextlib.suppress(OSError, ValueError, TypeError, UnicodeError, RecursionError):
        names = _point_names(location)
        atomic_write_text(
            data_dir() / "restore-point-status.json",
            encode_object(
                {
                    "cleanup_pending": pending,
                    "location": str(location.resolve()),
                    "inventory": inventory_digest(names),
                    "names": names,
                }
            ),
        )
    if pending:
        with contextlib.suppress(OSError):
            log_event("backup_cleanup_pending", copy_kind="restore_point", how="manual")


def _portable_passphrase() -> str:
    try:
        return derive_local_secret("browser-portable-bundles")
    except SecretStoreError as exc:
        raise RestorePointError(t("backup.restore_point.master_key", owner_language()), kind=MASTER_KEY) from exc


def _is_link(path: Path) -> bool:
    try:
        return is_link_like(path.lstat())
    except OSError:
        return False


def point_path(point_id: str, *, must_exist: bool = True, cfg: dict[str, Any] | None = None) -> Path:
    if not isinstance(point_id, str) or _ID_RE.fullmatch(point_id) is None:
        raise RestorePointError(t("backup.restore_point.unknown", owner_language()), kind=UNKNOWN_POINT)
    root = restore_points_dir(cfg=cfg).resolve()
    try:
        path = child_path(root, f"{point_id}.towx")
    except ValueError:
        raise RestorePointError(t("backup.restore_point.unsafe", owner_language()), kind=UNKNOWN_POINT) from None
    if path.parent.resolve() != root or _is_link(path):
        raise RestorePointError(t("backup.restore_point.unsafe", owner_language()), kind=UNKNOWN_POINT)
    if must_exist and (not path.is_file() or path.stat().st_size <= 0):
        raise RestorePointError(t("backup.restore_point.missing", owner_language()), kind=UNKNOWN_POINT)
    return path


def _point_view(path: Path) -> dict[str, Any] | None:
    match = _ID_RE.fullmatch(path.stem)
    if match is None or path.suffix != ".towx" or _is_link(path) or not path.is_file():
        return None
    try:
        size = path.stat().st_size
        created = datetime.strptime(match.group("stamp"), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except OSError, ValueError:
        return None
    try:
        revision = copy_revision(path)
    except OSError, ValueError:
        return None
    return {
        "id": path.stem,
        "created_at": created.isoformat(),
        "bytes": size,
        "revision": revision,
    }


def list_restore_points() -> list[dict[str, Any]]:
    root = restore_points_dir()
    if not root.is_dir() or _is_link(root):
        return []
    points = []
    try:
        candidates = list(root.iterdir())
    except OSError as exc:
        raise RestorePointError(t("backup.restore_point.cannot_read", owner_language()), kind=CANNOT_READ) from exc
    for path in candidates:
        view = _point_view(path)
        if view is not None:
            points.append(view)
    return sorted(points, key=lambda item: (str(item["created_at"]), str(item["id"])), reverse=True)


def _prune(*, protected: set[str]) -> None:
    """Counted by name (newest first); only a point about to be removed is decrypted and checked."""
    points = [point for point in list_restore_points() if str(point["id"]) not in protected and point["bytes"] > 0]
    for point in points[max(0, RESTORE_POINT_LIMIT - len(protected)) :]:
        path = point_path(str(point["id"]))
        try:
            verify_bundle(path, _passphrase())
        except ExportImportError:
            continue  # a foreign, unreadable or damaged copy is never ours to remove
        try:
            path.unlink()
            try:
                path.lstat()
            except FileNotFoundError:
                continue
            # A successful call is not proof of absence (network filesystems, replacements).
            # Never delete a replacement in a second attempt.
            raise RestorePointError(t("backup.restore_point.cannot_rotate", owner_language()), kind=CREATE_FAILED)
        except OSError as exc:
            raise RestorePointError(
                t("backup.restore_point.cannot_rotate", owner_language()), kind=CREATE_FAILED
            ) from exc


def create_restore_point(*, protected: set[str] | None = None) -> dict[str, Any]:
    with persistence_lock():
        return _create_restore_point(protected=protected)


def _create_restore_point(*, protected: set[str] | None = None) -> dict[str, Any]:
    root = restore_points_dir()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RestorePointError(
            t("backup.restore_point.cannot_create_dir", owner_language()), kind=CREATE_FAILED
        ) from exc
    if _is_link(root):
        raise RestorePointError(t("backup.restore_point.unsafe_dir", owner_language()), kind=CREATE_FAILED)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    point_id = f"{stamp}-{uuid.uuid4().hex[:8]}"
    path = point_path(point_id, must_exist=False)
    try:
        export_bundle(path, _passphrase(), include_log=False, overwrite=False)
        view = _point_view(path)
        if view is None or view["bytes"] <= 0:
            raise RestorePointError(t("backup.restore_point.read_back_failed", owner_language()), kind=CREATE_FAILED)
    except (ExportImportError, OSError, RestorePointError) as exc:
        # The exporter owns its failed writes. An existing or concurrently created
        # file at this name is not ours to delete.
        # Name the cause (OSError without its filename: no local paths in the UI flash). The
        # exporter wraps a system error (a full disk, a read-only folder, a file held open) in its
        # own English sentence: the system's words are found behind it.
        system = _system_error(exc)
        if system is not None:
            reason = system.strerror or type(system).__name__
        elif isinstance(exc, ExportImportError) and exc.reason == "too_large":
            reason = t("backup.restore_point.data_too_large", owner_language(), mib=MAX_BUNDLE_BYTES // 2**20)
        else:
            reason = str(exc)
        raise RestorePointError(
            t("backup.restore_point.cannot_create", owner_language(), reason=reason), kind=CREATE_FAILED
        ) from exc
    try:
        _prune(protected={point_id, *(protected or set())})
    except OSError, RestorePointError:
        # A verified copy remains usable even when an old copy cannot be removed.
        view["cleanup_warning"] = t("backup.restore_point.cleanup_warning", owner_language())
        logging.getLogger("tow.restore_points").warning("%s", view["cleanup_warning"])
    _record_cleanup(bool(view.get("cleanup_warning")), root)
    return view


def _system_error(exc: BaseException) -> OSError | None:
    """The ``OSError`` an error is or was raised from (its cause or context chain), if any."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, OSError):
            return current
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return None


def restore_from_point(point_id: str) -> dict[str, Any]:
    with persistence_lock():
        return _restore_from_point(point_id)


def check_restore_point(point_id: str) -> dict[str, Any]:
    with persistence_lock():
        try:
            return import_bundle(point_path(point_id), _passphrase(), apply=False)
        except ExportImportError as exc:
            raise invalid_file("backup.restore_point.validation_failed", exc.reason) from exc


def restore_point_delete_view(point_id: str) -> dict[str, Any]:
    with persistence_lock():
        view = _point_view(point_path(point_id, must_exist=False))
        if view is None:
            raise RestorePointError(t("backup.restore_point.unknown", owner_language()), kind=UNKNOWN_POINT)
        return view


def delete_restore_point(point_id: str, revision: str) -> dict[str, Any]:
    """Explicit deletion also permits a damaged archive, never a link or another name."""
    with persistence_lock():
        path = point_path(point_id, must_exist=False)
        before = cleanup_status()
        try:
            if not revision or copy_revision(path) != revision:
                raise RestorePointError(t("web.backup.delete_stale", owner_language()), kind=DELETE_FAILED)
            path.unlink()
            try:
                path.lstat()
            except FileNotFoundError:
                if type(before["pending"]) is bool:
                    _record_cleanup(before["pending"], path.parent)
                return {"ok": True, "deleted": point_id}
            # Do not retry: a replacement is not the selected copy.
        except OSError, ValueError:
            pass
        raise RestorePointError(t("web.backup.delete_failed", owner_language()), kind=DELETE_FAILED)


def _restore_from_point(point_id: str) -> dict[str, Any]:
    path = point_path(point_id)
    return _restore_bundle(path, _passphrase(), restored=point_id, protected={point_id})


def export_portable_bundle(output: Path) -> dict[str, Any]:
    with persistence_lock():
        try:
            return export_bundle(Path(output), _portable_passphrase(), include_log=False, overwrite=False)
        except ExportImportError as exc:
            raise RestorePointError(
                t("backup.restore_point.cannot_create_portable", owner_language()), kind=EXPORT_FAILED
            ) from exc


def check_portable_bundle(path: Path) -> dict[str, Any]:
    with persistence_lock():
        try:
            result = import_bundle(Path(path), _portable_passphrase(), apply=False)
        except ExportImportError as exc:
            raise invalid_file("backup.restore_point.portable_invalid", exc.reason) from exc
        if not result.get("ok") or not result.get("preview"):
            raise invalid_file("backup.restore_point.portable_check_failed", "damaged")
        return result


def restore_portable_bundle(path: Path) -> dict[str, Any]:
    with persistence_lock():
        return _restore_bundle(Path(path), _portable_passphrase(), restored="uploaded-file", protected=set())


def _restore_bundle(
    path: Path,
    passphrase: str,
    *,
    restored: str,
    protected: set[str],
) -> dict[str, Any]:
    access_before = {key: load_config().get(key) for key in _ACCESS_KEYS}
    try:
        preview = import_bundle(
            path,
            passphrase,
            apply=False,
            config_overrides=access_before,
            preserve_secret_keys={"lan_auth"},
        )
    except ExportImportError as exc:
        raise invalid_file("backup.restore_point.validation_failed", exc.reason) from exc
    if not preview.get("ok") or not preview.get("preview"):
        raise invalid_file("backup.restore_point.validation_failed", "damaged")
    safety_point = create_restore_point(protected=protected)
    try:
        applied = import_bundle(
            path,
            passphrase,
            apply=True,
            config_overrides=access_before,
            preserve_secret_keys={"lan_auth"},
        )
        if not applied.get("checkpoint"):  # Path("") would be "." and look like a checkpoint
            raise RestorePointError(t("backup.restore_point.no_checkpoint", owner_language()), kind=RESTORE_FAILED)
        checkpoint = Path(str(applied["checkpoint"]))
        read_back = load_config()
        if any(read_back.get(key) != value for key, value in access_before.items()):
            raise RestorePointError(t("backup.restore_point.lan_access_failed", owner_language()), kind=RESTORE_FAILED)
    except Exception as exc:
        checkpoint_value = locals().get("checkpoint")
        if isinstance(checkpoint_value, Path) and str(checkpoint_value):
            try:
                rollback_import(checkpoint_value, apply=True)
            except ExportImportError as rollback_exc:
                raise RestorePointError(
                    t("backup.restore_point.rollback_failed", owner_language()), kind=ROLLBACK_FAILED
                ) from rollback_exc
        if _is_rollback_failure(exc):
            raise RestorePointError(
                t("backup.restore_point.rollback_failed", owner_language()), kind=ROLLBACK_FAILED
            ) from exc
        if isinstance(exc, RestorePointError):
            raise
        raise RestorePointError(t("backup.restore_point.data_kept", owner_language()), kind=RESTORE_FAILED) from exc
    result = {
        "ok": True,
        "restored": restored,
        "safety_point": str(safety_point["id"]),
        "access_preserved": True,
        "checkpoint": str(applied["checkpoint"]),
        "log_recorded": applied.get("log_recorded") is True,
    }
    if safety_point.get("cleanup_warning"):
        result["cleanup_warning"] = safety_point["cleanup_warning"]
    return result


__all__ = [
    "RestorePointError",
    "check_portable_bundle",
    "check_restore_point",
    "cleanup_status",
    "create_restore_point",
    "delete_restore_point",
    "export_portable_bundle",
    "list_restore_points",
    "restore_from_point",
    "restore_point_delete_view",
    "restore_points_dir",
    "restore_portable_bundle",
]
