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
import json
import shutil
from pathlib import Path

from tow.paths import config_path, secrets_path, state_path
from tow.store import atomic_write_bytes

FORMAT = "tow-site-transaction/v1"
DIR_NAME = ".tow-site-transaction"


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


def remove_journal(root: Path) -> None:
    if not root.exists():
        return
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError("site transaction journal path is unsafe")
    shutil.rmtree(root)


def recover_unlocked(root: Path, targets: dict[str, Path]) -> bool:
    """Roll an unfinished transaction back (the caller holds the data lock); True if it did."""
    if not root.exists():
        return False
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError("site transaction journal path is unsafe")
    try:
        manifest = json.loads((root / "MANIFEST.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("site transaction journal is unreadable") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise RuntimeError("site transaction journal format is invalid")
    status = manifest.get("status")
    if status == "committed":
        remove_journal(root)
        return False
    if status != "prepared" or not isinstance(manifest.get("targets"), list):
        raise RuntimeError("site transaction journal state is invalid")
    entries = {entry.get("key"): entry for entry in manifest["targets"] if isinstance(entry, dict)}
    if set(entries) != set(targets) or len(entries) != len(manifest["targets"]):
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
            backup = root / backup_name
            if backup.is_symlink() or not backup.is_file():
                raise RuntimeError("site transaction journal backup is missing")
            if entry.get("sha256") != digest(backup.read_bytes()):
                raise RuntimeError("site transaction journal backup checksum mismatch")
        elif backup_name is not None or entry.get("sha256") is not None:
            raise RuntimeError("site transaction journal has unexpected backup")
    for key, target in targets.items():
        entry = entries[key]
        if entry["exists"]:
            content = (root / entry["backup"]).read_bytes()
            atomic_write_bytes(target, content)
            if target.read_bytes() != content:
                raise RuntimeError(f"site transaction recovery read-back failed: {key}")
        else:
            with contextlib.suppress(FileNotFoundError):
                target.unlink()
            if target.exists():
                raise RuntimeError(f"site transaction recovery could not remove: {key}")
    remove_journal(root)
    return True


def recover_site_journal() -> None:
    """The recovery hook: runs whenever a process takes the data lock (outermost level).

    A transaction in progress is never in the way: the web writes it while it holds the
    data lock, so no other thread or process can take the lock (and run this) meanwhile.
    """
    root = journal_root()
    if root.exists():
        recover_unlocked(root, journal_targets())
