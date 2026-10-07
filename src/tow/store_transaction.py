"""One journaled write of several TOW stores: config.yaml, state.json, the secrets and the
secret undo snapshot (secrets-undo.enc).

Either every store ends with its new content, or every store is back to what it was. A copy of
all four is taken first (the journal, ``tow.site_journal``); a failure inside the transaction
restores that copy at once, and a crash (the process gone mid-way) is restored by whichever TOW
process next takes the data lock: the journal's recovery hook runs before any writer. Site edits,
settings saves and every undo write through here, so no crash can leave, say, new secrets next to
the old config or an undo record pointing at secrets that are gone.

    with transaction() as txn:
        txn.save_secrets(secrets)
        txn.save_config(cfg)
        txn.save_state(state)

The writes go through ``tow.store`` and ``tow.config`` (looked up when called, so a test can make
one of them fail); ``_after_write`` is the test seam that sees each store written.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

from tow import config as config_store
from tow import site_journal, store
from tow.journal import digest


class TransactionError(RuntimeError):
    """The transaction failed; every store is back to what it was."""


class RollbackError(TransactionError):
    """The transaction failed and restoring the stores failed too: they may not belong together.

    The journal stays, so the next process that takes the data lock tries the restore again.
    """


# Test seam: called with the store's name ("config", "state", "secrets", "secret_undo") after each
# write. A test raising a BaseException here is a crash: nothing is restored in this process.
_after_write: Callable[[str], None] | None = None

_LOCK = threading.RLock()


class StoreTransaction:
    """The writes of one transaction; only ``transaction()`` makes one."""

    def __init__(self) -> None:
        self.written: list[str] = []

    def _wrote(self, name: str) -> None:
        self.written.append(name)
        if _after_write is not None:
            _after_write(name)

    def save_config(self, data: dict[str, Any]) -> None:
        config_store.save_config(data)
        self._wrote("config")

    def set_interval_sec(self, seconds: int) -> None:
        """In place: the owner's comments in config.yaml stay."""
        config_store.set_interval_sec(seconds)
        self._wrote("config")

    def set_flash_ttl_sec(self, seconds: int) -> None:
        config_store.set_flash_ttl_sec(seconds)
        self._wrote("config")

    def save_state(self, data: dict[str, Any]) -> None:
        store.save_state(data)
        self._wrote("state")

    def save_secrets(self, data: dict[str, Any]) -> None:
        store.save_secrets(data)
        self._wrote("secrets")

    def save_secret_undo(self, snapshot: dict[str, Any]) -> str:
        """The encrypted snapshot an undo restores secrets from; returns its reference."""
        reference = store.save_secret_undo(snapshot)
        self._wrote("secret_undo")
        return reference

    def delete_secret_undo(self, reference: str) -> None:
        """The snapshot is gone with the rest of the transaction, or comes back with it."""
        store.delete_secret_undo(reference)
        self._wrote("secret_undo")


def journal_root() -> Path:
    return site_journal.journal_root()


def journal_targets() -> dict[str, Path]:
    return site_journal.journal_targets()


def begin_unlocked() -> Path:
    """Copy every store into a new journal (the caller holds the data lock)."""
    root = journal_root()
    targets = journal_targets()
    site_journal.recover_unlocked(root, targets)  # an older, unfinished one first
    if root.exists():
        raise RuntimeError("unfinished store transaction could not be cleared")
    root.mkdir(parents=True)
    site = site_journal.journal(root)
    entries = []
    try:
        for key, target in targets.items():
            content = site.read(target)
            backup_name = f"{key}.bin" if content is not None else None
            if content is not None:
                store.atomic_write_bytes(root / f"{key}.bin", content)
                if site.read(root / f"{key}.bin") != content:
                    raise RuntimeError(f"store transaction backup read-back failed: {key}")
            entries.append(
                {
                    "key": key,
                    "target": str(target.resolve()),
                    "backup": backup_name,
                    "exists": content is not None,
                    "sha256": digest(content) if content is not None else None,
                }
            )
        store.atomic_write_text(
            root / site_journal.MARKER,
            json.dumps({"format": site_journal.FORMAT, "status": "prepared", "targets": entries}, indent=2),
        )
    except Exception:
        site_journal.remove_journal(root)
        raise
    return root


def begin() -> Path:
    """A journal taken under the data lock: the recovery hook of another process (or thread)
    must never see one that is still being written."""
    with store.persistence_lock(), _LOCK:
        return begin_unlocked()


def _mark_committed(root: Path) -> None:
    manifest_path = root / site_journal.MARKER
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["status"] = "committed"
    store.atomic_write_text(manifest_path, json.dumps(manifest, indent=2))


def recover() -> None:
    """Restore the stores of an unfinished transaction.

    A cheap unlocked look first: the web runs this before every request and must not wait for a
    scheduled check. A real restore holds the data lock, so it never interleaves with a writer.
    """
    root = journal_root()
    if not root.exists() and not site_journal.journal(root).is_link(root):
        return
    with store.persistence_lock(), _LOCK:
        site_journal.recover_unlocked(journal_root(), journal_targets())


@contextmanager
def transaction() -> Iterator[StoreTransaction]:
    """Write several stores as one: all of the new contents or none of them.

    Raises ``TransactionError`` (the stores are as before) or ``RollbackError`` (the restore
    failed too). A BaseException that is not an Exception (the process being stopped) leaves the
    journal for the recovery hook, exactly as a crash would.
    """
    with store.persistence_lock(), _LOCK:
        root = begin_unlocked()
        try:
            yield StoreTransaction()
            _mark_committed(root)
        except Exception as exc:
            try:
                site_journal.recover_unlocked(root, journal_targets())
            except Exception as rollback_exc:  # its __context__ is the failure being rolled back
                raise RollbackError("store transaction failed and the stores were not restored") from rollback_exc
            raise TransactionError("store transaction failed; every store restored") from exc
        # Committed: a journal left behind only says so, and the next lock taker clears it.
        with suppress(OSError, RuntimeError):
            site_journal.remove_journal(root)


def commit(
    *,
    config: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
    secrets: dict[str, Any] | None = None,
) -> None:
    """Write the given stores as one transaction (``None``: that store stays as it is)."""
    with transaction() as txn:
        if secrets is not None:
            txn.save_secrets(secrets)
        if config is not None:
            txn.save_config(config)
        if state is not None:
            txn.save_state(state)
