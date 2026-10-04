from __future__ import annotations

import contextlib
import json
import logging
import re
import stat
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tow.bundle import ExportImportError, export_bundle, import_bundle, rollback_import, verify_bundle
from tow.config import load_config
from tow.i18n import t
from tow.log import log_event, owner_language
from tow.paths import data_dir
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


class RestorePointError(RuntimeError):
    """Raised when a server-local restore point cannot be handled safely; ``kind`` says what failed."""

    def __init__(self, message: str = "", *, kind: str = RESTORE_FAILED) -> None:
        super().__init__(message)
        self.kind = kind


def _is_rollback_failure(exc: BaseException) -> bool:
    """A restore whose undo failed too: ours, or the import engine's own rollback failure."""
    if isinstance(exc, RestorePointError):
        return exc.kind == ROLLBACK_FAILED
    # tow.bundle's internal (untranslated) message; typed import errors are planned for 1.18.
    return isinstance(exc, ExportImportError) and "rollback also failed" in str(exc)


def restore_points_dir() -> Path:
    """``restore_points_dir`` from config.yaml (local or network folder); by default data/restore-points.
    A path of another system (a Windows config on Linux) is refused, never created."""
    from tow.locations import MANUAL, LocationError, resolve_checked

    try:
        return resolve_checked(str(load_config().get("restore_points_dir") or ""), MANUAL)
    except LocationError as exc:
        raise RestorePointError(str(exc), kind=CREATE_FAILED) from exc


def _passphrase() -> str:
    try:
        return derive_local_secret("restore-points")
    except SecretStoreError as exc:
        raise RestorePointError(t("backup.restore_point.master_key", owner_language()), kind=MASTER_KEY) from exc


def cleanup_pending() -> bool:
    """Informational state only; it never grants permission to delete an archive."""
    try:
        state = json.loads((data_dir() / "restore-point-status.json").read_text(encoding="utf-8"))
        return (
            isinstance(state, dict)
            and state.get("cleanup_pending") is True
            and state.get("location") == str(restore_points_dir().resolve())
        )
    except OSError, ValueError, UnicodeError, RecursionError, RestorePointError:
        return False


def _record_cleanup(pending: bool, location: Path) -> None:
    # A monitoring write must not invalidate an already verified archive.
    with contextlib.suppress(OSError):
        atomic_write_text(
            data_dir() / "restore-point-status.json",
            json.dumps({"cleanup_pending": pending, "location": str(location.resolve())}),
        )
    if pending:
        with contextlib.suppress(OSError):
            log_event("backup_cleanup_pending", copy_kind="restore_point", how="manual")


def _portable_passphrase() -> str:
    try:
        return derive_local_secret("browser-portable-bundles")
    except SecretStoreError as exc:
        raise RestorePointError(t("backup.restore_point.master_key", owner_language()), kind=MASTER_KEY) from exc


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)  # Windows only
    except OSError:
        return False
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def point_path(point_id: str, *, must_exist: bool = True) -> Path:
    if not isinstance(point_id, str) or _ID_RE.fullmatch(point_id) is None:
        raise RestorePointError(t("backup.restore_point.unknown", owner_language()), kind=UNKNOWN_POINT)
    root = restore_points_dir().resolve()
    path = root / f"{point_id}.towx"
    if path.parent.resolve() != root or path.is_symlink() or _is_reparse_point(path):
        raise RestorePointError(t("backup.restore_point.unsafe", owner_language()), kind=UNKNOWN_POINT)
    if must_exist and (not path.is_file() or path.stat().st_size <= 0):
        raise RestorePointError(t("backup.restore_point.missing", owner_language()), kind=UNKNOWN_POINT)
    return path


def _point_view(path: Path) -> dict[str, Any] | None:
    match = _ID_RE.fullmatch(path.stem)
    if match is None or path.suffix != ".towx" or path.is_symlink() or _is_reparse_point(path) or not path.is_file():
        return None
    try:
        size = path.stat().st_size
        created = datetime.strptime(match.group("stamp"), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except OSError, ValueError:
        return None
    if size <= 0:
        return None
    return {
        "id": path.stem,
        "created_at": created.isoformat(),
        "bytes": size,
    }


def list_restore_points() -> list[dict[str, Any]]:
    root = restore_points_dir()
    if not root.is_dir() or root.is_symlink() or _is_reparse_point(root):
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
    points = list_restore_points()
    keep = 0
    for point in points:
        point_id = str(point["id"])
        if point_id in protected:
            continue
        path = point_path(point_id)
        try:
            verify_bundle(path, _passphrase())
        except ExportImportError:
            continue  # a foreign, unreadable or damaged copy is never ours to remove
        keep += 1
        if keep <= RESTORE_POINT_LIMIT - len(protected):
            continue
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
    if root.is_symlink() or _is_reparse_point(root):
        raise RestorePointError(t("backup.restore_point.unsafe_dir", owner_language()), kind=CREATE_FAILED)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    point_id = f"{stamp}-{uuid.uuid4().hex[:8]}"
    path = point_path(point_id, must_exist=False)
    try:
        export_bundle(path, _passphrase(), include_log=False, overwrite=False)
        view = _point_view(path)
        if view is None:
            raise RestorePointError(t("backup.restore_point.read_back_failed", owner_language()), kind=CREATE_FAILED)
    except (ExportImportError, OSError, RestorePointError) as exc:
        # The exporter owns its failed writes. An existing or concurrently created
        # file at this name is not ours to delete.
        # Name the cause (OSError without its filename: no local paths in the UI flash).
        reason = (exc.strerror or type(exc).__name__) if isinstance(exc, OSError) else str(exc)
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


def restore_from_point(point_id: str) -> dict[str, Any]:
    with persistence_lock():
        return _restore_from_point(point_id)


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
            raise RestorePointError(
                t("backup.restore_point.portable_invalid", owner_language()), kind=INVALID_FILE
            ) from exc
        if not result.get("ok") or not result.get("preview"):
            raise RestorePointError(
                t("backup.restore_point.portable_check_failed", owner_language()), kind=INVALID_FILE
            )
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
        raise RestorePointError(
            t("backup.restore_point.validation_failed", owner_language()), kind=INVALID_FILE
        ) from exc
    if not preview.get("ok") or not preview.get("preview"):
        raise RestorePointError(t("backup.restore_point.validation_failed", owner_language()), kind=INVALID_FILE)
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
    "cleanup_pending",
    "create_restore_point",
    "export_portable_bundle",
    "list_restore_points",
    "restore_from_point",
    "restore_points_dir",
    "restore_portable_bundle",
]
