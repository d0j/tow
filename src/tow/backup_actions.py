"""Bind a confirmed action to the selected regular copy, not merely its name."""

from __future__ import annotations

import hashlib
import stat
from pathlib import Path


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
        ordinary = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
        if not ordinary or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
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
