"""The disk confirmation: is a client's file inside its save folder, and there in full."""

from __future__ import annotations

import re
import stat
from pathlib import Path, PurePosixPath, PureWindowsPath

from tow import platform
from tow.folders import windows_shaped

# How long a file seen on disk counts as confirmed (see reconcile_topic).
CONFIRMATION_REUSE_SEC = 24 * 3600
_DRIVE_ABSOLUTE = re.compile(r"[A-Za-z]:/")


def safe_relative_path(save_path: str, client_file_name: str) -> str | None:
    """The client's name of a file as a path inside its save folder, or None when it is not one.

    Judged by the system of the save folder, not the one TOW runs on: under a Windows folder
    (``D:\\TV``, ``\\\\nas\\tv``) a colon in any part is a drive (``C:x``) or an alternate data
    stream (``a:b``); under a POSIX one (``/downloads`` of a NAS) it is an ordinary character
    (``Star Trek: Discovery``). An absolute name is never one inside the folder.

    Decided from the text alone: it runs for every file of every torrent on every pass, and
    resolving each path woke sleeping drives and a NAS for nothing. A link inside the folder
    that leads out of it is caught where a file is looked at on disk (filesystem_confirmation).
    """
    raw = str(client_file_name or "").replace("\\", "/")
    if raw.startswith("/") or _DRIVE_ABSOLUTE.match(raw):
        return None
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        return None
    windows = windows_shaped(save_path)
    if windows and any(":" in part for part in parts):
        return None
    folder = PureWindowsPath(save_path) if windows else PurePosixPath(save_path)
    # Each part must stay one plain name below the folder.
    if folder.joinpath(*parts).parts != (*folder.parts, *parts):
        return None
    return "/".join(parts)


def filesystem_confirmation(
    save_path: str,
    relative_path: str,
    expected_size: int | None,
    *,
    base: Path | None = None,
    seen: bool | None = None,
) -> bool | None:
    """Whether the file is in its folder in full (True), missing or short (False), or unknown
    (None). ``seen``: the caller already asked ``seen_from_here`` of this folder - a pass asks
    once, since a share, a sleeping NAS or a lost drive can take its timeout for each look."""
    from tow.folders import seen_from_here

    folder = Path(save_path)
    if not (seen_from_here(save_path) if seen is None else seen):
        return None  # /downloads of a remote client, or a drive this PC does not see
    try:
        # A client's file may be past Windows' 260 characters: the extended form before any look.
        base = base or platform.long_path(folder).resolve()
        path = platform.long_path(base / Path(*relative_path.split("/"))).resolve()
        platform.ordinary_path(path).relative_to(platform.ordinary_path(base))
        details = path.stat()
        if not stat.S_ISREG(details.st_mode):
            return False
        return not (expected_size is not None and details.st_size < int(expected_size))
    except FileNotFoundError:
        return False
    except OSError, ValueError, TypeError:
        return None
