"""The MANIFEST of a night copy: its format, its byte limits, its signature (HMAC-SHA256 with a
key derived from the master key) and the check that a signed copy is this install's.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
from pathlib import Path
from typing import Any

from tow.i18n import t
from tow.log import owner_language
from tow.snapshots.errors import SnapshotError
from tow.snapshots.members import ordinary_directory, ordinary_file
from tow.store import SecretStoreError, derive_local_secret

FORMAT = "tow-snapshot-v2"  # a signed MANIFEST
UNSIGNED_FORMAT = "tow-snapshot-v1"  # copies made before 1.17: checked, never restored automatically
_SIGNATURE_PURPOSE = "night-copies"
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_MEMBER_SIZE = 2**63 - 1
# A journal carries names/checksums, not payloads. Allow ample encoding overhead
# above the entire supported manifest; these limits never cap state or history.
MAX_RESTORE_JOURNAL_BYTES = 4 * MAX_MANIFEST_BYTES


def signing_key() -> bytes:
    try:
        return derive_local_secret(_SIGNATURE_PURPOSE).encode("ascii")
    except SecretStoreError as exc:
        raise SnapshotError(t("backup.snapshot.no_key", owner_language())) from exc


def signature_of(manifest: dict[str, Any], key: bytes) -> str:
    body = {name: value for name, value in manifest.items() if name != "signature"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return hmac.new(key, canonical, hashlib.sha256).hexdigest()


def signature_matches(manifest: dict[str, Any], key: bytes) -> bool:
    signature = manifest.get("signature")
    return (
        isinstance(signature, str)
        and signature.isascii()
        and hmac.compare_digest(signature, signature_of(manifest, key))
    )


def read_manifest(path: Path) -> dict[str, Any]:
    lang = owner_language()
    try:
        ordinary_directory(path)
        ordinary_file(path / "MANIFEST.json")
        with (path / "MANIFEST.json").open("rb") as handle:
            content = handle.read(MAX_MANIFEST_BYTES + 1)
        if len(content) > MAX_MANIFEST_BYTES:
            raise SnapshotError(t("backup.snapshot.manifest_too_large", lang))
        manifest = json.loads(content.decode("utf-8"))
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise SnapshotError(t("backup.snapshot.manifest_unreadable", lang)) from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("format") not in (FORMAT, UNSIGNED_FORMAT)
        or not isinstance(manifest.get("files"), dict)
    ):
        raise SnapshotError(t("backup.snapshot.not_tow", lang))
    return manifest


def member_size(name: str, meta: Any) -> int:
    """A file size, not a coerced float/string or an unrepresentable filesystem integer."""
    size = meta.get("size") if isinstance(meta, dict) else None
    if type(size) is not int or not 0 <= size <= MAX_MEMBER_SIZE:
        raise SnapshotError(t("backup.snapshot.file_damaged", owner_language(), name=name))
    return size


def manifest_bytes(manifest: dict[str, Any]) -> bytes:
    with io.BytesIO() as output:
        for chunk in json.JSONEncoder(indent=2).iterencode(manifest):
            content = chunk.encode("utf-8")
            if output.tell() + len(content) > MAX_MANIFEST_BYTES:
                raise SnapshotError(t("backup.snapshot.manifest_too_large", owner_language()))
            output.write(content)
        return output.getvalue()


def owned_manifest(path: Path, key: bytes) -> dict[str, Any] | None:
    """Only a signed copy proves that TOW owns this folder and may prune it."""
    try:
        manifest = read_manifest(path)
    except SnapshotError:
        return None
    return manifest if manifest["format"] == FORMAT and signature_matches(manifest, key) else None
