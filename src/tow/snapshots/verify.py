"""Reading a night copy back: every hash, then the contents of its stores."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import Any

from tow.i18n import t
from tow.log import owner_language
from tow.snapshots.errors import SnapshotError
from tow.snapshots.location import snapshot_path
from tow.snapshots.manifest import FORMAT, member_size, read_manifest, signature_matches, signing_key
from tow.snapshots.members import (
    NOT_RESTORED,
    VALIDATED_MEMBERS,
    member_target,
    ordinary_directory,
    ordinary_file,
    parse_copy_config,
)
from tow.store import (
    SecretStoreError,
    StateVersionError,
    decrypt_secret_undo_bytes,
    decrypt_secrets_bytes,
    persistence_lock,
    validate_download_history_bytes,
    validate_state_bytes,
)
from tow.yaml_guard import MAX_INPUT_BYTES, YamlLimitError

READ_CHUNK_BYTES = 1024 * 1024


def verified_member(source: Path, name: str, meta: Any, *, collect: bool) -> bytes | None:
    """Hash one regular file in bounded blocks, optionally retaining exactly those bytes."""
    size = member_size(name, meta)
    lang = owner_language()
    if name == "config.yaml" and size > MAX_INPUT_BYTES:
        raise SnapshotError(str(YamlLimitError("yaml_limits.size")))
    digest = hashlib.sha256()
    content = bytearray() if collect else None
    seen = 0
    try:
        ordinary_directory(source.parent)
        ordinary_file(source)
        with source.open("rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size != size:
                raise ValueError("member is not a regular file of the expected size")
            while block := handle.read(min(READ_CHUNK_BYTES, size - seen + 1)):
                seen += len(block)
                if seen > size:
                    raise ValueError("member grew while reading")
                digest.update(block)
                if content is not None:
                    content.extend(block)
    except OSError as exc:
        raise SnapshotError(t("backup.snapshot.file_missing", lang, name=name)) from exc
    except (ValueError, OverflowError) as exc:
        raise SnapshotError(t("backup.snapshot.file_damaged", lang, name=name)) from exc
    if seen != size or digest.hexdigest() != meta.get("sha256"):
        raise SnapshotError(t("backup.snapshot.file_damaged", lang, name=name))
    return bytes(content) if content is not None else None


def read_verified(
    path: Path, *, retain: bool = True, require_signed: bool = False, payloads: bool = True
) -> tuple[dict[str, Any], dict[str, bytes], bool]:
    """(manifest, contents by member name, signed): all hashes pass before payload parsing.

    The bytes returned are the ones whose hash was checked, so a restore writes exactly
    what was verified (no second read the copies folder could change in between).
    Verification and preview hash everything, then re-read/hash only semantic stores
    one at a time: no parser sees changed bytes or an initially damaged copy. Restore
    reads each file once and retains its checked bytes, never re-reading for a write.
    A signed MANIFEST must carry this install's signature; an unsigned one (made before
    1.17) is still checked file by file, and the caller decides what it may be used for.
    """
    path = Path(path)
    lang = owner_language()
    manifest = read_manifest(path)
    signed = manifest["format"] == FORMAT
    if signed and not signature_matches(manifest, signing_key()):
        raise SnapshotError(t("backup.snapshot.bad_signature", lang))
    if require_signed and not signed:
        raise SnapshotError(t("backup.snapshot.unsigned", lang))
    contents: dict[str, bytes] = {}
    for name, meta in manifest["files"].items():
        member_target(name)  # a strict name, and a target that stays where it belongs
        content = verified_member(path / name, name, meta, collect=retain and name != NOT_RESTORED)
        if content is not None:
            contents[name] = content
        del content
    if retain:
        if "config.yaml" in contents:
            parse_copy_config(contents["config.yaml"])
        _check_payloads(contents)
    elif payloads:
        for name in VALIDATED_MEMBERS:
            if name not in manifest["files"]:
                continue
            content = verified_member(path / name, name, manifest["files"][name], collect=True)
            assert content is not None
            check_member(name, content)
            del content  # the next store must not overlap a previous verification-only buffer
    return manifest, contents, signed


def verify_snapshot(path: Path, *, payloads: bool = True) -> dict[str, Any]:
    """The MANIFEST when hashes and store contents pass (``signed`` permits restoration).
    ``payloads=False``: hashes only, for a copy whose stores were checked while it was made."""
    manifest, _contents, signed = read_verified(path, retain=False, payloads=payloads)
    return {**manifest, "signed": signed}


def check_snapshot(name: str) -> dict[str, Any]:
    with persistence_lock():
        return verify_snapshot(snapshot_path(name))


def check_member(name: str, content: bytes) -> None:
    if name == "config.yaml":
        parse_copy_config(content)
    else:
        _check_payloads({name: content})


def _check_payloads(contents: dict[str, bytes]) -> None:
    """Checksums alone do not make a usable copy; validate the exact bytes before any write or prune."""
    for name, validate in (
        ("state.json", validate_state_bytes),
        ("download_history.json", validate_download_history_bytes),
    ):
        if name in contents:
            try:
                validate(contents[name])
            except (UnicodeError, ValueError, RecursionError, StateVersionError) as exc:
                raise SnapshotError(t("backup.snapshot.file_unusable", owner_language(), name=name)) from exc
    for name, decrypt in (
        ("secrets.enc", decrypt_secrets_bytes),
        ("secrets-undo.enc", decrypt_secret_undo_bytes),
    ):
        if name in contents:
            try:
                decrypt(contents[name])
            except SecretStoreError as exc:
                raise SnapshotError(t("backup.snapshot.secrets_unusable", owner_language(), name=name)) from exc
