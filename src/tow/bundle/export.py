"""Export: the live stores, read under the data lock, sealed into a bundle and read back."""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

from tow import __version__
from tow.bundle.archive import read_bundle, zip_payload
from tow.bundle.checkpoint import checkpoint_file
from tow.bundle.crypto import seal_outer
from tow.bundle.errors import ExportImportError
from tow.bundle.io import FORMAT, atomic_write, json_bytes, read_limited, sha256
from tow.bundle.schema import parse_mapping, utf8_config
from tow.bundle.secret_scan import refuse_plaintext_secrets
from tow.errors import Msg
from tow.log import export_event_projection, read_events
from tow.paths import config_path, download_history_path
from tow.store import (
    STATE_SCHEMA_VERSION,
    SecretStoreError,
    StoreCorruptionError,
    atomic_create_bytes,
    decode_json_bytes,
    load_json,
    load_secret_undo,
    load_secrets,
    load_state,
    persistence_lock,
    restrict_file,
)


def _build_export_members(*, include_log: bool) -> dict[str, bytes]:
    config_file = config_path()
    config_bytes = utf8_config(read_limited(config_file, label="config.yaml"))
    config_data = parse_mapping(config_bytes, label="config.yaml")
    # load_state intentionally hides the format header from callers. An archive must
    # restore it, or an old reader could accept newer fields after importing it.
    state_data = {"schema_version": STATE_SCHEMA_VERSION, **load_state()}
    history_data = load_json_for_export(download_history_path(), {"schema_version": 1, "topics": {}})
    secrets_data = load_secrets()
    refuse_plaintext_secrets(config_data, state_data, history_data)
    state_bytes = json_bytes(state_data)
    history_bytes = json_bytes(history_data)
    members = {
        "config.yaml": config_bytes,
        "state.json": state_bytes,
        "download_history.json": history_bytes,
        "secrets.json": json_bytes(secrets_data),
    }
    undo = state_data.get("undo")
    if isinstance(undo, dict) and undo.get("kind") == "settings":
        if undo.get("secrets_undo_ref") != "settings-v1":
            raise ExportImportError("settings undo reference is invalid")
        try:
            undo_secrets = load_secret_undo("settings-v1")
        except SecretStoreError as exc:
            raise ExportImportError("cannot read encrypted settings undo snapshot") from exc
        members["secrets_undo.json"] = json_bytes({"secrets": undo_secrets})
    if include_log:
        members["events.json"] = json_bytes(
            {"events": [export_event_projection(event) for event in read_events(limit=200)]}
        )
    manifest = {
        "format": FORMAT,
        "schema_version": 1,
        "source_version": __version__,
        "members": sorted(members),
        "sha256": {name: sha256(data) for name, data in sorted(members.items())},
        "excluded": [
            "media_bytes",
            "torrent_files",
            "torrent_client_database",
            "os_keyrings",
            "scheduler_registrations",
            "caches",
            "source_master_key",
        ],
    }
    return {"manifest.json": json_bytes(manifest), **members}


def load_json_for_export(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        if not checkpoint_file(path, missing=True):
            # A quarantined history is not an empty history. Do not make an
            # apparently healthy backup that silently omits the original data.
            data = load_json(path, default, quarantine=False)
        else:
            raw = read_limited(path, label=path.name)
            data = decode_json_bytes(raw)
    except (UnicodeError, ValueError, RecursionError, StoreCorruptionError) as exc:
        raise ExportImportError(f"invalid {path.name}") from exc
    if not isinstance(data, dict):
        raise ExportImportError(f"{path.name} must be an object")
    return data


def export_bundle(
    output: Path, passphrase: str, *, include_log: bool = False, overwrite: bool = False
) -> dict[str, Any]:
    try:
        with persistence_lock():
            return _export_bundle(output, passphrase, include_log=include_log, overwrite=overwrite)
    except ExportImportError:
        raise
    except Exception as exc:
        raise ExportImportError("cannot create export bundle safely") from exc


def _export_bundle(
    output: Path, passphrase: str, *, include_log: bool = False, overwrite: bool = False
) -> dict[str, Any]:
    output = Path(output)
    if output.exists() and not overwrite:
        raise ExportImportError(
            "export output already exists; choose another path or use --force",
            owner_text=Msg("cli.bundle.output_exists", path=str(output)),
        )
    previous = output.read_bytes() if overwrite and output.is_file() else None
    members = _build_export_members(include_log=include_log)
    payload = zip_payload(members)
    outer = seal_outer(payload, passphrase)
    content = json_bytes(outer)
    if overwrite:
        atomic_write(output, content)
    else:
        atomic_create_bytes(output, content)
    restrict_file(output)
    try:
        verified = read_bundle(output, passphrase)
        if set(verified["members"]) != set(members):
            raise ExportImportError("export bundle read-back member mismatch")
    except ExportImportError:
        if previous is None:
            with contextlib.suppress(OSError):
                if not output.is_symlink() and output.read_bytes() == content:
                    output.unlink()  # never remove a replacement supplied by another writer
        else:
            atomic_write(output, previous)
        raise
    return {
        "ok": True,
        "format": FORMAT,
        "preview": False,
        "source_mutation": False,
        "read_back": True,
        "output": str(output),
        "members": sorted(name for name in members if name != "manifest.json"),
        "bytes": output.stat().st_size,
        "secrets": "encrypted-in-bundle",
        "source_master_key": "not-included",
    }
