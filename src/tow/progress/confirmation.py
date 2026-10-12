"""The disk confirmation: is a client's file inside its save folder, and there in full."""

from __future__ import annotations

import stat
from pathlib import Path, PurePath

# How long a file seen on disk counts as confirmed (see reconcile_topic).
CONFIRMATION_REUSE_SEC = 24 * 3600


def safe_relative_path(save_path: str, client_file_name: str) -> str | None:
    """The client's name of a file as a path inside its save folder, or None when it is not one.

    Decided from the text alone: it runs for every file of every torrent on every pass, and
    resolving each path woke sleeping drives and a NAS for nothing. A link inside the folder
    that leads out of it is caught where a file is looked at on disk (filesystem_confirmation).
    """
    raw = str(client_file_name or "").replace("\\", "/")
    if raw.startswith("/"):
        return None
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts) or ":" in parts[0]:
        return None
    folder = PurePath(save_path)
    # Each part must stay one plain name below the folder ("C:x" is a drive on Windows).
    if folder.joinpath(*parts).parts != (*folder.parts, *parts):
        return None
    return "/".join(parts)


def filesystem_confirmation(
    save_path: str, relative_path: str, expected_size: int | None, *, base: Path | None = None
) -> bool | None:
    from tow.folders import seen_from_here

    folder = Path(save_path)
    if not seen_from_here(save_path):
        return None  # /downloads of a remote client, or a drive this PC does not see
    try:
        base = base or folder.resolve()
        path = (base / Path(*relative_path.split("/"))).resolve()
        path.relative_to(base)
        details = path.stat()
        if not stat.S_ISREG(details.st_mode):
            return False
        return not (expected_size is not None and details.st_size < int(expected_size))
    except FileNotFoundError:
        return False
    except OSError, ValueError, TypeError:
        return None
