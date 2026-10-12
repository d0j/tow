"""Bounded reads and atomic writes of whole files, and the count of files this process wrote.

A write goes to a temporary file beside the target (``.<name>.<random>.tmp``), is flushed to
disk and then replaces the target, under the data lock; a reader sees the old file or the new
one, never half of one.
"""

from __future__ import annotations

import os
import stat
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Literal

from tow.platform import current as current_platform
from tow.storage.errors import StoreCorruptionError


class RegularReadError(ValueError):
    """``read_regular_bytes`` refused the file. ``kind`` says why, whatever the caller makes of
    it: ``"type"`` (not a regular file), ``"size"`` (over the limit) or ``"changed"`` (another file
    or another size by the time it was read)."""

    def __init__(self, kind: Literal["type", "size", "changed"]) -> None:
        super().__init__(f"regular file refused: {kind}")
        self.kind = kind


def read_regular_bytes(path: Path, limit: int, *, same_as: os.stat_result | None = None) -> bytes:
    """The bytes of a regular file of at most ``limit`` bytes, through one open handle.

    The opened file (``fstat``) must be a regular file (else ``RegularReadError("type")``) of 0 to
    ``limit`` bytes ("size"); with ``same_as`` (the caller's earlier ``lstat``) it must also be that
    file, of that size ("changed"). Its size plus one byte is read - never ``limit`` bytes for a
    small file - so a file that grew or shrank meanwhile is "changed". An OSError from opening or
    reading is the caller's.
    """
    with path.open("rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise RegularReadError("type")
        if same_as is not None and (
            (info.st_dev, info.st_ino) != (same_as.st_dev, same_as.st_ino) or info.st_size != same_as.st_size
        ):
            raise RegularReadError("changed")
        if not 0 <= info.st_size <= limit:
            raise RegularReadError("size")
        raw = handle.read(info.st_size + 1)
    if len(raw) > limit:
        raise RegularReadError("size")
    if len(raw) != info.st_size:
        raise RegularReadError("changed")
    return raw


@contextmanager
def _writer_lock(path: Path) -> Iterator[None]:
    del path
    from tow.store import persistence_lock  # the data lock still lives in tow.store

    with persistence_lock():
        yield


def replace_with_retry(source: Path, target: Path, *, attempts: int = 10) -> None:
    """os.replace, retrying briefly while a reader has the target open (Windows sharing violation)."""
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(min(0.02 * 2**attempt, 1.0))  # 20 ms ... 1 s, ~4.3 s in total


_write_generation = 0


def write_generation() -> int:
    """Counts the files this process has written: a per-request cache (tow.web) that saw another
    number reads again, so a page rendered after a save in the same request shows the save."""
    return _write_generation


def atomic_write_bytes(path: Path, content: bytes) -> None:
    _atomic_write_bytes(path, content, overwrite=True)


def atomic_create_bytes(path: Path, content: bytes) -> None:
    """Publish complete bytes only if the name is free, never clobber another writer."""
    _atomic_write_bytes(path, content, overwrite=False)


def _atomic_write_bytes(path: Path, content: bytes, *, overwrite: bool) -> None:
    global _write_generation
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name = None
    with _writer_lock(path):
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{path.name}.",
                suffix=".tmp",
                dir=path.parent,
                delete=False,
            ) as handle:
                temporary_name = Path(handle.name)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            if overwrite:
                replace_with_retry(temporary_name, path)
            else:
                current_platform().publish_exclusive(temporary_name, path)
            _write_generation += 1  # after the replace, under the writer lock (one writer at a time)
            try:
                directory_fd = os.open(path.parent, os.O_RDONLY)
            except OSError:
                directory_fd = None
            if directory_fd is not None:
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            if temporary_name is not None:
                try:
                    temporary_name.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    pass


def atomic_write_text(path: Path, text: str) -> None:
    try:
        atomic_write_bytes(path, text.encode("utf-8"))
    except UnicodeError as exc:
        raise StoreCorruptionError(f"text payload is not UTF-8: {path.name}") from exc


def restrict_file(path: Path) -> None:
    """Owner read and write only (0600), where the OS supports it; a failure is ignored."""
    with suppress(OSError):
        path.chmod(0o600)
