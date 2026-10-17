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

from tow.backup_actions import KEPT_SETTINGS
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

# The type of each setting an import may keep from this install (KEPT_SETTINGS).
_OVERRIDE_TYPES: dict[str, type] = {
    "bind": str,
    "port": int,
    "allow_lan": bool,
    "backup_dir": str,
    "restore_points_dir": str,
    "allow_unc_save_paths": bool,
    "allow_private_tracker_hosts": bool,
    "allow_private_notifier_hosts": bool,
    "protection": dict,
}
# The password other devices sign in with (secrets.enc): kept with the settings above.
_ACCESS_SECRET = "lan_auth"
# What an import that cannot read this install's password writes instead: this computer only.
_NO_NETWORK: dict[str, Any] = {"allow_lan": False, "bind": "127.0.0.1"}


def this_install_settings() -> dict[str, Any]:
    """This install's security settings (KEPT_SETTINGS) as config overrides for an import that
    keeps them; None for one it does not have. Local only when its config cannot be read."""
    from tow.config import load_config

    try:
        live = load_config()
    except Exception:  # noqa: BLE001 - replacing a broken or missing config is often the point
        return {**dict.fromkeys(KEPT_SETTINGS), "bind": "127.0.0.1", "allow_lan": False}
    return {key: copy.deepcopy(live.get(key)) for key in KEPT_SETTINGS}


def _security_settings(
    parsed: dict[str, Any], destination_secrets: dict[str, Any] | None, kept: set[str]
) -> list[dict[str, Any]]:
    """The security settings the file holds otherwise than this install, and whether the import
    keeps this install's value (``kept``: config keys, and ``lan_auth`` for the password, which
    is said only as "set")."""
    from tow.config import load_config, validated

    try:
        live: dict[str, Any] = load_config()
    except Exception:  # noqa: BLE001 - no readable config here: every value of the file is new
        live = {}
    try:
        incoming = validated(copy.deepcopy(parsed["config"]))  # defaults filled in, as live's
    except Exception:  # noqa: BLE001 - validate_config_schema accepted it; compare it as written
        incoming = parsed["config"]
    changes = [
        {"setting": key, "file": incoming.get(key), "this_computer": live.get(key), "kept": key in kept}
        for key in KEPT_SETTINGS
        if incoming.get(key) != live.get(key)
    ]
    theirs = parsed["secrets"].get(_ACCESS_SECRET)
    if destination_secrets is None:
        try:
            destination_secrets = load_secrets()
        except SecretStoreError:
            destination_secrets = None
    ours = None if destination_secrets is None else destination_secrets.get(_ACCESS_SECRET)
    if destination_secrets is None or theirs != ours:
        changes.append(
            {
                "setting": _ACCESS_SECRET,
                "file": "set" if theirs else None,
                "this_computer": ("set" if ours else None) if destination_secrets is not None else "unknown",
                "kept": _ACCESS_SECRET in kept,
            }
        )
    return changes


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
    recover_unreadable_access: bool = False,
) -> dict[str, Any]:
    """Check a TOW file and, with ``apply``, write it over this install (checkpoint, read-back,
    rollback). ``preserve_secret_keys`` keeps this install's access password; when its store
    cannot be opened that is refused (reason ``local_secrets``) unless ``recover_unreadable_access``
    (``tow import`` on this computer): then no password is kept or taken and network access is
    turned off (``network_access: "off"``)."""
    try:
        with persistence_lock():
            return _import_bundle(
                input_path,
                passphrase,
                apply=apply,
                path_maps=path_maps,
                config_overrides=config_overrides,
                preserve_secret_keys=preserve_secret_keys,
                recover_unreadable_access=recover_unreadable_access,
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
    if not set(overrides).issubset(_OVERRIDE_TYPES):
        raise ExportImportError("unsupported config override")
    # Typed values from the CLI or the web form: no "yes" or "80" written for them.
    for key, expected in _OVERRIDE_TYPES.items():
        validate_field(overrides, key, expected, label="config.yaml")
    effective_config = copy.deepcopy(parsed["config"])
    for key, value in overrides.items():
        if value is None:
            effective_config.pop(key, None)  # this install has none: the default, not the bundle's
        else:
            effective_config[key] = copy.deepcopy(value)
    validate_config_schema(effective_config)
    parsed["config_bytes"] = _config_override_bytes(effective_config)
    parsed["config"] = effective_config


def _forget_volumes(state: dict[str, Any]) -> None:
    """The volumes the exporting computer recorded for its backup folders, and its notes of
    missing ones, are its own observations: an import keeps neither."""
    state.pop("backup_volumes", None)
    for topic in state.get("topics") or []:
        if isinstance(topic, dict):
            topic.pop("volume_unseen", None)


def _without_volumes(history: Any) -> Any:
    """The history without the volume each save folder was on (tow.folders.recorded_volume): that
    is the exporting computer's observation, never this one's. An imported topic records its own
    at its next applying check that confirms a file."""
    if not isinstance(history, dict) or not isinstance(history.get("topics"), dict):
        return history
    topics = {
        key: {name: value for name, value in record.items() if name != "volume"} if isinstance(record, dict) else record
        for key, record in history["topics"].items()
    }
    return {**history, "topics": topics}


def _import_bundle(
    input_path: Path,
    passphrase: str,
    *,
    apply: bool = False,
    path_maps: Iterable[str] | None = None,
    config_overrides: dict[str, Any] | None = None,
    preserve_secret_keys: Iterable[str] | None = None,
    recover_unreadable_access: bool = False,
) -> dict[str, Any]:
    recover_import_transactions()
    parsed = read_bundle(Path(input_path), passphrase)
    overrides = dict(config_overrides or {})
    secret_keys = {str(key) for key in (preserve_secret_keys or ())}
    if not secret_keys.issubset({_ACCESS_SECRET}):
        raise ExportImportError("unsupported preserved secret key")
    destination_secrets: dict[str, Any] | None = None
    unreadable = False
    if secret_keys:
        try:
            destination_secrets = load_secrets()
        except SecretStoreError as exc:
            if not recover_unreadable_access:
                raise ExportImportError(
                    "cannot read destination secrets to preserve access", reason="local_secrets"
                ) from exc
            # This install's passwords cannot be opened (a lost or replaced master key, a damaged
            # store): its access password is unknown. The file's is not taken instead, and with no
            # password known network access is off and TOW listens on loopback only, whatever the
            # kept config says (no other device, no external access key reaches it). Restoring a
            # file on this computer is the way out of this.
            destination_secrets = {}
            unreadable = True
            overrides.update(_NO_NETWORK)
    # Said before this install's values replace the file's: what the file would have changed.
    kept = (set(overrides) - set(_NO_NETWORK) if unreadable else set(overrides)) | (
        set() if unreadable else secret_keys
    )
    security = _security_settings(parsed, None if unreadable else destination_secrets, kept)
    if overrides:
        _apply_config_overrides(parsed, overrides)
    if destination_secrets is not None:
        effective_secrets = copy.deepcopy(parsed["secrets"])
        for key in secret_keys:
            if key in destination_secrets:
                effective_secrets[key] = copy.deepcopy(destination_secrets[key])
            else:
                effective_secrets.pop(key, None)
        parsed["secrets"] = effective_secrets
    mappings = parse_path_maps(path_maps)
    mapped_state, warnings = apply_path_maps(parsed["state"], mappings)
    _forget_volumes(mapped_state)
    state_bytes = json_bytes(mapped_state, readable=True)  # as tow.store writes each file
    history_bytes = json_bytes(_without_volumes(parsed["history"]))
    members = sorted(name for name in parsed["members"] if name != "manifest.json")
    result = {
        "ok": True,
        "format": FORMAT,
        "preview": not apply,
        "apply_required": not apply,
        "members": members,
        "path_maps": [{"old": old, "new": new} for old, new in mappings],
        "config_overrides": sorted(overrides),
        "security_settings": security,
        "preserved_secret_keys": sorted(secret_keys),
        "local_secrets_unreadable": unreadable,
        **({"network_access": "off"} if unreadable else {}),
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
