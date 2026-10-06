"""Recovery of an interrupted store transaction, in any TOW process.

A site edit, a settings save or an undo (``tow.store_transaction``) writes config.yaml,
state.json, the secrets and the secret undo snapshot together, behind a journal: a copy of all
four is taken first, and an interrupted transaction is rolled back to that copy (the format and
the folder keep their first name, "site transaction", so older and newer versions read each
other's journal). The recovery used to run only from
web requests, so a scheduled check (or the watchdog, a night copy) started after a crash
read half-written stores. It is now a persistence recovery hook, like the check and import
journals: every process restores the stores before it takes the data lock for itself
(``tow.store`` registers ``recover_site_journal``, so no process can miss it).

Fails closed: a damaged journal raises ``RuntimeError`` (the web answers 503) instead of
letting anyone work on stores that may not belong together.
"""

from __future__ import annotations

import contextlib
import hashlib
from pathlib import Path
from typing import Any

from tow.paths import config_path, secrets_path, state_path
from tow.platform import is_link_like
from tow.store import atomic_write_bytes, decode_json_bytes

FORMAT = "tow-site-transaction/v1"
DIR_NAME = ".tow-site-transaction"
_FILES = frozenset({"MANIFEST.json", "config.bin", "state.bin", "secrets.bin", "secret_undo.bin"})


def secret_undo_path() -> Path:
    return secrets_path().with_name("secrets-undo.enc")


def journal_root() -> Path:
    return secret_undo_path().parent / DIR_NAME


def journal_targets() -> dict[str, Path]:
    return {
        "config": config_path(),
        "state": state_path(),
        "secrets": secrets_path().with_name("secrets.enc"),
        "secret_undo": secret_undo_path(),
    }


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def is_link(path: Path) -> bool:
    try:
        return is_link_like(path.lstat())
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise RuntimeError("site transaction path cannot be inspected") from exc


def _own_temporary(name: str) -> bool:
    return name.endswith(".tmp") and any(name.startswith(f".{allowed}.") for allowed in _FILES)


def _validate_root(root: Path) -> None:
    if is_link(root) or not root.is_dir():
        raise RuntimeError("site transaction journal path is unsafe")
    try:
        entries = list(root.iterdir())
    except OSError as exc:
        raise RuntimeError("site transaction journal cannot be inspected") from exc
    if any(
        is_link(entry) or not entry.is_file() or (entry.name not in _FILES and not _own_temporary(entry.name))
        for entry in entries
    ):
        raise RuntimeError("site transaction journal contains an unexpected entry")


def remove_journal(root: Path) -> None:
    if is_link(root):
        raise RuntimeError("site transaction journal path is unsafe")
    if not root.exists():
        return
    _validate_root(root)
    try:
        # Removing the marker first makes interrupted cleanup recognizable: stores
        # are final, so a later lock holder only removes the remaining owned files.
        (root / "MANIFEST.json").unlink(missing_ok=True)
        for entry in root.iterdir():
            if entry.name in _FILES or _own_temporary(entry.name):
                entry.unlink(missing_ok=True)
        root.rmdir()
    except OSError as exc:
        raise RuntimeError("site transaction journal cleanup failed") from exc


def read_store(path: Path, key: str) -> bytes | None:
    if is_link(path) or (path.exists() and not path.is_file()):
        raise RuntimeError(f"store transaction target is unsafe: {key}")
    if not path.exists():
        return None
    try:
        return path.read_bytes()
    except OSError as exc:
        raise RuntimeError(f"site transaction store cannot be read: {key}") from exc


def _load_manifest(root: Path) -> dict[str, Any]:
    try:
        manifest = decode_json_bytes((root / "MANIFEST.json").read_bytes())
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise RuntimeError("site transaction journal is unreadable") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise RuntimeError("site transaction journal format is invalid")
    status = manifest.get("status")
    if status not in ("prepared", "committed") or not isinstance(manifest.get("targets"), list):
        raise RuntimeError("site transaction journal state is invalid")
    return manifest


def _validated_entries(manifest: dict[str, Any], targets: dict[str, Path]) -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    for entry in manifest["targets"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("key"), str):
            # Recovery failures use RuntimeError so the web boundary returns 503.
            raise RuntimeError("site transaction journal targets are invalid")  # noqa: TRY004
        key = entry["key"]
        if key not in targets or key in entries:
            raise RuntimeError("site transaction journal targets are invalid")
        entries[key] = entry
    if set(entries) != set(targets):
        raise RuntimeError("site transaction journal targets are invalid")
    for key, target in targets.items():
        entry = entries[key]
        if entry.get("target") != str(target.resolve()):
            raise RuntimeError("site transaction journal target mismatch")
        exists = entry.get("exists")
        backup_name = entry.get("backup")
        if not isinstance(exists, bool):
            # RuntimeError like every other journal check: the middleware turns it into a 503.
            raise RuntimeError("site transaction journal exists flag is invalid")  # noqa: TRY004
        if exists:
            if backup_name != f"{key}.bin":
                raise RuntimeError("site transaction journal backup name is invalid")
            checksum = entry.get("sha256")
            if (
                not isinstance(checksum, str)
                or len(checksum) != 64
                or any(c not in "0123456789abcdef" for c in checksum)
            ):
                raise RuntimeError("site transaction journal backup checksum is invalid")
        elif backup_name is not None or entry.get("sha256") is not None:
            raise RuntimeError("site transaction journal has unexpected backup")
    return entries


def _restore_plan(root: Path, targets: dict[str, Path], entries: dict[str, dict[str, Any]]) -> dict[str, bytes | None]:
    plan: dict[str, bytes | None] = {}
    for key, target in targets.items():
        read_store(target, key)
        entry = entries[key]
        if entry["exists"]:
            content = read_store(root / f"{key}.bin", key)
            if content is None:
                raise RuntimeError("site transaction journal backup is missing")
            if entry["sha256"] != digest(content):
                raise RuntimeError("site transaction journal backup checksum mismatch")
            plan[key] = content
        else:
            plan[key] = None
    return plan


def recover_unlocked(root: Path, targets: dict[str, Path]) -> bool:
    """Roll an unfinished transaction back (the caller holds the data lock); True if it did."""
    if is_link(root):
        raise RuntimeError("site transaction journal path is unsafe")
    if not root.exists():
        return False
    _validate_root(root)
    if not (root / "MANIFEST.json").exists():
        # Preparation had not published its marker, or final cleanup was interrupted.
        remove_journal(root)
        return False
    manifest = _load_manifest(root)
    entries = _validated_entries(manifest, targets)
    if manifest["status"] == "committed":
        remove_journal(root)
        return False
    plan = _restore_plan(root, targets, entries)
    try:
        for key, target in targets.items():
            content = plan[key]
            if content is not None:
                atomic_write_bytes(target, content)
                if read_store(target, key) != content:
                    raise RuntimeError(f"site transaction recovery read-back failed: {key}")
            else:
                with contextlib.suppress(FileNotFoundError):
                    target.unlink()
                if target.exists():
                    raise RuntimeError(f"site transaction recovery could not remove: {key}")
    except OSError as exc:
        raise RuntimeError("site transaction recovery write failed") from exc
    remove_journal(root)
    return True


def recover_site_journal() -> None:
    """The recovery hook: runs whenever a process takes the data lock (outermost level).

    A transaction in progress is never in the way: the web writes it while it holds the
    data lock, so no other thread or process can take the lock (and run this) meanwhile.
    """
    recover_unlocked(journal_root(), journal_targets())
