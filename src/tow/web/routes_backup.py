"""Routes: copies of the data in Settings - night copies and their folders, restore points and the
portable bundle (export, check, restore)."""

from __future__ import annotations

import contextlib
import shutil
import tempfile
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from tow.bundle import MAX_BUNDLE_BYTES
from tow.clock import machine_now
from tow.log import error_class
from tow.paths import data_dir
from tow.restore_points import CREATE_FAILED, INVALID_FILE, ROLLBACK_FAILED, RestorePointError
from tow.store import StoreCorruptionError
from tow.web import services
from tow.web.text import format_bytes, t
from tow.web.views import flash_redirect

router = APIRouter()


def _backup_redirect(message: Any, kind: str = "ok", /, **params: Any) -> RedirectResponse:
    return flash_redirect("/settings?open=transfer", message, kind, **params)


def _restore_outcome(key: str, result: dict[str, Any]) -> tuple[str, str]:
    message = t(key)
    warnings = []
    if result.get("log_recorded") is False:
        warnings.append(t("web.settings.audit_missing"))
    if result.get("cleanup_warning"):
        warnings.append(t("backup.restore_point.restore_cleanup_warning"))
    return "; ".join([message, *warnings]), "warn" if warnings else "ok"


@router.post("/settings/backup/location")
@services.locked_state_mutation
def settings_backup_location(kind: str = Form(""), path: str = Form(""), action: str = Form("save")) -> Response:
    from tow.locations import LOCATIONS, check_writable, free_bytes, problem, resolve

    location = LOCATIONS.get(kind)
    if location is None:
        return _backup_redirect("web.backup.unknown_folder", "err")
    cfg = services.load_config()
    old = resolve(str(cfg.get(location.key) or ""), location)
    if action == "default":
        cfg.pop(location.key, None)
        services.save_config(cfg)
        services.log_event("settings_backup_location", location=kind, default=True, how="manual")
        new = resolve("", location)
        moved = t("web.backup.old_copies_stay", path=old) if new != old else ""
        return _backup_redirect(t("web.backup.folder_default", title=location.title, path=new, moved=moved))
    raw = path.strip().strip('"')
    if reason := problem(raw, location):
        return _backup_redirect("web.backup.folder_refused", "err", title=location.title, reason=reason)
    target = resolve(raw, location)
    if reason := check_writable(target):
        return _backup_redirect(f"{target}: {reason}", "err")
    free = free_bytes(target)
    room = t("web.backup.free", size=format_bytes(free)) if free is not None else ""
    if action == "check":
        return _backup_redirect(t("web.backup.write_works", path=target, room=room))
    if raw:
        cfg[location.key] = raw
    else:
        cfg.pop(location.key, None)
    services.save_config(cfg)
    services.log_event("settings_backup_location", location=kind, default=not raw, how="manual")
    moved = t("web.backup.old_copies_stay", path=old) if target != old else ""
    return _backup_redirect(t("web.backup.folder_set", title=location.title, path=target, room=room, moved=moved))


@router.post("/settings/backup/now")
def settings_backup_now() -> Response:
    from tow.snapshots import SnapshotError, create_snapshot

    try:
        result = create_snapshot(how="manual")
    except SnapshotError as exc:
        return _backup_redirect("web.backup.night_failed", "err", error=exc)
    message = t("web.backup.copy_made", name=result["snapshot"], size=format_bytes(result["bytes"]))
    if result.get("missing"):
        message += t("web.backup.copy_missing", names=", ".join(result["missing"]))
    if result.get("cleanup_warning"):
        message += f" · {t('backup.snapshot.cleanup_warning')}"
    return _backup_redirect(message, "warn" if result.get("missing") or result.get("cleanup_warning") else "ok")


@router.post("/settings/backup/night/{name}/restore")
def settings_backup_restore(name: str) -> Response:
    # A signed-in device on the network is the owner (owner's decision, 01.10.2026).
    from tow.snapshots import SnapshotError, restore_snapshot, snapshot_path

    try:
        result = restore_snapshot(snapshot_path(name), apply=True)
    except SnapshotError as exc:
        return _backup_redirect("web.backup.restore_failed", "err", error=exc)
    message = t("web.backup.restored", name=name, path=result["safety_copy"])
    if result.get("cleanup_warning"):
        return _backup_redirect(f"{message} · {t('backup.snapshot.restore_cleanup_warning')}", "warn")
    return _backup_redirect(message)


@router.post("/settings/restore-points")
def settings_restore_point_create() -> Response:
    try:
        point = services.create_restore_point()
    except RestorePointError as exc:
        services.log_event("settings_restore_point_create_fail", error=error_class(exc), how="manual")
        return flash_redirect("/settings?open=transfer", "web.settings.point_failed", "err", error=exc)
    services.log_event("settings_restore_point_created", restore_point=point["id"], how="manual")
    if point.get("cleanup_warning"):
        return flash_redirect("/settings?open=transfer", "backup.restore_point.cleanup_warning", "warn")
    return flash_redirect("/settings?open=transfer", "web.settings.point_saved", "ok")


@router.post("/settings/restore-points/{point_id}/restore")
def settings_restore_point_apply(point_id: str) -> Response:
    try:
        result = services.restore_from_point(point_id)
    except RestorePointError as exc:
        services.log_event("settings_restore_point_apply_fail", error=error_class(exc), how="manual")
        # The restore and its undo both failed: the data may have changed, never "restore failed".
        failed = "web.settings.rollback_critical" if exc.kind == ROLLBACK_FAILED else "web.settings.restore_failed"
        return flash_redirect("/settings?open=transfer", failed, "err")
    services.log_event(
        "settings_restore_point_applied",
        restore_point=result["restored"],
        safety_point=result["safety_point"],
        how="manual",
    )
    # A restored check interval needs nothing more: `tow run` reads it from the config.
    return _backup_redirect(*_restore_outcome("web.settings.restored", result))


@router.post("/settings/portable/export")
def settings_portable_export() -> Response:
    temp_dir: Path | None = None
    try:
        temp_dir = Path(tempfile.mkdtemp(prefix="tow-browser-export-", dir=data_dir()))
        output = temp_dir / "tow-backup.towx"
        services.export_portable_bundle(output)
        services.log_event("settings_portable_export", how="manual")
    except (OSError, RestorePointError, StoreCorruptionError) as exc:
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)
        with contextlib.suppress(OSError):
            services.log_event("settings_portable_export_fail", error=error_class(exc), how="manual")
        return flash_redirect("/settings?open=transfer", "web.settings.export_failed", "err")
    filename = f"tow-backup-{machine_now():%Y%m%d-%H%M%S}.towx"
    return FileResponse(
        output,
        filename=filename,
        media_type="application/octet-stream",
        headers={"Cache-Control": "no-store"},
        background=BackgroundTask(shutil.rmtree, temp_dir, ignore_errors=True),
    )


@router.post("/settings/portable/import")
async def settings_portable_import(
    backup_file: Annotated[UploadFile, File()],
    operation: Annotated[str, Form()],
) -> Response:
    action = str(operation or "").strip().lower()
    if action not in {"check", "restore"}:
        return flash_redirect("/settings?open=transfer", "web.settings.unknown_action", "err")
    temp_dir: Path | None = None
    total = 0
    try:
        temp_dir = Path(tempfile.mkdtemp(prefix="tow-browser-import-", dir=data_dir()))
        upload_path = temp_dir / "uploaded.towx"
        with upload_path.open("xb") as handle:
            while chunk := await backup_file.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_BUNDLE_BYTES:
                    raise RestorePointError("uploaded backup is too large", kind=INVALID_FILE)
                handle.write(chunk)
        if total <= 0:
            raise RestorePointError("uploaded backup is empty", kind=INVALID_FILE)
        if action == "check":
            await run_in_threadpool(services.check_portable_bundle, upload_path)
            services.log_event("settings_portable_check", status="valid", how="manual")
            flash, kind = t("web.settings.file_ok"), "ok"
        else:
            result = await run_in_threadpool(services.restore_portable_bundle, upload_path)
            services.log_event(
                "settings_portable_restore",
                safety_point=result["safety_point"],
                status="restored",
                how="manual",
            )
            flash, kind = _restore_outcome("web.settings.restored_file", result)
    except (OSError, RestorePointError) as exc:
        services.log_event(
            "settings_portable_import_fail",
            operation=action,
            error=error_class(exc),
            how="manual",
        )
        # By what failed, never by the (translated) text: the critical "undo failed too" must
        # read as critical in every language.
        failure = getattr(exc, "kind", None)
        kind = "err"
        if failure == ROLLBACK_FAILED:
            flash = t("web.settings.rollback_critical")
        elif failure == INVALID_FILE:
            flash = t("web.settings.file_invalid")
        elif failure == CREATE_FAILED:
            flash = t("web.settings.safety_point_failed")
        elif temp_dir is None:
            flash = t("web.settings.file_stage_failed")
        elif isinstance(exc, OSError):
            flash = t("web.settings.file_unreadable")
        else:
            flash = t("web.settings.restore_failed")
    finally:
        try:
            await backup_file.close()
        finally:
            if temp_dir is not None:
                shutil.rmtree(temp_dir, ignore_errors=True)
    return flash_redirect("/settings?open=transfer", flash, kind)
