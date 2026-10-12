"""The zip inside the envelope: its members, their manifest and checksums, and reading a bundle
file back into checked parts."""

from __future__ import annotations

import io
import json
import lzma
import zipfile
import zlib
from pathlib import Path, PurePosixPath
from typing import Any

from tow import __version__
from tow.bundle import io as bundle_io
from tow.bundle.crypto import decrypt_outer
from tow.bundle.errors import ExportImportError
from tow.bundle.io import FORMAT, read_limited, sha256
from tow.bundle.schema import (
    parse_mapping,
    utf8_config,
    validate_config_schema,
    validate_history_schema,
    validate_secrets_schema,
    validate_state_schema,
    validate_tree,
)
from tow.bundle.secret_scan import events_are_redacted, refuse_plaintext_secrets

REQUIRED_MEMBERS = ("config.yaml", "state.json", "download_history.json", "secrets.json")
OPTIONAL_MEMBERS = ("secrets_undo.json", "events.json")
ALLOWED_MEMBERS = {"manifest.json", *REQUIRED_MEMBERS, *OPTIONAL_MEMBERS}


def zip_payload(members: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(members):
            archive.writestr(name, members[name])
    return output.getvalue()


def _validate_member_name(name: str) -> None:
    if not isinstance(name, str) or not name or "\\" in name:
        raise ExportImportError("bundle member must be a strict relative path")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or name not in ALLOWED_MEMBERS:
        raise ExportImportError(f"unsupported or unsafe bundle member: {name}")


# What zipfile raises for a damaged archive besides BadZipFile: a negative seek (ValueError), an
# unknown version or method (NotImplementedError), a broken deflate or LZMA stream, a cut one.
_ARCHIVE_ERRORS = (
    OSError,
    RuntimeError,
    ValueError,
    EOFError,
    zipfile.BadZipFile,
    zlib.error,
    lzma.LZMAError,
)


def unzip_payload(payload: bytes) -> dict[str, bytes]:
    # Read through io: one limit for this and read_limited, wherever it is lowered.
    if len(payload) > bundle_io.MAX_BUNDLE_BYTES:
        raise ExportImportError("decrypted export bundle is too large", reason="too_large")
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload), "r")
        infos = archive.infolist()
    except _ARCHIVE_ERRORS as exc:
        raise ExportImportError("invalid export bundle archive") from exc
    names = [info.filename for info in infos]
    if len(names) != len(set(names)):
        raise ExportImportError("duplicate bundle member")
    members: dict[str, bytes] = {}
    total = 0
    try:
        for info in infos:
            _validate_member_name(info.filename)
            if info.is_dir() or (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise ExportImportError("bundle directories and links are not allowed")
            total += info.file_size
            if total > bundle_io.MAX_BUNDLE_BYTES:
                raise ExportImportError("bundle contents are too large", reason="too_large")
            # At most the declared size is unpacked (more data fails the CRC): the total bounds memory.
            with archive.open(info) as handle:
                members[info.filename] = handle.read(info.file_size or 1)
    except ExportImportError:
        raise
    except _ARCHIVE_ERRORS as exc:
        raise ExportImportError("cannot read export bundle archive") from exc
    finally:
        archive.close()
    return members


def _validated_payload(payload: bytes) -> dict[str, Any]:
    members = unzip_payload(payload)
    if "manifest.json" not in members:
        raise ExportImportError("bundle manifest is missing")
    try:
        manifest = json.loads(members["manifest.json"].decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ExportImportError("invalid bundle manifest") from exc
    validate_tree(manifest, label="manifest.json")
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT or manifest.get("schema_version") != 1:
        raise ExportImportError("unsupported bundle manifest")
    source_version = manifest.get("source_version")
    if not isinstance(source_version, str) or not source_version:
        raise ExportImportError("bundle source version is missing")
    if source_version.split(".", 1)[0] != __version__.split(".", 1)[0]:
        raise ExportImportError("bundle source version is incompatible")
    listed = manifest.get("members")
    checksums = manifest.get("sha256")
    if not isinstance(listed, list) or not isinstance(checksums, dict):
        raise ExportImportError("malformed bundle manifest")
    expected = {str(name) for name in listed}
    actual = set(members) - {"manifest.json"}
    if expected != actual or len(listed) != len(expected):
        raise ExportImportError("bundle manifest member list mismatch")
    if not set(REQUIRED_MEMBERS).issubset(actual):
        raise ExportImportError("bundle required member is missing")
    if set(checksums) != actual:
        raise ExportImportError("bundle checksum list mismatch")
    for name in actual:
        _validate_member_name(name)
        if checksums.get(name) != sha256(members[name]):
            raise ExportImportError(f"bundle checksum mismatch: {name}")

    # Authenticate the original member above, then prepare readable destination
    # bytes without altering the source archive or its checksum manifest.
    config_bytes = utf8_config(members["config.yaml"])
    config_data = parse_mapping(config_bytes, label="config.yaml")
    state_data = parse_mapping(members["state.json"], label="state.json")
    history_data = parse_mapping(members["download_history.json"], label="download_history.json")
    secrets_data = parse_mapping(members["secrets.json"], label="secrets.json")
    validate_config_schema(config_data)
    validate_state_schema(state_data)
    validate_history_schema(history_data)
    validate_secrets_schema(secrets_data, label="secrets.json")
    refuse_plaintext_secrets(config_data, state_data, history_data)
    if "secrets" in (state_data.get("undo") or {}):
        raise ExportImportError("plaintext secret undo is not allowed in bundle state")

    undo_data = None
    undo = state_data.get("undo")
    if isinstance(undo, dict) and undo.get("kind") == "settings":
        if undo.get("secrets_undo_ref") != "settings-v1" or "secrets_undo.json" not in actual:
            raise ExportImportError("settings undo reference has no encrypted snapshot")
        undo_data = parse_mapping(members["secrets_undo.json"], label="secrets_undo.json")
        if not isinstance(undo_data.get("secrets"), dict):
            raise ExportImportError("malformed encrypted settings undo payload")
        validate_secrets_schema(undo_data["secrets"], label="secrets_undo.json.secrets")
    elif "secrets_undo.json" in actual:
        raise ExportImportError("orphan encrypted settings undo member")

    events = None
    if "events.json" in actual:
        events = parse_mapping(members["events.json"], label="events.json")
        validate_tree(events, label="events.json")
        if not isinstance(events.get("events"), list) or not events_are_redacted(events):
            raise ExportImportError("events member is not redacted")
    return {
        "manifest": manifest,
        "members": members,
        "config_bytes": config_bytes,
        "config": config_data,
        "state": state_data,
        "history": history_data,
        "secrets": secrets_data,
        "undo": undo_data,
        "events": events,
    }


def verify_bundle(path: Path, passphrase: str) -> None:
    """Decrypt and validate an archive without recovering or changing any live store."""
    read_bundle(path, passphrase)


def read_bundle(path: Path, passphrase: str) -> dict[str, Any]:
    try:
        raw = read_limited(Path(path), label="export bundle")
        outer = json.loads(raw.decode("utf-8"))
        validate_tree(outer, label="export bundle envelope")
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ExportImportError("invalid export bundle envelope", reason="not_tow") from exc
    except ExportImportError as exc:
        raise ExportImportError(str(exc), reason="too_large" if exc.reason == "too_large" else "not_tow") from exc
    return _validated_payload(decrypt_outer(outer, passphrase))
