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
letting anyone work on stores that may not belong together. The folder checks and the restore
itself are ``tow.journal``'s; the manifest (``MANIFEST.json``) is this module's.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tow.journal import Copy, Journal, is_digest
from tow.paths import config_path, secrets_path, state_path

FORMAT = "tow-site-transaction/v1"
DIR_NAME = ".tow-site-transaction"
MARKER = "MANIFEST.json"
_FILES = frozenset({MARKER, "config.bin", "state.bin", "secrets.bin", "secret_undo.bin"})


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


def journal(root: Path) -> Journal:
    return Journal(root, MARKER, _FILES, RuntimeError, label="site transaction", store="site transaction store")


def remove_journal(root: Path) -> None:
    site = journal(root)
    if not site.exists():
        return
    try:
        site.remove()
    except OSError as exc:
        raise RuntimeError("site transaction journal cleanup failed") from exc


def _copies(manifest: Any, targets: dict[str, Path]) -> list[Copy] | None:
    """The stores to put back (None: the transaction committed); refuses a malformed manifest."""
    # Recovery failures use RuntimeError, never TypeError: the web boundary turns it into a 503.
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise RuntimeError("site transaction journal format is invalid")
    status = manifest.get("status")
    if status not in ("prepared", "committed") or not isinstance(manifest.get("targets"), list):
        raise RuntimeError("site transaction journal state is invalid")
    entries: dict[str, dict[str, Any]] = {}
    for entry in manifest["targets"]:
        key = entry.get("key") if isinstance(entry, dict) else None
        if not isinstance(key, str) or key not in targets or key in entries:
            raise RuntimeError("site transaction journal targets are invalid")
        entries[key] = entry
    if set(entries) != set(targets):
        raise RuntimeError("site transaction journal targets are invalid")
    copies = []
    for key, target in targets.items():
        entry = entries[key]
        if entry.get("target") != str(target.resolve()):
            raise RuntimeError("site transaction journal target mismatch")
        exists = entry.get("exists")
        if not isinstance(exists, bool):
            raise RuntimeError("site transaction journal exists flag is invalid")  # noqa: TRY004
        if exists and (entry.get("backup") != f"{key}.bin" or not is_digest(entry.get("sha256"))):
            raise RuntimeError("site transaction journal backup is invalid")
        if not exists and (entry.get("backup") is not None or entry.get("sha256") is not None):
            raise RuntimeError("site transaction journal has unexpected backup")
        copies.append(Copy(key, target, entry["backup"], entry["sha256"]))
    return copies if status == "prepared" else None


def recover_unlocked(root: Path, targets: dict[str, Path]) -> None:
    """Roll an unfinished transaction back (the caller holds the data lock)."""
    try:
        journal(root).recover(lambda manifest: _copies(manifest, targets))
    except OSError as exc:  # a store or copy that cannot be removed: still a RuntimeError (503)
        raise RuntimeError("site transaction journal cleanup failed") from exc


def recover_site_journal() -> None:
    """The recovery hook: runs whenever a process takes the data lock (outermost level).

    A transaction in progress is never in the way: the web writes it while it holds the
    data lock, so no other thread or process can take the lock (and run this) meanwhile.
    """
    recover_unlocked(journal_root(), journal_targets())
