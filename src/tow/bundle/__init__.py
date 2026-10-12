"""The portable TOW file (``.towx``): export, import, the import checkpoint and its rollback.

- ``errors``: ``ExportImportError``;
- ``io``: the format name, the size limit, JSON bytes, checksums, bounded reads, atomic writes;
- ``secret_scan``: no credential-named field outside the encrypted secrets;
- ``schema``: what each member may contain;
- ``crypto``: the passphrase key (PBKDF2) and the Fernet envelope;
- ``archive``: the zip, its manifest, reading a bundle back;
- ``path_maps``: ``--path-map OLD=NEW`` for the imported folders;
- ``checkpoint``: the import checkpoint, its transaction, recovery, rollback and pruning.

The rest of TOW imports only the names below. A test that replaces a name does it in the module
that reads it when it runs. What has no module of its own yet is below.
"""

from __future__ import annotations

import contextlib
import copy
import logging
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

from tow import __version__
from tow.bundle.archive import (
    ALLOWED_MEMBERS,
    OPTIONAL_MEMBERS,
    REQUIRED_MEMBERS,
    read_bundle,
    verify_bundle,
    zip_payload,
)
from tow.bundle.checkpoint import (
    CHECKPOINT_FORMAT,
    IMPORT_CHECKPOINTS_KEPT,
    TRANSACTION_FORMAT,
    checkpoint_file,
    create_import_checkpoint,
    prune_import_checkpoints,
    recover_import_transactions,
    recover_interrupted_import,
    rollback_import,
    write_import_transaction,
)
from tow.bundle.crypto import KDF_ITERATIONS, KDF_NAME, KDF_SALT_BYTES, seal_outer
from tow.bundle.errors import ExportImportError
from tow.bundle.io import FORMAT, MAX_BUNDLE_BYTES, atomic_write, json_bytes, read_limited, sha256
from tow.bundle.path_maps import apply_path_maps, parse_path_maps
from tow.bundle.schema import parse_mapping, utf8_config, validate_config_schema, validate_field
from tow.bundle.secret_scan import refuse_plaintext_secrets
from tow.errors import Msg
from tow.log import export_event_projection, log_event, read_events
from tow.paths import config_path, download_history_path, secrets_path, state_path
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
    master_fernet,
    persistence_lock,
    restrict_file,
    save_secret_undo,
    save_secrets,
    secret_undo_path,
)
from tow.yaml_guard import YamlLimitError
from tow.yaml_guard import dump as dump_yaml

# The backup folders of the install a bundle is restored into (a restore point keeps them).
_FOLDER_OVERRIDES = ("backup_dir", "restore_points_dir")


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


def _destination_readback(parsed: dict[str, Any], state_bytes: bytes, history_bytes: bytes) -> None:
    if config_path().read_bytes() != parsed["config_bytes"]:
        raise ExportImportError("config read-back mismatch")
    if state_path().read_bytes() != state_bytes:
        raise ExportImportError("state read-back mismatch")
    if download_history_path().read_bytes() != history_bytes:
        raise ExportImportError("download history read-back mismatch")
    try:
        if load_secrets() != parsed["secrets"]:
            raise ExportImportError("secrets semantic read-back mismatch")
        if parsed["undo"] is not None and load_secret_undo("settings-v1") != parsed["undo"]["secrets"]:
            raise ExportImportError("settings undo semantic read-back mismatch")
    except SecretStoreError as exc:
        raise ExportImportError("encrypted destination secret read-back failed") from exc


def _record_import_event(checkpoint: Path, mappings: list[tuple[str, str]], members: list[str]) -> bool:
    try:
        return log_event(
            "portable_import_applied",
            operation_id=uuid.uuid4().hex,
            format=FORMAT,
            checkpoint=str(checkpoint),
            path_maps=mappings,
            members=members,
        )
    except Exception as exc:  # noqa: BLE001 - the import is committed; a lost audit line is not fatal
        logging.getLogger("tow.bundle").warning("import audit event not written: %s", type(exc).__name__)
        return False


def import_bundle(
    input_path: Path,
    passphrase: str,
    *,
    apply: bool = False,
    path_maps: Iterable[str] | None = None,
    config_overrides: dict[str, Any] | None = None,
    preserve_secret_keys: Iterable[str] | None = None,
) -> dict[str, Any]:
    try:
        with persistence_lock():
            return _import_bundle(
                input_path,
                passphrase,
                apply=apply,
                path_maps=path_maps,
                config_overrides=config_overrides,
                preserve_secret_keys=preserve_secret_keys,
            )
    except ExportImportError:
        raise
    except Exception as exc:
        raise ExportImportError("cannot import TOW bundle safely") from exc


def _config_override_bytes(effective_config: dict[str, Any]) -> bytes:
    try:
        return dump_yaml(effective_config).encode("utf-8")
    except YamlLimitError as exc:
        raise ExportImportError(f"invalid config.yaml: {exc}") from exc
    except (TypeError, ValueError, UnicodeError, yaml.YAMLError) as exc:
        raise ExportImportError("cannot apply safe config overrides") from exc


def _apply_config_overrides(parsed: dict[str, Any], overrides: dict[str, Any]) -> None:
    """This install's own settings over the bundle's config.yaml (``parsed`` is changed in place)."""
    if not set(overrides).issubset({"bind", "port", "allow_lan", *_FOLDER_OVERRIDES}):
        raise ExportImportError("unsupported config override")
    # Typed values from the CLI or the web form: no "yes" or "80" written for them.
    validate_field(overrides, "bind", str, label="config.yaml")
    validate_field(overrides, "port", int, label="config.yaml")
    validate_field(overrides, "allow_lan", bool, label="config.yaml")
    for key in _FOLDER_OVERRIDES:
        validate_field(overrides, key, str, label="config.yaml")
    effective_config = copy.deepcopy(parsed["config"])
    for key, value in overrides.items():
        if key in _FOLDER_OVERRIDES and value is None:
            effective_config.pop(key, None)  # this install has none: the default folder, not the bundle's
        else:
            effective_config[key] = copy.deepcopy(value)
    validate_config_schema(effective_config)
    parsed["config_bytes"] = _config_override_bytes(effective_config)
    parsed["config"] = effective_config


def _import_bundle(
    input_path: Path,
    passphrase: str,
    *,
    apply: bool = False,
    path_maps: Iterable[str] | None = None,
    config_overrides: dict[str, Any] | None = None,
    preserve_secret_keys: Iterable[str] | None = None,
) -> dict[str, Any]:
    recover_import_transactions()
    parsed = read_bundle(Path(input_path), passphrase)
    overrides = dict(config_overrides or {})
    if overrides:
        _apply_config_overrides(parsed, overrides)
    secret_keys = {str(key) for key in (preserve_secret_keys or ())}
    if not secret_keys.issubset({"lan_auth"}):
        raise ExportImportError("unsupported preserved secret key")
    if secret_keys:
        try:
            destination_secrets = load_secrets()
        except SecretStoreError as exc:
            raise ExportImportError("cannot read destination secrets to preserve access") from exc
        effective_secrets = copy.deepcopy(parsed["secrets"])
        for key in secret_keys:
            if key in destination_secrets:
                effective_secrets[key] = copy.deepcopy(destination_secrets[key])
            else:
                effective_secrets.pop(key, None)
        parsed["secrets"] = effective_secrets
    mappings = parse_path_maps(path_maps)
    mapped_state, warnings = apply_path_maps(parsed["state"], mappings)
    state_bytes = json_bytes(mapped_state, readable=True)  # as tow.store writes each file
    history_bytes = json_bytes(parsed["history"])
    members = sorted(name for name in parsed["members"] if name != "manifest.json")
    result = {
        "ok": True,
        "format": FORMAT,
        "preview": not apply,
        "apply_required": not apply,
        "members": members,
        "path_maps": [{"old": old, "new": new} for old, new in mappings],
        "config_overrides": sorted(overrides),
        "preserved_secret_keys": sorted(secret_keys),
        "path_warnings": warnings,
        "scheduler": "not restored; turn autostart on separately with tow autostart on",
        "client_media": "not transferred; qBittorrent/client check is separate",
        "committed": False,
        "log_recorded": False,
    }
    if not apply:
        return result
    if secrets_path().is_file():
        raise ExportImportError(
            "destination legacy plaintext secrets require explicit migration before import",
            owner_text=Msg("cli.bundle.legacy_secrets", command="tow secrets migrate"),
        )
    try:
        master_fernet()
    except SecretStoreError as exc:
        raise ExportImportError(
            "destination master key is unavailable or invalid", owner_text=Msg("cli.bundle.master_key")
        ) from exc

    checkpoint = create_import_checkpoint()
    try:
        atomic_write(config_path(), parsed["config_bytes"])
        atomic_write(state_path(), state_bytes)
        atomic_write(download_history_path(), history_bytes)
        save_secrets(parsed["secrets"])
        if parsed["undo"] is not None:
            save_secret_undo(parsed["undo"]["secrets"])
        else:
            try:
                secret_undo_path().unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise ExportImportError("cannot remove stale settings undo snapshot") from exc
        _destination_readback(parsed, state_bytes, history_bytes)
        write_import_transaction(checkpoint, status="committed", operation_id=uuid.uuid4().hex)
    except (ExportImportError, SecretStoreError, OSError) as exc:
        try:
            rollback_import(checkpoint, apply=True)
        except ExportImportError as rollback_exc:
            raise ExportImportError(
                "import failed and destination rollback also failed", owner_text=Msg("cli.bundle.not_restored")
            ) from rollback_exc
        if isinstance(exc, ExportImportError):
            exc.owner_text = exc.owner_text or Msg("cli.bundle.restored")  # what failed is in the message
            raise
        raise ExportImportError(
            "import failed; destination was restored", owner_text=Msg("cli.bundle.restored")
        ) from exc

    result.update(
        {"preview": False, "apply_required": False, "checkpoint": str(checkpoint), "read_back": True, "committed": True}
    )
    result["log_recorded"] = _record_import_event(checkpoint, mappings, members)
    if not result["log_recorded"]:
        result["log_error"] = "audit event could not be persisted"
    try:
        write_import_transaction(checkpoint, status="committed", log_recorded=result["log_recorded"])
    except ExportImportError:
        result["transaction_log_status"] = "unrecorded"
    result["pruned_checkpoints"] = prune_import_checkpoints(keep=IMPORT_CHECKPOINTS_KEPT)
    return result


__all__ = [
    "ALLOWED_MEMBERS",
    "CHECKPOINT_FORMAT",
    "FORMAT",
    "IMPORT_CHECKPOINTS_KEPT",
    "KDF_ITERATIONS",
    "KDF_NAME",
    "KDF_SALT_BYTES",
    "MAX_BUNDLE_BYTES",
    "OPTIONAL_MEMBERS",
    "REQUIRED_MEMBERS",
    "TRANSACTION_FORMAT",
    "ExportImportError",
    "export_bundle",
    "import_bundle",
    "load_json_for_export",
    "parse_path_maps",
    "prune_import_checkpoints",
    "recover_import_transactions",
    "recover_interrupted_import",
    "rollback_import",
    "verify_bundle",
]
