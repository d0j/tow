"""What a night copy holds and where each member goes.

The fixed stores and this install's restore points (``restore-points/<id>.towx``), the
target of a member (refusing every name TOW does not write itself), the copy's own
config.yaml, and the checks that a member, a record or a copy folder is a plain file or
folder holding only what it should.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from tow.config import load_config
from tow.i18n import t
from tow.log import log_path, owner_language
from tow.paths import config_path, download_history_path, state_path
from tow.platform import plain_kind
from tow.snapshots.errors import SnapshotError
from tow.store import RegularReadError, encrypted_secrets_path, read_regular_bytes, secret_undo_path
from tow.yaml_guard import YamlLimitError
from tow.yaml_guard import load as load_yaml

POINT_MEMBER = re.compile(r"restore-points/(?P<id>[A-Za-z0-9-]{1,64})\.towx")
VALIDATED_MEMBERS = ("config.yaml", "state.json", "download_history.json", "secrets.enc", "secrets-undo.enc")
# Copied for the record, never restored: History keeps what happened after the copy was made.
NOT_RESTORED = "tow.jsonl"


def fixed_members() -> list[tuple[str, Path]]:
    """(name inside the snapshot, live path) of the stores every copy carries."""
    return [
        ("state.json", state_path()),
        ("download_history.json", download_history_path()),
        ("secrets.enc", encrypted_secrets_path()),
        ("secrets-undo.enc", secret_undo_path()),
        ("tow.jsonl", log_path()),
        ("config.yaml", config_path()),
    ]


def live_members() -> list[tuple[str, Path]]:
    """(name inside the snapshot, live path) - the allowlist, with this install's restore points."""
    members = fixed_members()
    from tow.restore_points import RestorePointError, list_restore_points, point_path

    try:
        members.extend(
            (f"restore-points/{point['id']}.towx", point_path(point["id"])) for point in list_restore_points()
        )
    except RestorePointError as exc:  # restore_points_dir of another system: said, not skipped
        raise SnapshotError(str(exc)) from exc
    return members


def points_dir_setting(copy_config: bytes | None = None) -> str:
    """``restore_points_dir`` of the live config.

    A live config that cannot be read (replacing it is often the point of a restore) says
    nothing: the copy's own setting is used, else the default folder.
    """
    try:
        cfg = load_config()
    except OSError, UnicodeError, TypeError, ValueError, yaml.YAMLError:
        cfg = parse_copy_config(copy_config) if copy_config is not None else {}
    return str(cfg.get("restore_points_dir") or "")


def member_target(name: str, *, points_dir: str | None = None) -> Path:
    """Where member ``name`` of a copy goes, refusing every name TOW does not write itself.

    A restore point is accepted only under its real id pattern, and its target must stay
    directly inside the restore points folder (``restore-points/\\Windows\\...`` or
    ``restore-points//Users/...`` would otherwise escape it). ``points_dir`` is that
    folder's setting (by default the live one, see ``points_dir_setting``).
    """
    fixed = dict(fixed_members())
    if name in fixed:
        return fixed[name]
    lang = owner_language()
    match = POINT_MEMBER.fullmatch(name) if isinstance(name, str) else None
    if match is None:
        if isinstance(name, str) and name.startswith("restore-points/"):
            raise SnapshotError(t("backup.snapshot.unsafe_path", lang, name=name))
        raise SnapshotError(t("backup.snapshot.unexpected_file", lang, name=name))
    from tow.restore_points import RestorePointError, point_path, restore_points_dir

    cfg = {"restore_points_dir": points_dir_setting() if points_dir is None else points_dir}
    try:
        target = point_path(match.group("id"), must_exist=False, cfg=cfg)
        inside = target.resolve().parent == restore_points_dir(cfg=cfg).resolve()
    except (RestorePointError, OSError) as exc:
        raise SnapshotError(t("backup.snapshot.unsafe_path", lang, name=name)) from exc
    if not inside:
        raise SnapshotError(t("backup.snapshot.unsafe_path", lang, name=name))
    return target


def parse_copy_config(content: bytes) -> dict[str, Any]:
    try:
        parsed = load_yaml(content.decode("utf-8"))
    except YamlLimitError as exc:
        raise SnapshotError(str(exc)) from exc
    except (UnicodeError, yaml.YAMLError) as exc:
        raise SnapshotError(t("backup.snapshot.config_not_mapping", owner_language())) from exc
    if parsed is None:
        parsed = {}
    if not isinstance(parsed, dict):
        raise SnapshotError(t("backup.snapshot.config_not_mapping", owner_language()))
    return parsed


def copy_members_in(folder: Path) -> set[str]:
    """The member names a copy of TOW's stores may hold, with the restore points present in it."""
    names = {name for name, _path in fixed_members()}
    if ordinary_directory(folder / "restore-points", missing=True):
        names.update(
            member
            for point in (folder / "restore-points").iterdir()
            if POINT_MEMBER.fullmatch(member := f"restore-points/{point.name}")
        )
    return names


def owned_tree(folder: Path, expected: set[str]) -> bool:
    """No foreign file, special file or link may be swept up with an owned copy."""
    ordinary_directory(folder)
    for path in folder.iterdir():
        if path.name == "restore-points":
            ordinary_directory(path)
            for point in path.iterdir():
                if f"restore-points/{point.name}" not in expected:
                    return False
                ordinary_file(point)
        elif path.name in expected:
            ordinary_file(path)
        else:
            return False
    return True


def ordinary_directory(path: Path, *, missing: bool = False) -> bool:
    kind = plain_kind(path, missing_ok=missing)
    if kind is None:
        return False
    if kind != "dir":
        raise ValueError("night-copy restore directory is unsafe")
    return True


def ordinary_file(path: Path, *, missing: bool = False) -> bool:
    kind = plain_kind(path, missing_ok=missing)
    if kind is None:
        return False
    if kind != "file":
        raise ValueError("night-copy restore file is unsafe")
    return True


def read_metadata_bytes(path: Path, *, limit: int) -> bytes:
    """Bound a regular opened record before allocating or interpreting its contents."""
    try:
        ordinary_file(path)
    except ValueError as exc:
        raise ValueError(t("backup.snapshot.record_type", owner_language())) from exc
    try:
        return read_regular_bytes(path, limit)
    except RegularReadError as exc:
        raise ValueError(t(_RECORD_REFUSED[exc.kind], owner_language())) from None


_RECORD_REFUSED = {
    "type": "backup.snapshot.record_type",
    "size": "backup.snapshot.record_size",
    "changed": "backup.snapshot.record_changed",
}
