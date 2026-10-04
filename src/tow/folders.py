from __future__ import annotations

import ntpath
import os
import posixpath
import re
from typing import Any

from tow import platform
from tow.i18n import t
from tow.log import owner_language
from tow.platform import posix, windows

_DRIVE = re.compile(r"^([A-Za-z]:)\\+([^\\]+)")


def save_root(path: str) -> str:
    """D:\\TV\\Show\\S01 → D:\\TV, /srv/media/Show → /srv/media. Not the full path."""
    raw = (path or "").strip().strip('"')
    if raw.startswith("/") and not _windows_shaped(raw):
        parts = [part for part in raw.split("/") if part]
        return "/" + "/".join(parts[:2])
    raw = raw.replace("/", "\\")
    if not raw:
        return ""
    if raw.startswith("\\\\"):
        parts = [p for p in raw.split("\\") if p]
        if len(parts) >= 2:
            return "\\\\" + parts[0] + "\\" + parts[1]
        return raw.rstrip("\\")
    m = _DRIVE.match(raw)
    if m:
        return f"{m.group(1)}\\{m.group(2)}"
    return raw.rstrip("\\")


def _root_key(root: str) -> str:
    """How two save roots compare when the list is de-duplicated: a Windows path ignores case
    (as Windows does); a POSIX path only where the system does - macOS by default - and
    exactly on Linux (and from Windows, where it is a Linux client's: a container, a NAS)."""
    if _windows_shaped(root):
        return ntpath.normcase(ntpath.normpath(root))
    root = posixpath.normpath(root)
    return root.casefold() if platform.current().name == "macos" else root


def recent_save_roots(state: dict[str, Any], *, n: int = 10) -> list[str]:
    """Most recently used full folders, retaining older root-only history without rewriting it."""
    if n <= 0:
        return []
    stored = state.get("save_roots") or []
    candidates = [*stored, *(topic.get("save_path") for topic in reversed(state.get("topics") or []))]
    seen: list[str] = []
    keys: set[str] = set()
    for value in candidates:
        root = str(value or "").strip()
        if not root or _root_key(root) in keys:
            continue
        keys.add(_root_key(root))
        seen.append(root)
        if len(seen) >= n:
            break
    return seen


def resolve_save_path(path: str, state: dict[str, Any]) -> str:
    """Typed path wins. Empty → last entered root. Never invent a default folder."""
    raw = (path or "").strip()
    if raw:
        return raw
    roots = recent_save_roots(state)
    return roots[0] if roots else ""


_DRIVE_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")


def is_windows_device_path(path: str) -> bool:
    """Windows recognizes namespace prefixes with forward, back or mixed slashes.

    Refuse them before resolving a path: device namespaces can bypass ordinary folder rules.
    This also applies to a Windows client when TOW itself runs on another system.
    """
    return path.replace("/", "\\").startswith(("\\\\?\\", "\\\\.\\"))


def _protected_roots_by_kind() -> tuple[tuple[str, list[str], bool], ...]:
    """("windows" | "system", roots, fold case): this system's protected folders (tow.platform)
    and the other systems' defaults - a client on another machine may run either.

    Windows and program folders, AppData (incl. the Startup folder); on Linux and macOS the
    system folders and the folders programs start from (~/.config autostart, ~/Library).
    POSIX folders ignore case on macOS (its default volumes do) and from Windows (the client's
    system is not known: the safer reading), never on Linux, where /System is not /system.
    """
    backend = platform.current()
    own = backend.protected_folders()
    if backend.name == "windows":
        windows_roots, system_roots = own, list(posix.SYSTEM_FOLDERS)
    else:
        windows_roots, system_roots = windows.protected_folders(), own
    return (("windows", windows_roots, False), ("system", system_roots, backend.name != "linux"))


def _windows_shaped(path: str) -> bool:
    return bool(_DRIVE_ABSOLUTE.match(path)) or path.startswith(("\\\\", "//")) or "\\" in path


def _normalized(path: str, *, fold: bool = False) -> str:
    if _windows_shaped(path):
        return ntpath.normcase(ntpath.normpath(path))
    value = posixpath.normpath(path)
    return value.casefold() if fold else value


def _inside(path: str, root: str, *, fold: bool = False) -> bool:
    """``path`` is ``root`` or below it, compared the way the path's own system compares."""
    if _windows_shaped(path) != _windows_shaped(root):
        return False
    path, root = _normalized(path, fold=fold), _normalized(root, fold=fold)
    separator = "\\" if _windows_shaped(root) else "/"
    return path == root or path.startswith(root.rstrip("\\/") + separator)


_SHORT_NAME = re.compile(r"~\d")


def _ambiguous_component(path: str) -> str | None:
    """A Windows folder name that Windows itself reads as another one.

    ``AppData.`` and ``AppData `` are ``AppData`` (trailing dots and spaces are dropped), and
    ``APPDAT~1`` is the 8.3 short name of ``AppData``: compared as text they slip past the rule.
    """
    if not _windows_shaped(path):
        return None
    for part in re.split(r"[\\/]+", path):
        if not part or (len(part) == 2 and part[1] == ":"):
            continue
        if part.endswith((".", " ")) or _SHORT_NAME.search(part):
            return part
    return None


def _real_path(path: str) -> str | None:
    """The path as this PC resolves it (8.3 names, junctions, links), or None when it is not a
    path of this PC. The nearest existing folder is resolved; the rest is appended as typed."""
    local = os.path.isabs(path) and not path.startswith(("\\\\", "//"))
    if not local:
        return None  # a share (no network access here), or a path on the client's machine
    current, tail = os.path.normpath(path), []
    try:
        while not os.path.exists(current):
            parent = os.path.dirname(current)
            if parent == current:
                return None
            tail.append(os.path.basename(current))
            current = parent
        real = os.path.join(os.path.realpath(current), *reversed(tail))
    except OSError, ValueError:
        return None
    # macOS: /home, /Users ... resolve into the data volume (/System/Volumes/Data/home); that is the
    # same folder as /home, not a folder under /System.
    if real.startswith(_MAC_DATA_VOLUME):
        real = real[len(_MAC_DATA_VOLUME) - 1 :]
    return real


_MAC_DATA_VOLUME = "/System/Volumes/Data/"


def protected_kind(path: str) -> str | None:
    """The kind of protected folder ``path`` is in: "windows" (Windows, program, AppData folders,
    or a name Windows reads as another one),
    "system" (POSIX system folders) or None."""
    if _ambiguous_component(path) is not None:
        return "windows"
    return _protected_by(path)


def is_protected_folder(path: str) -> bool:
    """Windows, program, AppData or POSIX system folders - as typed or as this PC resolves the path."""
    return _ambiguous_component(path) is not None or _protected_by(path) is not None


def _protected_by(path: str) -> str | None:
    """ "windows" or "system" when ``path`` - as typed, or as this PC resolves it - is inside a
    protected folder (compared with each root as typed and as resolved too)."""
    if is_windows_device_path(path):
        return "windows"
    candidates = [path]
    if (real := _real_path(path)) is not None:
        candidates.append(real)
    # Any profile's AppData of a Windows client (C:\Users\<name>\AppData), also when TOW runs on
    # Linux or macOS and knows no Windows profile of its own.
    if any(_windows_shaped(candidate) and windows.is_profile_appdata(candidate) for candidate in candidates):
        return "windows"
    for kind, roots, fold in _protected_roots_by_kind():
        # The real folder of each root too: C:\Users\user\AppData may itself be a link elsewhere.
        resolved = [real_root for real_root in map(_real_path, roots) if real_root is not None]
        if any(_inside(candidate, root, fold=fold) for candidate in candidates for root in [*roots, *resolved]):
            return kind
    return None


def save_path_policy_problem(path: str) -> str | None:
    """Protect system folders equally for every owner device; history is not an allowlist."""
    if (name := _ambiguous_component(path)) is not None:
        return t("folders.ambiguous_name", owner_language(), name=name)
    if (kind := _protected_by(path)) is not None:
        return t("folders.protected" if kind == "windows" else "folders.protected_system", owner_language())
    return None


def save_path_problem(path: str, *, allow_unc: bool = False) -> str | None:
    """Why a client save path is refused, or None when it is acceptable.

    Accepted: drive-absolute Windows paths (``D:\\media``) and absolute POSIX paths
    (``/downloads`` for a remote or containerised client). UNC shares only when
    ``allow_unc_save_paths`` is enabled: a typed ``\\\\host\\share`` makes the client
    connect to that host with the service account.
    """
    value = str(path or "")
    if any(ord(ch) < 32 for ch in value):
        return t("folders.control_chars", owner_language())
    if is_windows_device_path(value):
        return t("folders.device_paths", owner_language())
    is_unc = value.startswith(("\\\\", "//"))
    if is_unc and not allow_unc:
        return t("folders.unc_off", owner_language())
    if not (is_unc or _DRIVE_ABSOLUTE.match(value) or value.startswith("/")):
        return t("folders.full_path", owner_language())
    if ".." in re.split(r"[\\/]+", value):
        return t("folders.dotdot", owner_language())
    return None


def seen_from_here(path: str) -> bool:
    """Whether this PC sees the drive, share or top folder of a client's save path.

    ``/downloads`` of a client in a container (no such folder here), a Windows path while TOW
    runs on Linux or macOS, a POSIX path while it runs on Windows, a missing drive letter: no.
    """
    value = str(path or "").strip()
    if not value:
        return False
    anchor = os.path.splitdrive(value)[0]  # a drive or share on Windows; always "" on POSIX
    if anchor:
        return os.path.exists(anchor + os.sep)
    if not os.path.isabs(value) or value.startswith(("\\\\", "//")):
        return False
    top = next((part for part in value.split("/") if part), "")
    return bool(top) and os.path.isdir("/" + top)


def _comparable(path: str) -> str:
    """A client's path as its own system compares it: Windows paths fold case and slashes."""
    value = (path or "").strip()
    if not value:
        return ""
    if _windows_shaped(value):
        return ntpath.normcase(ntpath.normpath(value))
    return posixpath.normpath(value)


def paths_equal(a: str, b: str) -> bool:
    """Whether two client paths name the same folder, whichever system TOW and the client run."""
    na, nb = _comparable(a), _comparable(b)
    return na == nb and bool(na)


def remember_save_root(state: dict[str, Any], path: str, *, n: int = 10) -> None:
    """Remember the exact full folder, not a guessed ancestor, newest first."""
    root = (path or "").strip()
    if not root:
        return
    key = _root_key(root)
    cur = [x for x in recent_save_roots(state, n=n) if _root_key(x) != key]
    state["save_roots"] = [root, *cur][:n]
