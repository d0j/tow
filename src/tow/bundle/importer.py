"""Import: a bundle checked, previewed, then written over the live stores after a checkpoint,
read back, and rolled back on any failure."""

from __future__ import annotations

import copy
import logging
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

from tow.bundle.archive import read_bundle
from tow.bundle.checkpoint import (
    IMPORT_CHECKPOINTS_KEPT,
    create_import_checkpoint,
    prune_import_checkpoints,
    recover_import_transactions,
    rollback_import,
    write_import_transaction,
)
from tow.bundle.errors import ExportImportError
from tow.bundle.io import FORMAT, atomic_write, json_bytes
from tow.bundle.path_maps import apply_path_maps, parse_path_maps
from tow.bundle.schema import validate_config_schema, validate_field
from tow.errors import Msg
from tow.log import log_event
from tow.paths import config_path, download_history_path, secrets_path, state_path
from tow.store import (
    SecretStoreError,
    load_secret_undo,
    load_secrets,
    master_fernet,
    persistence_lock,
    save_secret_undo,
    save_secrets,
    secret_undo_path,
)
from tow.yaml_guard import YamlLimitError
from tow.yaml_guard import dump as dump_yaml

# The backup folders of the install a bundle is restored into (a restore point keeps them).
_FOLDER_OVERRIDES = ("backup_dir", "restore_points_dir")


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
