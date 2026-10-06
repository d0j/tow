"""Where backups go: the owner picks a folder (local, another drive or a network share), or the default.

A relative path is relative to the install (the folder of config.yaml), so the defaults move
with TOW. A network share is written as \\\\server\\share\\folder: scheduled tasks do not see
mapped drive letters reliably, UNC paths always work.
"""

from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from tow.folders import is_windows_device_path, protected_kind, windows_name_problem
from tow.i18n import t
from tow.log import owner_language
from tow.paths import config_path, data_dir
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
    path = Path(raw).expanduser()
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


def _of_another_system(raw: str) -> bool:
    """An absolute path written for another system: D:\\... or \\\\nas\\share on Linux and macOS (it
    would become a folder named "D:\\..." inside the install), /mnt/... on Windows (a folder on
    the current drive)."""
    looks_absolute = bool(_ABSOLUTE.match(raw))
    return looks_absolute and not Path(raw).expanduser().is_absolute()


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
        return t("locations.folder_unavailable", owner_language(), error=exc.strerror or type(exc).__name__)
    return None


def free_bytes(path: Path) -> int | None:
    import shutil

    probe = path
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return None
