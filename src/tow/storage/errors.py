"""The errors more than one storage module raises (``tow.store`` re-exports them)."""

from __future__ import annotations


class SecretStoreError(RuntimeError):
    """Raised when TOW cannot safely read or write encrypted secrets."""


class StoreCorruptionError(RuntimeError):
    """Raised when a persisted JSON store is corrupt and was quarantined."""
