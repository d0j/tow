"""Bind a confirmed action to the selected regular copy, not merely its name."""

from __future__ import annotations

import hashlib
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tow.platform import is_plain_dir, is_plain_file


def cleanup_names(location: Path, candidate: Callable[[str], bool]) -> list[str]:
    """Candidate copy names in a cleanup folder, sorted; no contents read, no link followed.

    The names bind a cleanup observation to the copies it saw. They grant no deletion rights.
    """
    try:
        info = location.lstat()
    except FileNotFoundError:
        return []
    if not is_plain_dir(info):
        raise ValueError("cleanup folder is not a regular directory")
    return sorted(path.name for path in location.iterdir() if candidate(path.name))


def inventory_digest(names: list[str]) -> str:
    digest = hashlib.sha256()
    for name in names:
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def added_names(recorded: Any, digest: Any, observed: list[str]) -> set[str]:
    """Names that appeared since a cleanup observation; a copy removed by hand is no change.

    An observation of an earlier version records only the digest, which must match exactly.
    """
    if recorded is None:
        if digest != inventory_digest(observed):
            raise ValueError("cleanup observation belongs to an earlier inventory")
        return set()
    if not isinstance(recorded, list) or any(not isinstance(name, str) for name in recorded):
        raise ValueError("invalid cleanup inventory")
    return set(observed) - set(recorded)


def copy_revision(path: Path) -> str:
    """Metadata identity including location; refuse links and special files.

    The data lock serializes TOW's actions. This token additionally rejects a stale
    confirmation after a folder change, replacement or partial removal. It is not
    authentication or protection against a hostile process running as the owner.
    """
    digest = hashlib.sha256()
    digest.update(str(path.resolve()).encode("utf-8"))

    def add(member: Path, *, directory: bool = False) -> None:
        info = member.lstat()
        if not (is_plain_dir(info) if directory else is_plain_file(info)):
            raise ValueError("copy is not an ordinary file or directory")
        digest.update(
            repr((str(member), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)).encode(
                "utf-8"
            )
        )

    if stat.S_ISDIR(path.lstat().st_mode):
        add(path, directory=True)
        for member in sorted(path.iterdir()):
            if member.name == "restore-points" and stat.S_ISDIR(member.lstat().st_mode):
                add(member, directory=True)
                for point in sorted(member.iterdir()):
                    add(point)
            else:
                add(member)
    else:
        add(path)
    return digest.hexdigest()
