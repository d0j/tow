"""Where the night copies are: the copies folder, the copies in it and one copy by name."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from tow.config import load_config
from tow.i18n import t
from tow.log import owner_language
from tow.paths import data_dir
from tow.platform import child_path
from tow.snapshots.errors import SnapshotError
from tow.snapshots.manifest import member_size, read_manifest

PREFIX = "tow-"


def backup_root(cfg: dict[str, Any] | None = None) -> Path:
    """The night copies folder: ``backup_dir`` from config.yaml, by default ``backup/night``
    next to config.yaml (moves with the install); a network share (\\\\server\\share) works too.
    A path of another system (a Windows config on Linux) is refused, never created."""
    from tow.locations import NIGHT, LocationError, resolve_checked

    value = str((cfg if cfg is not None else load_config()).get("backup_dir") or "").strip()
    try:
        root = resolve_checked(value, NIGHT)
    except LocationError as exc:
        raise SnapshotError(str(exc)) from exc
    data = data_dir().resolve()
    if root.resolve() == data or data in root.resolve().parents:
        raise SnapshotError(t("backup.snapshot.inside_data", owner_language()))
    return root


def snapshot_folders(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(
        p
        for p in root.iterdir()
        if p.is_dir()
        and not p.is_symlink()
        and not p.is_junction()
        and p.name.startswith(PREFIX)
        and (p / "MANIFEST.json").is_file()
    )


def list_snapshots(limit: int | None = 10) -> list[dict[str, Any]]:
    """Newest night copies first: name, time, size (no verification)."""
    try:
        root = backup_root()
        folders = snapshot_folders(root)
    except SnapshotError, OSError:
        return []
    rows = []
    for folder in reversed(folders if limit is None else folders[-limit:]):
        try:
            manifest = read_manifest(folder)
            created = datetime.fromisoformat(str(manifest.get("created_at")))
            created_ts = created.timestamp()
            size = sum(member_size(name, meta) for name, meta in manifest["files"].items())
        except SnapshotError, OSError, ValueError, TypeError, AttributeError, OverflowError:
            continue
        rows.append(
            {
                "name": folder.name,
                "created_ts": created_ts,
                "created_at": created.isoformat(),
                "bytes": size,
                "version": manifest.get("version"),
            }
        )
    return rows


def snapshot_path(name: str) -> Path:
    """A night copy by name, refusing anything that is not one (no paths, no ..)."""
    if not name.startswith(PREFIX) or not all(ch.isalnum() or ch in "-_" for ch in name):
        raise SnapshotError(t("backup.snapshot.unknown", owner_language()))
    root = backup_root()
    try:
        path = child_path(root, name)
    except ValueError:
        raise SnapshotError(t("backup.snapshot.unknown", owner_language())) from None
    if path.is_symlink() or path.is_junction() or path.resolve().parent != root.resolve():
        raise SnapshotError(t("backup.snapshot.unknown", owner_language()))
    if not (path / "MANIFEST.json").is_file():
        raise SnapshotError(t("backup.snapshot.unknown", owner_language()))
    return path
