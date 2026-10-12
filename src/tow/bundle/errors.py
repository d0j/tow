"""The error of the portable TOW file: what failed for the log, and what the owner is told."""

from __future__ import annotations

from tow.errors import Msg, TowError


class ExportImportError(RuntimeError):
    """Raised when a portable TOW export/import cannot be handled safely.

    ``reason`` is what the owner is told about a file that does not pass: ``not_tow`` (not a TOW
    backup at all), ``other_key`` (cannot be decrypted: another master key, or damaged),
    ``too_large``, or ``damaged`` (anything else); the message stays technical, for the log.
    ``owner_text`` is what the owner is told instead, when the refusal says more than the reason
    (a file that exists, a --path-map typed wrong, data put back after a failed import)."""

    def __init__(self, message: str = "", *, reason: str = "damaged", owner_text: Msg | TowError | None = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.owner_text = owner_text
