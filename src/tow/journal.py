"""A crash journal: copies of the stores a write changes, put back all at once.

A write that changes several stores first copies each one into its journal folder, then
publishes the marker: a manifest that records each copy's SHA-256, or that the store did not
exist. A folder without a marker was never in use, and is only removed. Whichever process takes
the data lock next puts the stores of an unfinished journal back: every store and every copy is
checked first - nothing is written before all of them pass - and then each store gets the
checked bytes (never reread) and is read back, or is removed. A finished journal loses its
marker first, so an interrupted cleanup leaves a folder that is simply removed again. Nothing in
the folder is followed or deleted unless it is a plain file with one of the journal's own names,
or a leftover temporary of one (``atomic_write_bytes``).

The owners keep their folder, their manifest format and their error type:
``tow.check_transaction`` (state and history of a check) and ``tow.site_journal`` (config,
state, secrets and secret undo of ``tow.store_transaction``).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tow.platform import is_link_like
from tow.store import atomic_write_bytes, decode_json_bytes

# What tempfile adds between the prefix and the suffix atomic_write_bytes() gives it (eight
# of these characters today), then that suffix.
_TEMPORARY = re.compile(r"[a-z0-9_]+\.tmp")


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def is_digest(value: Any) -> bool:
    """A SHA-256 as a manifest records it: 64 lowercase hex digits."""
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


@dataclass(frozen=True)
class Copy:
    """One store of a journal: ``name`` (in messages), where it lives, and its copy in the
    folder with the copy's SHA-256 - both None when the store did not exist."""

    name: str
    target: Path
    backup: str | None
    sha256: str | None


@dataclass(frozen=True)
class Journal:
    """One journal folder. ``error`` is the owner's exception type; ``label`` names the journal
    and ``store`` its stores in the messages ("check transaction", "check store")."""

    root: Path
    marker: str
    names: frozenset[str]  # every file the folder may hold, the marker included
    error: Callable[[str], Exception]
    label: str
    store: str

    def is_link(self, path: Path) -> bool:
        try:
            return is_link_like(path.lstat())
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise self.error(f"cannot inspect {self.label} path: {path.name}") from exc

    def exists(self) -> bool:
        """Whether the folder is there; a link in its place is refused."""
        if self.is_link(self.root):
            raise self.error(f"unsafe {self.label} directory: it must not be a symlink or reparse point")
        return self.root.exists()

    def _own_temporary(self, name: str) -> bool:
        """A leftover of an interrupted atomic_write_bytes() of one of the journal's own files:
        ``.<own name>.<random letters>.tmp``. Any other ``*.tmp`` is not the journal's, so it is
        never deleted (``check_folder`` refuses the folder instead)."""
        for own in self.names:
            prefix = f".{own}."
            if name.startswith(prefix) and _TEMPORARY.fullmatch(name[len(prefix) :]):
                return True
        return False

    def check_folder(self) -> None:
        """Only plain files with the journal's own names (or their temporaries) in a plain folder."""
        if not self.root.is_dir() or self.is_link(self.root):
            raise self.error(f"{self.label} directory is invalid")
        try:
            entries = list(self.root.iterdir())
        except OSError as exc:
            raise self.error(f"cannot inspect {self.label} directory") from exc
        if any(
            self.is_link(entry)
            or not entry.is_file()
            or (entry.name not in self.names and not self._own_temporary(entry.name))
            for entry in entries
        ):
            raise self.error(f"{self.label} directory contains an unexpected entry")

    def published(self) -> bool:
        return (self.root / self.marker).exists()

    def read_marker(self) -> Any:
        try:
            return decode_json_bytes((self.root / self.marker).read_bytes())
        except (OSError, UnicodeError, ValueError, RecursionError) as exc:
            raise self.error(f"{self.label} marker is unreadable") from exc

    def read(self, path: Path) -> bytes | None:
        """A store or a copy, None when it does not exist; never through a link."""
        if self.is_link(path):
            raise self.error(f"unsafe {self.store}, must not be a symlink: {path.name} (or reparse point)")
        if not path.exists():
            return None
        if not path.is_file():
            raise self.error(f"unsafe {self.store}, not a regular file: {path.name}")
        try:
            return path.read_bytes()
        except OSError as exc:
            raise self.error(f"cannot read {self.store}: {path.name}") from exc

    def verified(self, copies: Iterable[Copy]) -> list[tuple[Copy, bytes | None]]:
        """Every store readable, every copy there with its checksum: (copy, its bytes or None)."""
        self.check_folder()
        plan: list[tuple[Copy, bytes | None]] = []
        for copy in copies:
            self.read(copy.target)
            content = None
            if copy.backup is not None:
                content = self.read(self.root / copy.backup)
                if content is None:
                    raise self.error(f"cannot restore {self.store}: {copy.name}")
                if digest(content) != copy.sha256:
                    raise self.error(f"{self.label} backup checksum mismatch: {copy.backup}")
            plan.append((copy, content))
        return plan

    def restore(self, copies: Iterable[Copy]) -> None:
        """Put every store back as its copy says; nothing is written unless every one passes.
        The checked bytes are written: a copy reread later could hold something else."""
        for copy, content in self.verified(copies):
            try:
                if content is not None:
                    atomic_write_bytes(copy.target, content)
                else:
                    copy.target.unlink(missing_ok=True)
            except OSError as exc:
                raise self.error(
                    f"{self.label} recovery write failed, cannot restore {self.store}: {copy.name}"
                    if content is not None
                    else f"cannot remove new {self.store}: {copy.name}"
                ) from exc
            if self.read(copy.target) != content:
                raise self.error(f"{self.store} restore read-back mismatch: {copy.name}")

    def remove(self) -> None:
        """The marker first, then every file of the journal, then the folder."""
        self.check_folder()
        (self.root / self.marker).unlink(missing_ok=True)
        for entry in self.root.iterdir():
            if entry.name in self.names or self._own_temporary(entry.name):
                entry.unlink(missing_ok=True)
        try:
            self.root.rmdir()
        except OSError as exc:
            raise self.error(f"cannot remove {self.label} directory") from exc

    def recover(self, copies: Callable[[Any], list[Copy] | None]) -> None:
        """Put an unfinished journal back and remove it.

        ``copies`` checks the marker and returns its stores - None for a journal that committed
        (only its cleanup was interrupted: later writes to the stores are legitimate).
        """
        if not self.exists():
            return
        self.check_folder()
        if not self.published():
            self.remove()  # preparation never published the marker, or cleanup was interrupted
            return
        restore = copies(self.read_marker())
        if restore is not None:
            self.restore(restore)
        self.remove()
