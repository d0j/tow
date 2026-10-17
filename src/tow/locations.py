"""Where backups go: the owner picks a folder (local, another drive or a network share), or the default.

A relative path is relative to the install (the folder of config.yaml), so the defaults move
with TOW. A network share is written as \\\\server\\share\\folder: scheduled tasks do not see
mapped drive letters reliably, UNC paths always work.
"""

from __future__ import annotations

import errno
import os
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tow.folders import is_windows_device_path, protected_kind, windows_name_problem
from tow.i18n import t
from tow.log import owner_language
from tow.paths import config_path, data_dir, expand_user
from tow.platform import is_windows


@dataclass(frozen=True)
class Location:
    key: str  # config.yaml key
    title_key: str  # catalog key of the folder's name (tow/locales)
    default: str  # relative to the install, or "" = inside the data folder
    data_subdir: str = ""  # the default when ``default`` is ""

    @property
    def title(self) -> str:
        """The folder's name in the current language ("night copies")."""
        return t(self.title_key, owner_language())


NIGHT = Location("backup_dir", "locations.night", "backup/night")
MANUAL = Location("restore_points_dir", "locations.manual", "", data_subdir="restore-points")
LOCATIONS = {"night": NIGHT, "manual": MANUAL}


def install_dir() -> Path:
    return config_path().resolve().parent


def resolve(value: str | None, location: Location) -> Path:
    raw = str(value or "").strip().strip('"')
    if not raw:
        if location.default:
            return install_dir() / location.default
        return data_dir() / location.data_subdir
    path = expand_user(raw)
    return path if path.is_absolute() else install_dir() / path


class LocationError(ValueError):
    """A folder in config.yaml that cannot be used on this system; the text says why."""


def resolve_checked(value: str | None, location: Location) -> Path:
    """Apply the Settings folder policy again when a configured path is actually used."""
    raw = str(value or "").strip().strip('"')
    if raw and _of_another_system(raw):
        raise LocationError(
            t("locations.other_system_config", owner_language(), title=location.title, key=location.key, value=raw)
        )
    if reason := problem(raw, location):
        raise LocationError(reason)
    return resolve(raw, location)


def is_default(value: str | None, location: Location) -> bool:
    raw = str(value or "").strip()
    return not raw or raw.replace("\\", "/").strip("/") == location.default


def problem(value: str, location: Location) -> str | None:
    """Why the folder cannot be used (syntax and place), or None."""
    raw = str(value or "").strip().strip('"')
    if not raw:
        return None
    if any(ord(ch) < 32 for ch in raw) or is_windows_device_path(raw):
        return t("locations.bad_chars", owner_language())
    if _DRIVE_RELATIVE.match(raw):
        return t("locations.drive_relative", owner_language())
    if is_windows() and (kind := windows_name_problem(raw)) is not None:
        return t(_WINDOWS_NAME_TEXT[kind], owner_language())
    if ".." in Path(raw.replace("\\", "/")).parts:
        return t("locations.dotdot", owner_language())
    if _of_another_system(raw):
        return t("locations.other_system", owner_language())
    path = resolve(raw, location)
    # The same rule as for downloads (system, programs, AppData), as typed and as it resolves.
    kind = protected_kind(raw) or protected_kind(str(path))
    if kind is not None:
        return t("locations.protected" if kind == "windows" else "locations.protected_system", owner_language())
    if location is NIGHT:
        text = os.path.normcase(os.path.realpath(path))
        data = os.path.normcase(os.path.realpath(data_dir()))
        if text == data or text.startswith(data.rstrip(os.sep) + os.sep):
            return t("locations.night_outside_data", owner_language())
    return None


def is_network_share(value: str | None) -> bool:
    """A Windows network share (``\\\\server\\share\\…``, also written with ``/``): opening it signs
    in to that server with this account's Windows credentials."""
    raw = str(value or "").strip().strip('"')
    return is_windows() and raw.replace("/", "\\").startswith("\\\\")


def _of_another_system(raw: str) -> bool:
    """An absolute path written for another system: D:\\... or \\\\nas\\share on Linux and macOS (it
    would become a folder named "D:\\..." inside the install), /mnt/... on Windows (a folder on
    the current drive)."""
    looks_absolute = bool(_ABSOLUTE.match(raw))
    return looks_absolute and not expand_user(raw).is_absolute()


_WINDOWS_NAME_TEXT = {
    "device": "locations.windows_device",
    "stream": "locations.windows_stream",
    "admin_share": "locations.windows_admin_share",
}
_ABSOLUTE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|//|/)")
_DRIVE_RELATIVE = re.compile(r"^[A-Za-z]:(?![\\/])")


def check_writable(path: Path) -> str | None:
    """Create the folder if needed and write/delete a probe file; the reason when it fails."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".tow-write-test-{uuid.uuid4().hex[:8]}"
        probe.write_bytes(b"ok")
        probe.unlink()
    except PermissionError:
        return t("locations.no_rights", owner_language())
    except FileNotFoundError:
        return t("locations.drive_unavailable", owner_language())
    except OSError as exc:
        # In words of the page's language, never the system's own (English) error text.
        return t(_OS_ERROR_KEYS.get(exc.errno or 0, "locations.folder_unavailable"), owner_language())
    return None


# EINVAL is also what Windows says for a name with < > : " | ? * (ERROR_INVALID_NAME).
_OS_ERROR_KEYS = {
    errno.EINVAL: "locations.bad_name",
    errno.ENAMETOOLONG: "locations.name_too_long",
    errno.ENOSPC: "locations.no_space",
    errno.EROFS: "locations.no_rights",
}


# state.json: {"night" | "manual": {"path", "id"[, "unseen_at"]}} - the volume each backup folder
# was on when a copy was last written and read back there (tow.folders.recorded_volume), and since
# when it is missing or another one (said in Needs attention until a copy is written there again).
VOLUMES_KEY = "backup_volumes"


def kind_of(location: Location) -> str:
    """``night`` or ``manual``: the location's name in LOCATIONS."""
    return next(kind for kind, known in LOCATIONS.items() if known is location)


def recorded_volume(state: Mapping[str, Any], location: Location, path: Path) -> str | None:
    """The volume recorded for the backup folder ``path`` (None: none for this folder)."""
    from tow.folders import recorded_volume as recorded

    volumes = state.get(VOLUMES_KEY)
    entry = volumes.get(kind_of(location)) if isinstance(volumes, dict) else None
    return recorded(entry, str(path))


def volume_unseen(state: Mapping[str, Any], location: Location, path: Path) -> bool:
    """The backup folder has a recorded volume and is not on it now (unmounted, another disk,
    or the volume cannot be told): nothing written there would be the backup, nothing free there
    is its space. Read-only."""
    from tow.folders import volume_at

    expected = recorded_volume(state, location, path)
    return expected is not None and volume_at(str(path)) != expected


def refuse_unseen_volume(location: Location, path: Path) -> None:
    """Before a copy is written (the data lock held): ``LocationError`` when the folder is not on
    its recorded volume, and the state notes since when (Needs attention says it). Nothing is
    created or written in the folder: a copy on the disk below an unmounted one is no copy."""
    import time

    from tow.store import save_state

    state = _state_as_it_is()
    if state is None or not volume_unseen(state, location, path):
        return
    entry = state[VOLUMES_KEY][kind_of(location)]
    if isinstance(entry, dict) and not isinstance(entry.get("unseen_at"), int):
        entry["unseen_at"] = int(time.time())
        save_state(state)
    raise LocationError(t("locations.volume_missing", owner_language(), title=location.title))


def remember_volume(location: Location, path: Path) -> None:
    """After a copy was written and read back in ``path`` (the data lock held): its volume is
    recorded when none is for this folder (a moved folder records anew), and a note that it was
    missing goes. A recorded volume is never replaced here: only the owner's explicit accept does
    that (``accept_volume``). A failure here leaves the verified copy as it is."""
    from tow.folders import volume_at
    from tow.store import save_state

    state = _state_as_it_is()
    if state is None:
        return
    volumes = state.setdefault(VOLUMES_KEY, {})
    if not isinstance(volumes, dict):
        return
    kind = kind_of(location)
    entry = volumes.get(kind)
    if recorded_volume(state, location, path) is None:
        identity = volume_at(str(path))
        if not identity:
            return
        volumes[kind] = {"path": str(path), "id": identity}
    elif isinstance(entry, dict) and "unseen_at" in entry:
        entry.pop("unseen_at")
    else:
        return
    save_state(state)


def _state_as_it_is() -> dict[str, Any] | None:
    """state.json as it is, never quarantined or repaired here (a copy that fails on a damaged
    store leaves it as it was); None when it cannot be read."""
    from tow.store import StoreCorruptionError, load_state

    try:
        return load_state(quarantine=False)
    except OSError, ValueError, StoreCorruptionError:
        return None


def _recorded_for(path: Path, state: Mapping[str, Any] | None = None) -> str | None:
    """The volume recorded for whichever backup folder ``path`` is, read without writing (from
    ``state`` when the caller has it)."""
    state = state if state is not None else _state_as_it_is()
    if state is None:
        return None
    for location in LOCATIONS.values():
        if (expected := recorded_volume(state, location, path)) is not None:
            return expected
    return None


def free_bytes(path: Path, *, state: Mapping[str, Any] | None = None) -> int | None:
    """Free space where a copy would go; None when it cannot be known - also for a backup folder
    that is not on its recorded volume (the disk below an unmounted one is not its space).
    ``state``: the state the caller already read (else it is read here)."""
    import shutil

    from tow.folders import volume_at

    expected = _recorded_for(path, state)
    if expected is not None and volume_at(str(path)) != expected:
        return None
    probe = path
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return None
