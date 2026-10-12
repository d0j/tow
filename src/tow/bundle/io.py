"""The bytes of a bundle: its format name and size limit, JSON as TOW writes it, checksums,
bounded reads and atomic writes."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from tow.bundle.errors import ExportImportError
from tow.store import atomic_write_bytes

FORMAT = "tow-export-v1"

# The bound of a bundle file, and of everything unpacked from it together: what a restore point
# of the live data may hold. There is no smaller limit per member (the history is the big one).
MAX_BUNDLE_BYTES = 64 * 1024 * 1024


def json_bytes(data: Any, *, readable: bool = False) -> bytes:
    """Compact JSON, as the live store writes the download history: written indented and
    sorted, a history of some 20,000 file records outgrew what a bundle could hold, and every
    restore point (the web update's too) failed. ``readable``: indented, as the store writes
    state.json and as the small records a person may open are kept."""
    try:
        if readable:
            text = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)
        else:
            text = json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        return (text + "\n").encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ExportImportError("portable TOW data is not JSON serializable") from exc


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_limited(path: Path, *, label: str) -> bytes:
    try:
        size = path.stat().st_size
        if size > MAX_BUNDLE_BYTES:
            raise ExportImportError(f"{label} is too large", reason="too_large")
        with path.open("rb") as handle:
            content = handle.read(MAX_BUNDLE_BYTES + 1)
        if len(content) > MAX_BUNDLE_BYTES:
            raise ExportImportError(f"{label} is too large", reason="too_large")
        return content
    except ExportImportError:
        raise
    except OSError as exc:
        raise ExportImportError(f"cannot read {label}") from exc


def atomic_write(path: Path, content: bytes) -> None:
    try:
        atomic_write_bytes(path, content)
    except OSError as exc:
        raise ExportImportError(f"cannot write imported {path.name}") from exc
